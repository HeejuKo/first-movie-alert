"""Shared provider queries and notification delivery for CLI and Lambda."""

import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import httpx

from movie_alert.checker import select_first_showtimes
from movie_alert.config import AppConfig, WatchRule
from movie_alert.models import SEOUL, Showtime
from movie_alert.outcomes import AlertOutcome
from movie_alert.pipeline import block_reason, showtime_key
from movie_alert.providers.base import (
    AccessDeniedError,
    Provider,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)
from movie_alert.providers.cgv import CgvProvider
from movie_alert.providers.cgv_relay import CgvRelayProvider
from movie_alert.runtime import NotificationEnvironment, create_dispatcher

logger = logging.getLogger(__name__)


def create_provider(config: AppConfig, client: httpx.Client) -> Provider:
    if config.cgv.source == "mcp_relay":
        return CgvRelayProvider(client, config.cgv, config.http)
    return CgvProvider(client, config.cgv, config.http)


def _fetch_showtimes(provider: Provider, rule: WatchRule, show_date: date) -> list[Showtime]:
    logger.info("상영표 조회 중: %s · %s", rule.branch_name, show_date)
    rows = provider.fetch_showtimes(rule.movie_id, rule.branch_id, show_date)
    logger.info(
        "상영표 조회 완료: %s · %s (%d회차)",
        rule.branch_name,
        show_date,
        len(rows),
    )
    return rows


def run_live(
    config: AppConfig,
    provider: Provider,
    now: datetime,
    *,
    can_query: Callable[[], bool] | None = None,
) -> tuple[list[Showtime], list[str]]:
    if now.utcoffset() is None:
        raise ValueError("now에는 시간대가 필요합니다.")
    now = now.astimezone(SEOUL)
    results = []
    errors = []
    active_rules = [
        rule
        for rule in config.watches
        if rule.enabled and rule.active_from <= now < rule.active_until
    ]
    dates = sorted({show_date for rule in active_rules for show_date in rule.dates})
    for show_date in dates:
        if show_date < now.date():
            continue
        date_rules = [
            rule
            for rule in active_rules
            if show_date in rule.dates and show_date.isoweekday() in rule.weekdays
        ]
        if not date_rules:
            continue

        fetched_by_rule: dict[int, tuple[WatchRule, list[Showtime]]] = {}
        date_had_error = False

        parallel = config.http.max_parallel_requests
        for batch_start in range(0, len(date_rules), parallel):
            if can_query is not None and not can_query():
                message = "실행 시간이 부족하여 남은 조회를 중단합니다."
                logger.error("%s", message)
                errors.append(message)
                return results, errors
            batch = list(enumerate(date_rules[batch_start : batch_start + parallel], batch_start))
            stop_queries = False
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {
                    executor.submit(_fetch_showtimes, provider, rule, show_date): (index, rule)
                    for index, rule in batch
                }
                for future in as_completed(futures):
                    index, rule = futures[future]
                    try:
                        fetched_by_rule[index] = (rule, future.result())
                    except (
                        AccessDeniedError,
                        ProviderUnavailableError,
                        RateLimitError,
                    ) as error:
                        message = f"{rule.name} {show_date}: {error}"
                        logger.error("%s", message)
                        errors.append(message)
                        stop_queries = True
                        for pending in futures:
                            pending.cancel()
                        break
                    except ProviderError as error:
                        message = f"{rule.name} {show_date}: {error}"
                        logger.error("%s", message)
                        errors.append(message)
                        date_had_error = True
            if stop_queries:
                for _, (completed_rule, rows) in sorted(fetched_by_rule.items()):
                    results.extend(select_first_showtimes(rows, completed_rule))
                return results, errors

        ordered_rows = [item for _, item in sorted(fetched_by_rule.items())]
        for completed_rule, rows in ordered_rows:
            results.extend(select_first_showtimes(rows, completed_rule))

        if (
            not date_had_error
            and len(fetched_by_rule) == len(date_rules)
            and all(not rows for _, rows in ordered_rows)
        ):
            logger.info("%s: 모든 지점에 시간표가 없어 이후 날짜 조회를 중단합니다.", show_date)
            break
    return results, errors


def deliver_notifications(
    config: AppConfig,
    showtimes: list[Showtime],
    client: httpx.Client,
    environment: NotificationEnvironment,
    now: datetime,
    *,
    can_notify: Callable[[], bool] | None = None,
) -> list[AlertOutcome]:
    """Dispatch detected first sessions only within their active watch period."""
    dispatcher = None
    outcomes = []
    retention = max(
        30 * 24 * 60 * 60,
        math.ceil(max(rule.active_until.timestamp() for rule in config.watches) - now.timestamp())
        + 7 * 24 * 60 * 60,
    )
    for showtime in showtimes:
        key = showtime_key(showtime)
        reason = block_reason(showtime, now)
        deadlines = [
            rule.active_until
            for rule in config.watches
            if rule.enabled
            and rule.active_from <= now < rule.active_until
            and select_first_showtimes([showtime], rule)
        ]
        if reason is not None or not deadlines:
            outcomes.append(AlertOutcome(key, "skipped", reason or "활성 감시 규칙이 없습니다."))
            continue
        if can_notify is not None and not can_notify():
            outcomes.append(
                AlertOutcome(key, "deferred", "실행 시간이 부족하여 전송을 보류합니다.")
            )
            continue
        if dispatcher is None:
            dispatcher = create_dispatcher(
                environment, client, config.http.timeout_seconds, retention
            )
        outcomes.append(dispatcher.dispatch(showtime, active_until=max(deadlines)))
    return outcomes
