"""One-shot CLI with notification preview and explicit live delivery."""

import argparse
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import httpx

from movie_alert.application import create_provider, deliver_notifications, run_live
from movie_alert.checker import select_first_showtimes
from movie_alert.config import ConfigError, load_config
from movie_alert.notifier import NotificationError
from movie_alert.outcomes import AlertOutcome
from movie_alert.pipeline import current_time, notification_preview
from movie_alert.providers.base import ProviderError
from movie_alert.providers.cgv import parse_schedule
from movie_alert.runtime import notification_environment

logger = logging.getLogger(__name__)


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError("지원하지 않는 JSON 값입니다.")


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
    return 1 if errors or any(outcome.is_failure for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
