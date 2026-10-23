import importlib.util
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import boto3
from botocore.exceptions import ClientError
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location(
    "operations_report", Path(__file__).parents[1] / "scripts/operations_report.py"
)
operations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operations)
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def test_log_pagination_counts_failures_and_billing_without_echoing_raw_data():
    report = (
        "REPORT RequestId: synthetic\tDuration: 1000.00 ms\tBilled Duration: 1100 ms"
        "\tMemory Size: 256 MB\tMax Memory Used: 150 MB\n"
    )
    summary = (
        "[INFO]\t확인 완료: request_id=test summary="
        "{'check': 1, 'errors': 0, 'outcomes': {'sent': 1}, 'elapsed_seconds': 1.0}"
    )
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [
        {"events": [{"timestamp": 1000, "message": summary}]},
        {"events": [{"timestamp": 2000, "message": report}]},
        {
            "events": [
                {"timestamp": 3000, "message": "실행 실패: request_id=x type=StateError"},
                {"timestamp": 4000, "message": "ignored fake secret must not be printed"},
            ]
        },
    ]
    result = operations.log_summary(client, NOW - timedelta(hours=1), NOW)
    assert result["completed_invocations"] == result["completed_checks"] == 1
    assert result["handler_failures_by_type"] == {"StateError": 1}
    assert result["notification_outcomes"] == {"sent": 1}
    assert result["billed_gb_seconds"] == "0.275"
    assert result["max_memory_used_mb"] == 150
    assert "secret" not in str(result)


def test_malformed_log_and_no_data_are_not_reported_as_healthy():
    client = Mock()
    client.get_paginator.return_value.paginate.return_value = [{"events": []}]
    assert operations.log_summary(client, NOW, NOW)["status"] == "no_data"
    client.get_paginator.return_value.paginate.return_value = [
        {"events": [{"timestamp": 1000, "message": "확인 완료: request_id=x summary=broken"}]}
    ]
    assert operations.log_summary(client, NOW, NOW)["status"] == "partial"


def test_metrics_distinguish_zero_from_missing_data():
    client = Mock()
    client.get_metric_statistics.side_effect = [
        {"Datapoints": [{"Sum": 5}, {"Sum": 3}]},
        {"Datapoints": [{"Sum": 0}]},
        {"Datapoints": []},
    ]
    result = operations.lambda_metrics(client, NOW - timedelta(hours=1), NOW)
    assert result == {"status": "partial", "Invocations": 8, "Errors": 0, "Throttles": None}


def test_cost_query_uses_full_utc_days_and_all_pages():
    client = boto3.client(
        "ce",
        region_name="us-east-1",
        aws_access_key_id="synthetic-access-key",
        aws_secret_access_key="synthetic-secret-key",
    )
    parameters = {
        "TimePeriod": {"Start": "2026-10-16", "End": "2026-10-23"},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
    }
    day = {
        "TimePeriod": {"Start": "2026-10-22", "End": "2026-10-23"},
        "Estimated": True,
        "Groups": [],
    }
    with closing(client), Stubber(client) as stub:
        stub.add_response(
            "get_cost_and_usage", {"ResultsByTime": [day], "NextPageToken": "next"}, parameters
        )
        stub.add_response(
            "get_cost_and_usage",
            {"ResultsByTime": [day]},
            {**parameters, "NextPageToken": "next"},
        )
        result = operations.daily_costs(client, NOW)
        stub.assert_no_pending_responses()
    assert len(result["days"]) == 1
    assert result["end_exclusive"] == "2026-10-23"
    assert result["scope"] == "account_by_service_not_project"


def test_permission_error_is_explicit_and_does_not_expose_sdk_message():
    operation = Mock(
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "private response"}}, "test"
        )
    )
    assert operations.read_section(operation) == {
        "status": "unavailable",
        "error_code": "AccessDenied",
    }
