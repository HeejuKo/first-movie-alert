from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from movie_alert import lambda_handler as entry
from movie_alert.main import deliver_notifications, run_live
from movie_alert.models import SEOUL
from movie_alert.pipeline import AlertOutcome

NOW = datetime(2026, 10, 23, 12, tzinfo=SEOUL)


@pytest.fixture(autouse=True)
def polling_lock(monkeypatch):
    lock = Mock()
    lock.acquire.return_value = True
    monkeypatch.setattr(entry, "create_poll_lock", lambda: lock)
    return lock


@pytest.fixture
def clock(monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(entry.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        entry.time, "sleep", lambda delay: elapsed.__setitem__(0, elapsed[0] + delay)
    )
    monkeypatch.setattr(entry, "current_time", lambda: NOW + timedelta(seconds=elapsed[0]))
    return elapsed


@pytest.fixture
def context(clock):
    return SimpleNamespace(
        aws_request_id="test-request",
        get_remaining_time_in_millis=lambda: int((180 - clock[0]) * 1000),
    )


@pytest.fixture
def runtime(monkeypatch, config):
    monkeypatch.setattr(entry, "load_config", lambda path: config)
    monkeypatch.setattr(entry, "create_provider", Mock())
    query = Mock(return_value=([], []))
    send = Mock(return_value=[])
    environment = Mock()
    monkeypatch.setattr(entry, "run_live", query)
    monkeypatch.setattr(entry, "deliver_notifications", send)
    monkeypatch.setattr(entry, "notification_environment", environment)
    return query, send, environment


def test_default_checks_once_without_waiting(context, clock, runtime):
    result = entry.handler({}, context)
    assert len(result["checks"]) == 1
    runtime[0].assert_called_once()
    assert clock[0] == 0


@pytest.mark.parametrize("fails", [True, False])
def test_real_notification_round_records_health_without_changing_query_result(
    context, runtime, config, monkeypatch, fails
):
    from movie_alert.config import OperationsSettings

    monkeypatch.setattr(
        entry,
        "load_config",
        lambda path: replace(
            config, operations=OperationsSettings(enabled=True, failure_threshold=3)
        ),
    )
    monitor = Mock()
    monitor.record.return_value = None
    monkeypatch.setattr(entry, "create_health_monitor", Mock(return_value=monitor))
    if fails:
        runtime[0].return_value = ([], ["network timeout"])
        with pytest.raises(RuntimeError):
            entry.handler({"notify": True}, context)
    else:
        assert entry.handler({"notify": True}, context)["status"] == "ok"
    assert monitor.record.call_args.kwargs["success"] is not fails


def test_disabled_operational_alerts_do_not_create_monitor(context, runtime, monkeypatch):
    monitor = Mock()
    monkeypatch.setattr(entry, "create_health_monitor", monitor)
    entry.handler({"notify": True}, context)
    monitor.assert_not_called()


def test_two_checks_start_thirty_seconds_apart(context, clock, runtime):
    query, send, environment = runtime
    starts = []

    def fetch(*args, **kwargs):
        starts.append(clock[0])
        clock[0] += 7
        return [], []

    query.side_effect = fetch
    result = entry.handler({"checks": 2}, context)
    assert starts == [0, 30]
    assert result["status"] == "ok"
    assert len(result["checks"]) == 2
    send.assert_not_called()
    environment.assert_not_called()


def test_explicit_notification_uses_same_delivery_path(context, runtime):
    query, send, environment = runtime
    query.return_value = (["synthetic-showtime"], [])
    send.return_value = [SimpleNamespace(status="not_claimed")]
    result = entry.handler({"notify": True, "checks": 1}, context)
    assert result["checks"][0]["outcomes"] == {"not_claimed": 1}
    environment.assert_called_once()
    assert send.call_args.args[1] == ["synthetic-showtime"]


def test_slow_first_check_skips_second_when_budget_is_short(context, clock, runtime):
    query, _, _ = runtime

    def fetch(*args, **kwargs):
        clock[0] += 95
        return [], []

    query.side_effect = fetch
    assert len(entry.handler({"checks": 2}, context)["checks"]) == 1
    assert query.call_count == 1


def test_period_ends_before_second_check(context, runtime, config, monkeypatch):
    rules = tuple(
        replace(rule, active_until=NOW + timedelta(seconds=20)) for rule in config.watches
    )
    monkeypatch.setattr(entry, "load_config", lambda path: replace(config, watches=rules))
    result = entry.handler({"checks": 2}, context)
    assert len(result["checks"]) == 1
    assert runtime[0].call_count == 1


def test_expired_watch_does_no_io(context, runtime, monkeypatch):
    monkeypatch.setattr(entry, "current_time", lambda: NOW + timedelta(days=40))
    assert entry.handler({"notify": True}, context)["reason"] == "inactive"
    for mock in runtime:
        mock.assert_not_called()


def test_delayed_scheduler_call_does_no_io(context, runtime):
    event = {"notify": True, "scheduled_at": (NOW - timedelta(seconds=91)).isoformat()}
    assert entry.handler(event, context)["reason"] == "stale_schedule"
    for mock in runtime:
        mock.assert_not_called()


@pytest.mark.parametrize("scheduled", ["<aws.scheduler.scheduled-time>", 123, "2026-10-23"])
def test_invalid_scheduler_time_fails_before_io(context, runtime, scheduled):
    with pytest.raises(RuntimeError):
        entry.handler({"scheduled_at": scheduled}, context)
    for mock in runtime:
        mock.assert_not_called()


def test_provider_failure_is_lambda_failure_without_delivery(context, runtime):
    runtime[0].return_value = (["partial"], ["provider-error"])
    with pytest.raises(RuntimeError, match="Movie Alert 실행 실패"):
        entry.handler({"notify": True}, context)
    runtime[1].assert_not_called()
    assert runtime[0].call_count == 1


@pytest.mark.parametrize("status", ["deferred", "uncertain", "failed", "state_error"])
def test_delivery_failure_is_not_reported_as_success(context, runtime, status, caplog):
    key = "showtime:v1:" + "a" * 64
    runtime[1].return_value = [AlertOutcome(key, status, "synthetic failure")]
    with pytest.raises(RuntimeError):
        entry.handler({"notify": True}, context)
    assert key in caplog.text


def test_unexpected_errors_do_not_expose_secrets(context, runtime, caplog):
    runtime[0].side_effect = ValueError("synthetic-webhook-secret")
    with pytest.raises(RuntimeError) as error:
        entry.handler({}, context)
    assert "synthetic-webhook-secret" not in str(error.value)
    assert "synthetic-webhook-secret" not in caplog.text


def test_no_query_starts_after_budget_is_exhausted(config):
    provider = Mock()
    selected, errors = run_live(config, provider, NOW, can_query=lambda: False)
    assert selected == []
    assert errors
    provider.fetch_showtimes.assert_not_called()


def test_budget_exhaustion_stops_remaining_branch_batches(config):
    provider = Mock()
    provider.fetch_showtimes.return_value = []
    budget = iter([True, False])
    selected, errors = run_live(config, provider, NOW, can_query=lambda: next(budget))
    assert selected == []
    assert errors
    assert provider.fetch_showtimes.call_count == config.http.max_parallel_requests


@pytest.mark.parametrize("event", [{"checks": 3}, {"checks": True}, {"notify": "true"}, []])
def test_invalid_event_does_no_io(context, runtime, event):
    with pytest.raises(RuntimeError):
        entry.handler(event, context)
    for mock in runtime:
        mock.assert_not_called()


def test_first_query_requires_enough_remaining_time(context, runtime, clock):
    clock[0] = 100
    with pytest.raises(RuntimeError):
        entry.handler({}, context)
    runtime[0].assert_not_called()


def test_concurrent_invocation_does_not_query_or_release_another_owner(
    context, runtime, polling_lock
):
    polling_lock.acquire.return_value = False
    result = entry.handler({"notify": True}, context)
    assert result["reason"] == "already_running"
    runtime[0].assert_not_called()
    runtime[1].assert_not_called()
    polling_lock.release.assert_not_called()


def test_polling_lease_covers_remaining_invocation_lifetime(context, runtime, polling_lock):
    entry.handler({"checks": 1}, context)
    polling_lock.acquire.assert_called_once_with("test-request", int(NOW.timestamp()), 240)
    polling_lock.release.assert_called_once_with("test-request")


def test_failure_still_releases_the_polling_lease(context, runtime, polling_lock):
    runtime[0].side_effect = ValueError("synthetic-private-value")
    with pytest.raises(RuntimeError):
        entry.handler({}, context)
    polling_lock.release.assert_called_once_with("test-request")


def test_polling_lock_failure_prevents_query(context, runtime, polling_lock):
    polling_lock.acquire.side_effect = RuntimeError("private-storage-error")
    with pytest.raises(RuntimeError):
        entry.handler({}, context)
    runtime[0].assert_not_called()
    polling_lock.release.assert_not_called()


def test_no_delivery_starts_after_budget_is_exhausted(config, showtimes):
    outcomes = deliver_notifications(
        config, showtimes[:1], Mock(), Mock(), NOW, can_notify=lambda: False
    )
    assert len(outcomes) == 1
    assert outcomes[0].status == "deferred"
