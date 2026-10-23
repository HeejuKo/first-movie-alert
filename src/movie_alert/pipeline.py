"""Coordinate first-session alerts and durable delivery state."""

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from movie_alert.models import SEOUL, Showtime
from movie_alert.notifier import (
    DeliveryReceipt,
    NotificationConnectionError,
    NotificationDeliveryUnknown,
    NotificationRateLimited,
    NotificationRejected,
    notification_content,
)
from movie_alert.state import StateError, StateStore


class Notifier(Protocol):
    def send(self, showtime: Showtime) -> DeliveryReceipt: ...

    def send_text(self, content: str) -> DeliveryReceipt: ...


@dataclass(frozen=True)
class AlertOutcome:
    key: str
    status: str
    detail: str
    message_id: str | None = None


def current_time() -> datetime:
    return datetime.now(SEOUL)


def showtime_key(showtime: Showtime) -> str:
    """Use actual screening identity, never the first-session rank or scan time."""
    identity = [
        showtime.provider,
        showtime.branch_id,
        showtime.movie_id,
        showtime.business_date.isoformat(),
        showtime.screen_id,
        showtime.sequence,
        showtime.starts_at.astimezone(SEOUL).isoformat(),
    ]
    serialized = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return "showtime:v1:" + hashlib.sha256(serialized.encode()).hexdigest()


def block_reason(showtime: Showtime, now: datetime) -> str | None:
    if now.utcoffset() is None or showtime.starts_at.utcoffset() is None:
        raise ValueError("알림 판정 시각에는 시간대가 필요합니다.")
    if showtime.starts_at <= now:
        return "이미 시작한 회차입니다."
    return None


def notification_preview(showtime: Showtime, now: datetime) -> dict[str, object]:
    reason = block_reason(showtime, now)
    content = (
        notification_content(showtime)
        if reason is None
        else (
            f"상태 확인용 미리보기 · {showtime.movie_name}\n"
            f"{showtime.branch_name} · {showtime.starts_at.astimezone(SEOUL):%Y-%m-%d %H:%M} KST\n"
            f"알림 미전송: {reason}"
        )
    )
    return {
        "key": showtime_key(showtime),
        "eligible": reason is None,
        "reason": reason,
        "content": content,
    }


class AlertDispatcher:
    def __init__(
        self,
        state: StateStore,
        notifier: Notifier,
        *,
        clock: Callable[[], datetime] = current_time,
        lease_seconds: int = 120,
        retry_delay_seconds: int = 60,
        retention_seconds: int = 30 * 24 * 60 * 60,
    ) -> None:
        for value in (lease_seconds, retry_delay_seconds, retention_seconds):
            if type(value) is not int or value <= 0:
                raise ValueError("알림 처리 시간 설정은 양의 정수여야 합니다.")
        self.state = state
        self.notifier = notifier
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.retention_seconds = retention_seconds

    def dispatch(self, showtime: Showtime, *, active_until: datetime) -> AlertOutcome:
        now = self.clock()
        if active_until.utcoffset() is None:
            raise ValueError("감시 종료 시각에는 시간대가 필요합니다.")
        key = showtime_key(showtime)
        reason = block_reason(showtime, now)
        if reason is not None:
            return AlertOutcome(key, "skipped", reason)
        if now >= active_until:
            return AlertOutcome(key, "skipped", "감시 기간이 종료되었습니다.")
        return self._dispatch(
            key,
            now=now,
            send=lambda: self.notifier.send(showtime),
            active_until=active_until,
            valid_until=showtime.starts_at,
            retain_until=showtime.ends_at,
        )

    def dispatch_message(self, key: str, content: str, *, active_until: datetime) -> AlertOutcome:
        """Use the same claims and ambiguous-delivery policy for operational alerts."""
        if not key.startswith("operations:v1:"):
            raise ValueError("운영 알림에는 별도의 상태 키가 필요합니다.")
        now = self.clock()
        if active_until.utcoffset() is None:
            raise ValueError("감시 종료 시각에는 시간대가 필요합니다.")
        if now >= active_until:
            return AlertOutcome(key, "skipped", "감시 기간이 종료되었습니다.")
        return self._dispatch(
            key,
            now=now,
            send=lambda: self.notifier.send_text(content),
            active_until=active_until,
            valid_until=active_until,
            retain_until=active_until,
        )

    def _dispatch(
        self,
        key: str,
        *,
        now: datetime,
        send: Callable[[], DeliveryReceipt],
        active_until: datetime,
        valid_until: datetime,
        retain_until: datetime,
    ) -> AlertOutcome:
        owner = uuid4().hex
        claim_epoch = int(now.timestamp())
        lease_until_epoch = claim_epoch + self.lease_seconds
        try:
            if not self.state.claim(key, owner, claim_epoch, self.lease_seconds):
                return AlertOutcome(
                    key, "not_claimed", "이미 처리됐거나 다른 실행이 처리 중입니다."
                )
        except StateError:
            return AlertOutcome(key, "state_error", "처리 권한 확보에 실패해 전송하지 않았습니다.")

        now = self.clock()
        retention = max(
            self.retention_seconds,
            math.ceil(max(active_until.timestamp(), retain_until.timestamp()) - now.timestamp())
            + 7 * 24 * 60 * 60,
        )
        try:
            if now >= active_until or valid_until <= now:
                self.state.mark_failed(key, owner, int(now.timestamp()), retention)
                return AlertOutcome(
                    key, "skipped", "전송 전에 감시 기간 또는 상영 시작이 지났습니다."
                )
            self.state.ensure_owned(key, owner, int(now.timestamp()))
            now = self.clock()
            if now.timestamp() >= lease_until_epoch:
                return AlertOutcome(key, "state_error", "처리 기한이 만료되어 전송하지 않았습니다.")
            if now >= active_until or valid_until <= now:
                self.state.mark_failed(key, owner, int(now.timestamp()), retention)
                return AlertOutcome(key, "skipped", "소유권 확인 중 알림 유효 기간이 끝났습니다.")
        except StateError:
            return AlertOutcome(
                key, "state_error", "처리 소유권을 확인하지 못해 전송하지 않았습니다."
            )
        try:
            receipt = send()
        except (NotificationConnectionError, NotificationRateLimited) as error:
            now = self.clock()
            required_delay = self.retry_delay_seconds
            if isinstance(error, NotificationRateLimited) and error.retry_after_seconds is not None:
                required_delay = max(required_delay, error.retry_after_seconds)
            retry_epoch = math.ceil(now.timestamp() + required_delay)
            delay = retry_epoch - int(now.timestamp())
            try:
                deadline = min(active_until.timestamp(), valid_until.timestamp())
                if retry_epoch >= deadline:
                    self.state.mark_failed(key, owner, int(now.timestamp()), retention)
                    return AlertOutcome(
                        key, "failed", "재시도 대기 후에는 유효한 알림 기간이 끝납니다."
                    )
                self.state.release_for_retry(
                    key, owner, int(now.timestamp()), delay, max(retention, delay + 1)
                )
            except StateError:
                return AlertOutcome(
                    key, "state_error", "전송 실패 후 재시도 상태를 저장하지 못했습니다."
                )
            return AlertOutcome(
                key,
                "deferred",
                "전송 실패를 기록했습니다. 대기 후 후속 실행에서 재시도할 수 있습니다.",
            )
        except NotificationRejected:
            return self._hold(key, owner, retention, uncertain=False)
        except NotificationDeliveryUnknown:
            return self._hold(key, owner, retention, uncertain=True)
        try:
            self.state.mark_sent(key, owner, int(self.clock().timestamp()), retention)
        except StateError:
            held = self._hold(key, owner, retention, uncertain=True)
            return AlertOutcome(
                key,
                "uncertain",
                "Discord 성공 응답 후 상태 저장에 실패했습니다. "
                + (
                    "자동 재전송을 보류합니다."
                    if held.status == "uncertain"
                    else "상태 확인이 필요합니다."
                ),
                receipt.message_id,
            )
        return AlertOutcome(
            key, "sent", "Discord 메시지 생성과 완료 상태 저장을 확인했습니다.", receipt.message_id
        )

    def _hold(self, key: str, owner: str, retention: int, *, uncertain: bool) -> AlertOutcome:
        try:
            if uncertain:
                self.state.mark_uncertain(key, owner, int(self.clock().timestamp()), retention)
            else:
                self.state.mark_failed(key, owner, int(self.clock().timestamp()), retention)
        except StateError:
            return AlertOutcome(
                key, "state_error", "전송 결과 보관에 실패해 수동 상태 확인이 필요합니다."
            )
        if uncertain:
            return AlertOutcome(key, "uncertain", "전송 여부가 불명확해 자동 재전송을 보류합니다.")
        return AlertOutcome(key, "failed", "Discord가 요청을 거부해 변경 없이 재시도하지 않습니다.")
