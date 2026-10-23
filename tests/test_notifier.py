import json
import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from movie_alert.models import BookingStatus
from movie_alert.notifier import (
    DiscordNotifier,
    NotificationConnectionError,
    NotificationDeliveryUnknown,
    NotificationRateLimited,
    NotificationRejected,
    notification_content,
)

# Synthetic secret and Discord responses; no live webhook is used.
WEBHOOK = "https://discord.com/api/webhooks/123456/synthetic-secret-token"


@pytest.fixture
def available_showtime(showtimes):
    return replace(showtimes[0], booking_status=BookingStatus.AVAILABLE)


def test_send_confirms_message_and_disables_mentions(available_showtime):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "654321"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        receipt = DiscordNotifier(client, WEBHOOK, timeout_seconds=7).send(available_showtime)

    assert receipt.message_id == "654321"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.params["wait"] == "true"
    assert request.extensions["timeout"] == {"connect": 5, "read": 7, "write": 7, "pool": 7}
    payload = json.loads(request.content)
    assert payload["allowed_mentions"] == {"parse": []}
    assert "10/24(토) 상영시간표 발견" in payload["content"]
    assert "1회차: 10:00" in payload["content"]
    assert "강남" in payload["content"]
    assert "상영관:" not in payload["content"]
    assert available_showtime.screen_name not in payload["content"]
    assert len(payload["content"]) <= 2000


def test_message_converts_timezone_and_preserves_business_date(available_showtime):
    showtime = replace(
        available_showtime,
        starts_at=datetime(2026, 10, 24, 16, 10, tzinfo=UTC),
    )
    content = notification_content(showtime)
    assert "10/25(일) 상영시간표 발견" in content
    assert "1회차: 01:10" in content
    assert "상영 영업일: 2026-10-24" in content


@pytest.mark.parametrize(
    ("status", "label"),
    [(BookingStatus.UNKNOWN, "미확인"), (BookingStatus.UNAVAILABLE, "불가")],
)
def test_schedule_notification_sends_without_confirmed_availability(
    available_showtime, status, label
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "654321"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = DiscordNotifier(client, WEBHOOK)
        receipt = notifier.send(replace(available_showtime, booking_status=status))
    assert receipt.message_id == "654321"
    assert len(requests) == 1
    content = json.loads(requests[0].content)["content"]
    assert "상영시간표 발견" in content
    assert f"예매 가능 여부: {label}" in content
    assert "예매 오픈 감지" not in content


def test_naive_showtime_is_rejected(available_showtime):
    with pytest.raises(NotificationRejected, match="시간대"):
        notification_content(
            replace(available_showtime, starts_at=available_showtime.starts_at.replace(tzinfo=None))
        )


def test_long_names_and_newlines_stay_within_discord_limit(available_showtime):
    content = notification_content(
        replace(
            available_showtime,
            movie_name="@everyone\n영화" * 2000,
            branch_name="지점" * 2000,
        )
    )
    assert len(content) <= 2000
    assert "@everyone\n영화" not in content


@pytest.mark.parametrize(
    "url",
    [
        "http://discord.com/api/webhooks/123456/synthetic-secret-token",
        "https://example.com/api/webhooks/123456/synthetic-secret-token",
        "https://discord.com.evil.test/api/webhooks/123456/synthetic-secret-token",
        "https://discord.com/api/webhooks/123456/synthetic-secret-token?wait=false",
        "https://discord.com/api/webhooks/123456/synthetic-secret-token#fragment",
        "https://user:pass@discord.com/api/webhooks/123456/synthetic-secret-token",
        "https://discord.com:8443/api/webhooks/123456/synthetic-secret-token",
        "https://discord.com/api/webhooks/123456",
        "https://discord.com/channels/123456",
        "https://[invalid",
    ],
)
def test_invalid_destination_is_rejected_without_secret(url):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        with pytest.raises(NotificationRejected) as error:
            DiscordNotifier(client, url)
    assert "synthetic-secret-token" not in str(error.value)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected(timeout):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        with pytest.raises(NotificationRejected, match="제한 시간"):
            DiscordNotifier(client, WEBHOOK, timeout_seconds=timeout)


@pytest.mark.parametrize("status_code", [301, 400, 401, 403, 404])
def test_http_rejection_has_no_secret_or_response_body(available_showtime, status_code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status_code,
            text=WEBHOOK,
            headers={"Location": "https://other.test/"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotificationRejected) as error:
            DiscordNotifier(client, WEBHOOK).send(available_showtime)
    assert error.value.status_code == status_code
    assert "synthetic-secret-token" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"Retry-After": "5.5"}, {"retry_after": 3}, 5.5),
        ({}, {"retry_after": 0.75}, 0.75),
        ({"Retry-After": "invalid"}, {"retry_after": "2.5"}, 2.5),
        ({"Retry-After": "NaN"}, {"retry_after": -1}, None),
        ({}, {"retry_after": True}, None),
        ({}, {}, None),
    ],
)
def test_rate_limit_exposes_wait_without_retry(available_showtime, headers, body, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers=headers, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotificationRateLimited) as error:
            DiscordNotifier(client, WEBHOOK).send(available_showtime)
    assert error.value.retry_after_seconds == expected
    assert len(requests) == 1


@pytest.mark.parametrize("status_code", [408, 500, 502, 503, 504])
def test_uncertain_http_failure_is_not_retried(available_showtime, status_code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status_code, text=WEBHOOK)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(NotificationDeliveryUnknown) as error:
            DiscordNotifier(client, WEBHOOK).send(available_showtime)
    assert "synthetic-secret-token" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("exception_type", "expected_error"),
    [
        (httpx.ConnectError, NotificationConnectionError),
        (httpx.ConnectTimeout, NotificationConnectionError),
        (httpx.PoolTimeout, NotificationConnectionError),
        (httpx.ReadTimeout, NotificationDeliveryUnknown),
        (httpx.WriteTimeout, NotificationDeliveryUnknown),
        (httpx.ReadError, NotificationDeliveryUnknown),
        (httpx.RemoteProtocolError, NotificationDeliveryUnknown),
    ],
)
def test_transport_failure_is_classified_and_sanitized(
    available_showtime, exception_type, expected_error
):
    requests = []

    def handler(request):
        requests.append(request)
        raise exception_type(f"request failed: {request.url}", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(expected_error) as error:
            DiscordNotifier(client, WEBHOOK).send(available_showtime)
    rendered_error = "".join(traceback.format_exception(error.value))
    assert "synthetic-secret-token" not in rendered_error
    assert error.value.__cause__ is None
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (204, None),
        (201, {"id": "123"}),
        (200, {}),
        (200, {"id": 123}),
        (200, {"id": ""}),
        (200, {"id": "１２３"}),
        (200, {"id": "not-an-id"}),
        (200, []),
    ],
)
def test_missing_message_confirmation_is_unknown(available_showtime, status_code, body):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=body))
    ) as client:
        with pytest.raises(NotificationDeliveryUnknown):
            DiscordNotifier(client, WEBHOOK).send(available_showtime)


def test_malformed_message_confirmation_is_unknown(available_showtime):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=WEBHOOK))
    ) as client:
        with pytest.raises(NotificationDeliveryUnknown) as error:
            DiscordNotifier(client, WEBHOOK).send(available_showtime)
    assert "synthetic-secret-token" not in str(error.value)


def test_httpx_info_log_redacts_webhook_url(available_showtime, caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "123"}))
    ) as client:
        notifier = DiscordNotifier(client, WEBHOOK)
        assert "synthetic-secret-token" not in repr(notifier)
        notifier.send(available_showtime)
    assert "HTTP Request: POST" in caplog.text
    assert "비공개" in caplog.text
    assert "synthetic-secret-token" not in caplog.text
    assert WEBHOOK not in caplog.text
