"""Durable, conservative daily request reservations for scheduled relay polling."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from movie_alert.config import PollingSettings
from movie_alert.models import SEOUL
from movie_alert.pipeline import current_time
from movie_alert.polling import next_polling_start, polling_allowed
from movie_alert.providers.base import DailyQuotaExceeded, PollingPausedError
from movie_alert.state import DynamoDBClient, StateError


@dataclass(frozen=True)
class QuotaSnapshot:
    day: date
    used: int
    limit: int
    remote_exhausted: bool = False

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit or self.remote_exhausted

    @property
    def notification_key(self) -> str:
        return f"operations:v1:relay-quota:{self.day.isoformat()}"


class DynamoDBRequestBudget:
    def __init__(
        self,
        client: DynamoDBClient,
        table: str,
        settings: PollingSettings,
        *,
        clock: Callable[[], datetime] = current_time,
    ) -> None:
        if not 1 <= settings.daily_request_limit <= 3000:
            raise ValueError("일일 요청 예산은 1~3000이어야 합니다.")
        self.client, self.table, self.settings, self.clock = client, table, settings, clock
        self.day = clock().astimezone(SEOUL).date()
        self.snapshot = QuotaSnapshot(self.day, 0, settings.daily_request_limit)
        self._lock = Lock()

    def _write(
        self, expression: str, values: dict[str, Any], *, condition: str | None = None
    ) -> QuotaSnapshot:
        names = {"#used": "used", "#stopped": "remote_exhausted", "#expires": "expires_at"}
        # Include only names referenced by this request; DynamoDB rejects unused aliases.
        names = {
            key: value
            for key, value in names.items()
            if key in expression or key in (condition or "")
        }
        try:
            result = self.client.update_item(
                TableName=self.table,
                Key={"id": {"S": f"relay-budget:v1:{self.day.isoformat()}"}},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
                **(
                    {
                        "ConditionExpression": condition,
                        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                    }
                    if condition
                    else {}
                ),
            )
            item = result["Attributes"]
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise StateError("릴레이 일일 요청 예산을 저장하지 못했습니다.") from None
            self.snapshot = self._parse(error.response.get("Item", {}))
            if not self.snapshot.exhausted:
                raise StateError("릴레이 일일 요청 예산의 조건을 확인하지 못했습니다.") from None
            raise DailyQuotaExceeded("릴레이 일일 요청 한도에 도달하여 감시를 쉽니다.") from None
        except BotoCoreError:
            raise StateError("릴레이 일일 요청 예산을 저장하지 못했습니다.") from None
        except (KeyError, TypeError):
            raise StateError("릴레이 일일 요청 예산 응답을 확인하지 못했습니다.") from None
        self.snapshot = self._parse(item)
        return self.snapshot

    def _parse(self, item: dict[str, Any]) -> QuotaSnapshot:
        try:
            used = int(item["used"]["N"])
            stopped = item.get("remote_exhausted", {"BOOL": False})["BOOL"]
            if not 0 <= used <= 3000 or type(stopped) is not bool:
                raise ValueError
            return QuotaSnapshot(self.day, used, self.settings.daily_request_limit, stopped)
        except (KeyError, TypeError, ValueError):
            raise StateError("릴레이 일일 요청 예산이 올바르지 않습니다.") from None

    def load(self) -> QuotaSnapshot:
        # UpdateItem's returned values avoid a new GetItem permission or a table scan.
        with self._lock:
            return self._write(
                "SET #used = if_not_exists(#used, :zero), #expires = :expires",
                {
                    ":zero": {"N": "0"},
                    ":expires": {"N": str(int(self.clock().timestamp()) + 2 * 86400)},
                },
            )

    def before_request(self) -> date:
        with self._lock:
            now = self.clock()
            if now.astimezone(SEOUL).date() != self.day or not polling_allowed(now, self.settings):
                raise PollingPausedError("감시 휴식 시간으로 추가 조회를 중단합니다.")
            if self.snapshot.exhausted:
                raise DailyQuotaExceeded("릴레이 일일 요청 한도에 도달하여 감시를 쉽니다.")
            self._write(
                "SET #used = if_not_exists(#used, :zero) + :one, #expires = :expires",
                {
                    ":zero": {"N": "0"},
                    ":one": {"N": "1"},
                    ":limit": {"N": str(self.settings.daily_request_limit)},
                    ":false": {"BOOL": False},
                    ":expires": {"N": str(int(now.timestamp()) + 2 * 86400)},
                },
                condition=(
                    "(attribute_not_exists(#used) OR #used < :limit) AND "
                    "(attribute_not_exists(#stopped) OR #stopped = :false)"
                ),
            )
            # The final allowed reservation must still be sent and its response processed.
            return self.day

    def after_response(self, response: httpx.Response, day: date) -> None:
        if day != self.day:
            raise StateError("릴레이 요청 예산의 날짜가 일치하지 않습니다.")
        exhausted = response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
        if response.status_code == 429:
            try:
                payload = response.json()
                exhausted = exhausted or (
                    isinstance(payload, dict)
                    and isinstance(payload.get("error"), dict)
                    and payload["error"].get("code") == "DAILY_RATE_LIMIT_EXCEEDED"
                )
            except ValueError:
                pass
        if exhausted:
            with self._lock:
                self._write("SET #stopped = :true", {":true": {"BOOL": True}})
            if response.status_code == 429:
                raise DailyQuotaExceeded("릴레이가 일일 호출 한도 소진을 응답하여 감시를 쉽니다.")


def quota_content(snapshot: QuotaSnapshot, settings: PollingSettings) -> str:
    resume = next_polling_start(
        datetime.combine(snapshot.day, datetime.min.time(), SEOUL), settings
    )
    reason = (
        "릴레이가 일일 호출 한도 소진을 응답했습니다."
        if snapshot.remote_exhausted
        else f"오늘의 릴레이 요청 예산 {snapshot.limit:,}회를 모두 사용했습니다."
    )
    return (
        "Movie Alert 감시 일시 중지\n"
        f"{reason}\n"
        f"봇 요청 예약: {snapshot.used:,}회 (재시도·영화 목록 확인 포함)\n"
        f"다음 감시: {resume:%Y-%m-%d %H:%M} KST\n"
        "그때까지 시간표 조회를 쉽니다."
    )
