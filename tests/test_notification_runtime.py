import importlib
import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import NoRegionError

from movie_alert.config import ConfigError
from movie_alert.models import SEOUL, BookingStatus
from movie_alert.notifier import DiscordNotifier
from movie_alert.pipeline import AlertDispatcher, AlertOutcome
from movie_alert.runtime import (
    NotificationEnvironment,
    create_dispatcher,
    notification_environment,
)
from movie_alert.state import StateStore

cli = importlib.import_module("movie_alert.main")
runtime = importlib.import_module("movie_alert.runtime")
NOW = datetime(2026, 10, 21, tzinfo=SEOUL)
ENVIRONMENT = NotificationEnvironment(
    "https://discord.com/api/webhooks/123/fake-test-token", "movie-alert-test", "ap-northeast-2"
)


def test_webhook_is_not_in_environment_repr():
    assert "fake-test-token" not in repr(ENVIRONMENT)


def test_missing_environment_is_clear_and_does_not_read_aws_credentials(monkeypatch):
    for key in (
        "DISCORD_WEBHOOK_URL",
        "MOVIE_ALERT_DYNAMODB_TABLE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(key, raising=False)
    client = Mock()
    monkeypatch.setattr(runtime.boto3, "client", client)
    with pytest.raises(ConfigError, match="DISCORD_WEBHOOK_URL"):
        notification_environment()
    client.assert_not_called()


def test_runtime_has_bounded_sdk_settings_without_connecting(monkeypatch):
    client_factory = Mock(return_value=Mock())
    monkeypatch.setattr(runtime.boto3, "client", client_factory)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))
    ) as client:
        dispatcher = create_dispatcher(ENVIRONMENT, client, 10, 30 * 86400)
    options = client_factory.call_args.kwargs["config"]
    assert options.connect_timeout == options.read_timeout == 5
    assert options.retries["total_max_attempts"] == 2
    assert dispatcher.lease_seconds >= 120


def test_sdk_setup_error_is_sanitized(monkeypatch):
    monkeypatch.setattr(runtime.boto3, "client", Mock(side_effect=NoRegionError()))
    with httpx.Client() as client, pytest.raises(ConfigError, match="AWS"):
        create_dispatcher(ENVIRONMENT, client, 10, 30 * 86400)


def test_started_showtimes_do_not_initialize_aws_or_discord(config, showtimes, monkeypatch):
    factory = Mock(side_effect=AssertionError("must not create notification dependencies"))
    monkeypatch.setattr(cli, "create_dispatcher", factory)
    with httpx.Client() as client:
        outcomes = cli.deliver_notifications(
            config, [showtimes[0]], client, ENVIRONMENT, showtimes[0].starts_at
        )
    assert len(outcomes) == 1
    assert outcomes[0].status == "skipped"
    factory.assert_not_called()


def test_unknown_capture_sends_once_and_status_change_does_not_resend(
    config, showtimes, monkeypatch
):
    requests = []
    state = Mock(spec=StateStore)
    state.claim.side_effect = [True, False]

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "123"})

    def factory(environment, client, timeout_seconds, retention_seconds):
        return AlertDispatcher(
            state,
            DiscordNotifier(client, environment.webhook_url, timeout_seconds),
            clock=lambda: NOW,
            retention_seconds=retention_seconds,
        )

    monkeypatch.setattr(cli, "create_dispatcher", factory)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = cli.deliver_notifications(config, showtimes[:1], client, ENVIRONMENT, NOW)
        changed = replace(showtimes[0], booking_status=BookingStatus.AVAILABLE)
        second = cli.deliver_notifications(config, [changed], client, ENVIRONMENT, NOW)
    assert first[0].status == "sent"
    assert second[0].status == "not_claimed"
    assert state.claim.call_count == 2
    state.mark_sent.assert_called_once()
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["allowed_mentions"] == {"parse": []}
    assert "상영시간표 발견" in payload["content"]
    assert "예매 가능 여부: 미확인" in payload["content"]
    assert "예매 오픈 감지" not in payload["content"]
    assert showtimes[0].booking_status is BookingStatus.UNKNOWN


def test_expired_watch_does_not_initialize_delivery(config, showtimes, monkeypatch):
    available = replace(showtimes[0], booking_status=BookingStatus.AVAILABLE)
    expired = replace(config.watches[0], active_until=NOW)
    factory = Mock()
    monkeypatch.setattr(cli, "create_dispatcher", factory)
    with httpx.Client() as client:
        result = cli.deliver_notifications(
            replace(config, watches=(expired,)), [available], client, ENVIRONMENT, NOW
        )
    assert result[0].status == "skipped"
    factory.assert_not_called()


@pytest.mark.parametrize("status", list(BookingStatus))
def test_eligible_live_result_is_dispatched_with_its_watch_deadline(
    config, showtimes, monkeypatch, status
):
    available = replace(showtimes[0], booking_status=status)
    dispatcher = Mock()
    dispatcher.dispatch.return_value = AlertOutcome("test-key", "sent", "synthetic success", "123")
    factory = Mock(return_value=dispatcher)
    monkeypatch.setattr(cli, "create_dispatcher", factory)
    with httpx.Client() as client:
        result = cli.deliver_notifications(config, [available], client, ENVIRONMENT, NOW)
    assert result[0].status == "sent"
    dispatcher.dispatch.assert_called_once_with(
        available, active_until=config.watches[0].active_until
    )


def test_saved_response_cannot_trigger_real_notifications():
    with pytest.raises(SystemExit) as error:
        cli.main(["--config", "unused", "--response", "unused", "--notify"])
    assert error.value.code == 2


def test_cli_defaults_to_preview_even_for_synthetic_available_results(
    config, showtimes, monkeypatch, capsys
):
    available = replace(showtimes[0], booking_status=BookingStatus.AVAILABLE)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "run_live", lambda *args: ([available], []))
    monkeypatch.setattr(cli, "current_time", lambda: NOW)
    factory = Mock()
    monkeypatch.setattr(cli, "create_dispatcher", factory)
    assert cli.main(["--config", "unused"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["notifications"]["mode"] == "preview"
    assert output["notifications"]["outcomes"] == []
    assert output["notifications"]["previews"][0]["eligible"] is True
    factory.assert_not_called()


def test_query_errors_prevent_real_delivery(config, showtimes, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "notification_environment", lambda: ENVIRONMENT)
    monkeypatch.setattr(cli, "run_live", lambda *args: (showtimes[:1], ["synthetic query error"]))
    delivery = Mock()
    monkeypatch.setattr(cli, "deliver_notifications", delivery)
    assert cli.main(["--config", "unused", "--notify"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"] == ["synthetic query error"]
    delivery.assert_not_called()
