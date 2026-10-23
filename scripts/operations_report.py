"""Read operational evidence without invoking Lambda or changing notification state."""

import argparse
import ast
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import mean
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.paginate import Paginator

FUNCTION = "movie-alert-checker"
LOG_GROUP = f"/aws/lambda/{FUNCTION}"
SDK_CONFIG = Config(
    connect_timeout=5, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 2}
)


def log_summary(client: Any, start: datetime, end: datetime) -> dict[str, Any]:
    durations = []
    memory = []
    outcomes: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    report_statuses: Counter[str] = Counter()
    reports = checks = query_errors = malformed = events = 0
    billed_gb_seconds = Decimal(0)
    first = last = last_check = None
    for page in client.get_paginator("filter_log_events").paginate(
        logGroupName=LOG_GROUP,
        startTime=int(start.timestamp() * 1000),
        endTime=int(end.timestamp() * 1000),
    ):
        for event in page["events"]:
            events += 1
            timestamp = datetime.fromtimestamp(event["timestamp"] / 1000, UTC).isoformat()
            first = first or timestamp
            last = timestamp
            message = event["message"]
            if message.startswith("REPORT RequestId:"):
                reports += 1
                duration = re.search(r"\tDuration: ([\d.]+) ms", message)
                used = re.search(r"Max Memory Used: (\d+) MB", message)
                billed = re.search(r"Billed Duration: (\d+) ms", message)
                allocated = re.search(r"\tMemory Size: (\d+) MB", message)
                status = re.search(r"\tStatus: (\S+)", message)
                if duration and used and billed and allocated:
                    durations.append(float(duration[1]))
                    memory.append(int(used[1]))
                    billed_gb_seconds += Decimal(billed[1]) / 1000 * Decimal(allocated[1]) / 1024
                else:
                    malformed += 1
                if status:
                    report_statuses[status[1]] += 1
            if "실행 실패: request_id=" in message:
                error_type = re.search(r" type=(\w+)", message)
                failures[error_type[1] if error_type else "unknown"] += 1
            if "확인 완료: request_id=" in message:
                try:
                    summary = ast.literal_eval(message.split("summary=", 1)[1])
                    query_errors += summary["errors"]
                    outcomes.update(summary["outcomes"])
                except (ValueError, SyntaxError, KeyError, TypeError, IndexError):
                    malformed += 1
                    continue
                checks += 1
                last_check = timestamp
    return {
        "status": "ok" if events and not malformed else "no_data" if not events else "partial",
        "all_pages_read": True,
        "first_event_at": first,
        "last_event_at": last,
        "last_check_at": last_check,
        "completed_invocations": reports,
        "completed_checks": checks,
        "query_errors": query_errors,
        "handler_failures_by_type": dict(failures),
        "report_statuses": dict(report_statuses),
        "notification_outcomes": dict(outcomes),
        "duration_average_ms": round(mean(durations), 3) if durations else None,
        "duration_max_ms": max(durations, default=None),
        "max_memory_used_mb": max(memory, default=None),
        "billed_gb_seconds": str(billed_gb_seconds),
        "unparsed_records": malformed,
    }


def lambda_metrics(client: Any, start: datetime, end: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok"}
    for name in ("Invocations", "Errors", "Throttles"):
        response = client.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName=name,
            Dimensions=[{"Name": "FunctionName", "Value": FUNCTION}],
            StartTime=start,
            EndTime=end,
            Period=300,
            Statistics=["Sum"],
        )
        points = response["Datapoints"]
        # Missing telemetry is not proof of zero invocations or failures.
        result[name] = sum(point["Sum"] for point in points) if points else None
    if any(result[name] is None for name in ("Invocations", "Errors", "Throttles")):
        result["status"] = "partial"
    return result


def daily_costs(client: Any, now: datetime) -> dict[str, Any]:
    # Cost Explorer is delayed; omit the incomplete current UTC day.
    end = now.astimezone(UTC).date()
    start = end - timedelta(days=7)
    days: dict[str, dict[str, Any]] = {}
    # The SDK has no built-in paginator for this operation, despite its NextPageToken.
    paginator = Paginator(
        client.get_cost_and_usage,
        {
            "input_token": "NextPageToken",
            "output_token": "NextPageToken",
            "result_key": "ResultsByTime",
        },
        client.meta.service_model.operation_model("GetCostAndUsage"),
    )
    for page in paginator.paginate(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    ):
        for day in page["ResultsByTime"]:
            date = day["TimePeriod"]["Start"]
            record = days.setdefault(
                date, {"date": date, "estimated": day["Estimated"], "services": []}
            )
            record["estimated"] |= day["Estimated"]
            record["services"].extend(
                {"service": group["Keys"][0], **group["Metrics"]["UnblendedCost"]}
                for group in day["Groups"]
            )
    return {
        "status": "ok" if days else "no_data",
        "scope": "account_by_service_not_project",
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "days": list(days.values()),
    }


def read_section(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except ClientError as error:
        return {"status": "unavailable", "error_code": error.response["Error"]["Code"]}
    except BotoCoreError as error:
        return {"status": "unavailable", "error_code": type(error).__name__}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--hours", type=int, choices=range(1, 73), default=24, metavar="1..72")
    window.add_argument("--since", help="Timezone-aware ISO timestamp within the last 72 hours")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument(
        "--metrics", action="store_true", help="Requires CloudWatch read permission"
    )
    parser.add_argument(
        "--cost", action="store_true", help="Cost Explorer: charged per page; account-wide costs"
    )
    args = parser.parse_args()
    now = datetime.now(UTC)
    start = now - timedelta(hours=args.hours)
    if args.since:
        try:
            start = datetime.fromisoformat(args.since)
            if start.utcoffset() is None or not now - timedelta(hours=72) <= start < now:
                raise ValueError
        except ValueError:
            parser.error("--since에는 최근 72시간 이내의 시간대가 있는 ISO 시각을 지정하세요.")
    session = boto3.Session(region_name=args.region)
    report = {
        "region": args.region,
        "function": FUNCTION,
        "start": start.isoformat(),
        "end_exclusive": now.isoformat(),
        "logs": read_section(
            lambda: log_summary(session.client("logs", config=SDK_CONFIG), start, now)
        ),
    }
    if args.metrics:
        report["metrics"] = read_section(
            lambda: lambda_metrics(session.client("cloudwatch", config=SDK_CONFIG), start, now)
        )
    if args.cost:
        report["cost"] = read_section(
            lambda: daily_costs(
                session.client("ce", region_name="us-east-1", config=SDK_CONFIG), now
            )
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Exit status reports collection completeness; inspect failures/outcomes for service health.
    if any(
        isinstance(section, dict) and section.get("status") != "ok" for section in report.values()
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
