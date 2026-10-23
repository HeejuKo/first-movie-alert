"""Invoke the deployed checker and show only safe configuration and result fields."""

import argparse
import base64
import json
import time

import boto3
from botocore.config import Config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--checks", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Read schedule and recent execution logs without invoking",
    )
    args = parser.parse_args()
    session = boto3.Session(region_name="ap-northeast-2")
    if args.scheduled:
        scheduler = session.client(
            "scheduler",
            config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}),
        )
        schedule = scheduler.get_schedule(Name="movie-alert-checker", GroupName="movie-alert")
        print(
            json.dumps({"schedule_state": schedule["State"], "end_date": str(schedule["EndDate"])})
        )
        logs = session.client(
            "logs",
            config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}),
        )
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(
            logGroupName="/aws/lambda/movie-alert-checker",
            startTime=int((time.time() - 300) * 1000),
        ):
            for event in page["events"]:
                message = event["message"]
                if (
                    message.startswith("REPORT RequestId:")
                    or "확인 완료: request_id=" in message
                    or "실행 실패: request_id=" in message
                ):
                    print(message.strip())
        return
    client = session.client(
        "lambda",
        config=Config(connect_timeout=5, read_timeout=190, retries={"total_max_attempts": 1}),
    )
    config = client.get_function_configuration(FunctionName="movie-alert-checker")
    print(
        json.dumps(
            {
                key: config.get(key)
                for key in (
                    "FunctionName",
                    "State",
                    "LastUpdateStatus",
                    "Runtime",
                    "Architectures",
                    "MemorySize",
                    "Timeout",
                )
            }
        )
    )
    response = client.invoke(
        FunctionName="movie-alert-checker",
        InvocationType="RequestResponse",
        LogType="Tail",
        Payload=json.dumps({"notify": args.notify, "checks": args.checks}).encode(),
    )
    payload = json.load(response["Payload"])
    if response.get("FunctionError"):
        # Runtime may include a traceback. Do not echo it or the original SDK response.
        print(json.dumps({"status": "error", "type": payload.get("errorType")}))
        raise SystemExit(1)
    print(json.dumps(payload, ensure_ascii=False))
    for line in (
        base64.b64decode(response.get("LogResult", "")).decode(errors="replace").splitlines()
    ):
        if line.startswith("REPORT RequestId:"):
            print(line)


if __name__ == "__main__":
    main()
