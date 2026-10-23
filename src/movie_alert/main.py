"""One-shot CLI with notification preview and explicit live delivery."""

import argparse
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import httpx

from movie_alert.checker import select_first_showtimes
from movie_alert.config import AppConfig, ConfigError, WatchRule, load_config
from movie_alert.models import SEOUL, Showtime
from movie_alert.notifier import NotificationError
from movie_alert.pipeline import (
    AlertOutcome,
    block_reason,
    current_time,
    notification_preview,
    showtime_key,
)
from movie_alert.providers.base import (
    AccessDeniedError,
    Provider,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)
from movie_alert.providers.cgv import CgvProvider, parse_schedule
from movie_alert.providers.cgv_relay import CgvRelayProvider
from movie_alert.runtime import (
    NotificationEnvironment,
    create_dispatcher,
    notification_environment,
)

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
    config: AppConfig, provider: Provider, now: datetime
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


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("지원하지 않는 JSON 값입니다.")


def deliver_notifications(
    config: AppConfig,
    showtimes: list[Showtime],
    client: httpx.Client,
    environment: NotificationEnvironment,
    now: datetime,
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
        if dispatcher is None:
            dispatcher = create_dispatcher(
                environment, client, config.http.timeout_seconds, retention
            )
        outcomes.append(dispatcher.dispatch(showtime, active_until=max(deadlines)))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Movie Alert 첫 회차 조회·알림 미리보기")
    parser.add_argument("--config", type=Path, required=True, help="TOML 감시 설정 파일")
    parser.add_argument("--response", type=Path, help="저장된 CGV 응답 재생; 네트워크 요청 없음")
    parser.add_argument(
        "--notify", action="store_true", help="실제 조회에서 발견한 첫 회차를 Discord 전송"
    )
    args = parser.parse_args(argv)
    if args.response and args.notify:
        parser.error("저장 응답은 현재 시간표를 보장하지 않아 --notify와 함께 사용할 수 없습니다.")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
        environment = notification_environment() if args.notify else None
    except ConfigError as error:
        logger.error("%s", error)
        return 2
    outcomes: list[AlertOutcome] = []
    if args.response:
        try:
            payload = json.loads(args.response.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.error("응답 파일을 읽을 수 없거나 JSON 형식이 잘못되었습니다.")
            return 1
        try:
            rows = parse_schedule(payload)
        except ProviderError as error:
            logger.error("%s", error)
            return 1
        # Offline replay is intentionally independent of the wall clock and watch period.
        selected = [row for rule in config.watches for row in select_first_showtimes(rows, rule)]
        errors = []
    else:
        with httpx.Client() as client:
            selected, errors = run_live(config, create_provider(config, client), current_time())
            if environment is not None and not errors:
                try:
                    outcomes = deliver_notifications(
                        config, selected, client, environment, current_time()
                    )
                except (ConfigError, NotificationError) as error:
                    logger.error("%s", error)
                    return 2
    previews = [notification_preview(row, current_time()) for row in selected]
    print(
        json.dumps(
            {
                "mode": "offline" if args.response else "live",
                "first_showtimes": [
                    {
                        key: value
                        for key, value in asdict(row).items()
                        if key not in {"screen_id", "screen_name"}
                    }
                    for row in selected
                ],
                "errors": errors,
                "notifications": {
                    "mode": "live" if args.notify else "preview",
                    "previews": previews,
                    "outcomes": [asdict(outcome) for outcome in outcomes],
                },
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )
    delivery_errors = any(
        outcome.status in {"deferred", "uncertain", "failed", "state_error"} for outcome in outcomes
    )
    return 1 if errors or delivery_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
