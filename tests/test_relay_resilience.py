from datetime import UTC, date, datetime
from unittest.mock import Mock

import httpx
import pytest

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.providers.base import ProviderError, ProviderUnavailableError
from movie_alert.providers.cgv_relay import CgvRelayProvider
from movie_alert.providers.common import retry_after_seconds

DAY = date(2026, 10, 31)


def provider(client, delays):
    return CgvRelayProvider(
        client,
        CgvSettings("A420", "08", source="mcp_relay"),
        HttpSettings(timeout_seconds=20, max_retries=1),
        sleep=delays.append,
    )


@pytest.mark.parametrize("failure", [503, 504, "connect_error"])
def test_one_transient_retry_can_recover_with_two_requests(failure):
    calls, delays = [], []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            if failure == "connect_error":
                raise httpx.ConnectError("synthetic failure", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"success": True, "data": {"timetable": []}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert provider(client, delays).fetch_showtimes("30001367", "0300", DAY) == []
    assert calls == ["/api/cgv/timetable"] * 2
    assert delays == [0.5]


@pytest.mark.parametrize("failure", [502, "read_timeout"])
def test_movie_list_fallback_uses_the_retry_budget_instead_of_a_third_request(failure):
    calls, delays = [], []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            if failure == "read_timeout":
                raise httpx.ReadTimeout("synthetic failure", request=request)
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "playDate": "20261031",
                    "filters": {"theaterCode": "0300"},
                    "movies": [{"movieCode": "30001367"}],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailableError):
            provider(client, delays).fetch_showtimes("30001367", "0300", DAY)
    assert calls == ["/api/cgv/timetable", "/api/cgv/movies"]
    assert delays == []


def test_a_retry_cannot_add_an_unbudgeted_movie_list_request():
    calls, delays = [], []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(503 if len(calls) == 1 else 502)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailableError, match="HTTP 502"):
            provider(client, delays).fetch_showtimes("30001367", "0300", DAY)
    assert calls == ["/api/cgv/timetable"] * 2


@pytest.mark.parametrize("status", [429, 502, 503])
def test_retry_after_stops_immediate_retries_and_cross_checks(status):
    transport = Mock(return_value=httpx.Response(status, headers={"Retry-After": "120"}))
    delays = []
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ProviderError) as error:
            provider(client, delays).fetch_showtimes("30001367", "0300", DAY)
    assert error.value.retry_after_seconds == 120
    assert transport.call_count == 1
    assert delays == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [("120", 120), ("-1", 0), ("invalid", 0), ("Tue, 27 Oct 2026 04:02:00 GMT", 120)],
)
def test_retry_after_parses_delay_or_http_date(header, expected):
    response = httpx.Response(503, headers={"Retry-After": header})
    assert retry_after_seconds(response, now=datetime(2026, 10, 27, 4, tzinfo=UTC)) == expected


def test_relay_error_code_does_not_expose_raw_response_text():
    def handler(request):
        return httpx.Response(
            503,
            headers={"Retry-After": "60"},
            json={
                "error": {"code": "CGV_RELAY_FAILED", "message": "private-token"},
                "diagnostics": {"secret": "private-token"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderUnavailableError) as error:
            provider(client, []).fetch_showtimes("30001367", "0300", DAY)
    assert "CGV_RELAY_FAILED" in str(error.value)
    assert "private-token" not in str(error.value)
