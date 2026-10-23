"""Single-key notification recovery; mutations default to dry-run."""

import argparse
import json
from contextlib import closing

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from movie_alert.pipeline import current_time
from movie_alert.recovery import NotificationRecovery
from movie_alert.state import StateError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "retry", "mark-sent"])
    parser.add_argument("--key", required=True)
    parser.add_argument("--table", default="movie-alert-state")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--operator", default="")
    parser.add_argument("--message-id")
    parser.add_argument("--confirm-not-delivered", action="store_true")
    parser.add_argument("--accept-duplicate-risk", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    if args.action == "inspect" and args.apply:
        parser.error("inspect는 조회 전용입니다.")
    if args.apply and not args.expected_revision:
        parser.error("--apply에는 직전 inspect의 --expected-revision이 필요합니다.")
    try:
        with closing(
            boto3.Session(region_name=args.region).client(
                "dynamodb",
                config=Config(
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"mode": "standard", "total_max_attempts": 1},
                ),
            )
        ) as client:
            recovery = NotificationRecovery(client, args.table)
            snapshot = recovery.inspect(args.key)
            if args.expected_revision and args.expected_revision != snapshot.revision:
                raise ValueError("상태 revision이 변경됐습니다. 다시 inspect하세요.")
            if args.action == "inspect":
                print(json.dumps(snapshot.view(), ensure_ascii=False))
                return
            now = int(current_time().timestamp())
            changes = recovery.prepare(
                snapshot,
                action=args.action,
                now_epoch=now,
                operator=args.operator,
                confirm_not_delivered=args.confirm_not_delivered,
                accept_duplicate_risk=args.accept_duplicate_risk,
                message_id=args.message_id,
            )
            if args.apply:
                recovery.apply(snapshot, changes, now_epoch=int(current_time().timestamp()))
            print(
                json.dumps(
                    {
                        "mode": "applied" if args.apply else "dry-run",
                        "current": snapshot.view(),
                        "action": args.action,
                        "next_status": changes["status"]["S"],
                        "sends_discord": False,
                    },
                    ensure_ascii=False,
                )
            )
    except (BotoCoreError, ClientError, StateError, ValueError) as error:
        if isinstance(error, (StateError, ValueError)):
            parser.exit(1, str(error) + "\n")
        parser.exit(1, "AWS 상태 확인·변경에 실패했습니다. 인증과 복구 권한을 확인하세요.\n")


if __name__ == "__main__":
    main()
