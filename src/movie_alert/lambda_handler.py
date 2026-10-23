"""Bounded scheduled checks using the same query and delivery path as the CLI."""

import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from movie_alert.config import AppConfig, ConfigError, load_config
from movie_alert.main import create_provider, deliver_notifications, run_live
from movie_alert.notifier import NotificationError
from movie_alert.pipeline import current_time
from movie_alert.runtime import NotificationEnvironment, create_poll_lock, notification_environment

logger = logging.getLogger(__name__)


class LambdaContext(Protocol):
    aws_request_id: str

    def get_remaining_time_in_millis(self) -> int: ...


def _active(config: AppConfig, now: datetime) -> bool:
    return any(
        rule.enabled and rule.active_from <= now < rule.active_until for rule in config.watches
    )


def _query_budget(config: AppConfig) -> float:
    # HTTPX bounds individual phases, and relay fallback can make a second request.
    # Leave time for cleanup after the bounded request/retry sequence.
    attempts = config.http.max_retries + 1
    return 8 * config.http.timeout_seconds * attempts + 10


def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    started = time.monotonic()
    try:
        return _handle(event, context, started)
    except Exception as error:
        # Never serialize the event, environment, SDK/HTTP exception or webhook URL.
        logger.error(
            "실행 실패: request_id=%s type=%s", context.aws_request_id, type(error).__name__
        )
        raise RuntimeError("Movie Alert 실행 실패: CloudWatch의 오류 유형을 확인하세요.") from None


def _handle(event: dict[str, Any], context: LambdaContext, started: float) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ConfigError("Lambda 이벤트는 객체여야 합니다.")
    notify = event.get("notify", False)
    checks = event.get("checks", 2)
    if type(notify) is not bool or type(checks) is not int or checks not in (1, 2):
        raise ConfigError("notify는 bool, checks는 1 또는 2여야 합니다.")
    now = current_time()
    scheduled_at = event.get("scheduled_at")
    if scheduled_at is not None:
        try:
            scheduled = datetime.fromisoformat(scheduled_at)
        except (TypeError, ValueError):
            raise ConfigError("스케줄 시각은 ISO 날짜·시간이어야 합니다.") from None
        if scheduled.utcoffset() is None:
            raise ConfigError("스케줄 시각에는 시간대가 필요합니다.")
        if not -60 <= (now - scheduled).total_seconds() <= 90:
            logger.info("지연되거나 잘못된 스케줄 호출 생략: request_id=%s", context.aws_request_id)
            return {"status": "skipped", "reason": "stale_schedule", "checks": []}
    config_path = Path(os.environ.get("MOVIE_ALERT_CONFIG", "config/watch.toml"))
    config = load_config(config_path)
    if not _active(config, now):
        logger.info("감시 기간 밖 호출 생략: request_id=%s", context.aws_request_id)
        return {"status": "skipped", "reason": "inactive", "checks": []}
    environment = notification_environment() if notify else None
    lock = create_poll_lock()
    owner = context.aws_request_id
    lease_seconds = math.ceil(context.get_remaining_time_in_millis() / 1000) + 60
    if not lock.acquire(owner, int(current_time().timestamp()), lease_seconds):
        logger.info("중첩 호출 생략: request_id=%s", owner)
        return {"status": "skipped", "reason": "already_running", "checks": []}
    try:
        return _run_checks(config, context, started, notify, checks, environment)
    finally:
        lock.release(owner)


def _run_checks(
    config: AppConfig,
    context: LambdaContext,
    started: float,
    notify: bool,
    checks: int,
    environment: NotificationEnvironment | None,
) -> dict[str, Any]:
    summaries = []
    failed = False
    for index in range(checks):
        delay = max(0.0, started + index * 30 - time.monotonic())
        if context.get_remaining_time_in_millis() / 1000 < delay + _query_budget(config):
            logger.warning(
                "추가 확인 생략: request_id=%s reason=remaining_time", context.aws_request_id
            )
            if not summaries:
                raise ConfigError("첫 조회에 필요한 실행 시간이 부족합니다.")
            break
        if delay:
            time.sleep(delay)
        if not _active(config, current_time()):
            break
        round_started = time.monotonic()
        with httpx.Client() as client:
            selected, errors = run_live(
                config,
                create_provider(config, client),
                current_time(),
                can_query=lambda: (
                    context.get_remaining_time_in_millis() / 1000 >= _query_budget(config)
                ),
            )
            outcomes = []
            if environment is not None and not errors:
                outcomes = deliver_notifications(
                    config,
                    selected,
                    client,
                    environment,
                    current_time(),
                    can_notify=lambda: (
                        context.get_remaining_time_in_millis() / 1000
                        # Allow SDK retries for claim, ownership and completion,
                        # as well as the Discord request and cleanup.
                        >= 4 * config.http.timeout_seconds + 80
                    ),
                )
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        summary = {
            "check": index + 1,
            "detected": len(selected),
            "errors": len(errors),
            "outcomes": counts,
            "elapsed_seconds": round(time.monotonic() - round_started, 3),
        }
        summaries.append(summary)
        logger.info("확인 완료: request_id=%s summary=%s", context.aws_request_id, summary)
        if errors or any(
            counts.get(status) for status in ("deferred", "uncertain", "failed", "state_error")
        ):
            failed = True
            break
    if failed:
        raise NotificationError("조회·알림·상태 처리 오류가 발생했습니다.")
    return {"status": "ok", "notify": notify, "checks": summaries}
