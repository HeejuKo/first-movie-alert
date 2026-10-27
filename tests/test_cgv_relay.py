import socket
import ssl
from datetime import date

import httpx
import pytest

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.models import BookingStatus
from movie_alert.providers.base import (
    ProviderUnavailableError,
    ResponseValidationError,
)
from movie_alert.providers.cgv_relay import CgvRelayProvider, parse_relay_schedule


def payload():
    return {
        "success": True,
        "data": {
            "playDate": "20261024",
            "timetable": [
                {
                    "scheduleId": "2026102400561",
                    "movieCode": "30001367",
                    "movieName": "극장판 치이카와-인어 섬의 비밀",
                    "theaterCode": "0056",
                    "theaterName": "CGV 강남",
                    "playDate": "20261024",
                    "startTime": "10:00",
                    "endTime": "11:49",
                    "totalSeats": 172,
                    "remainingSeats": 93,
                }
            ],
        },
    }


def movie_list_payload(movie_ids=("30001367",)):
    return {
        "success": True,
        "data": {
            "playDate": "20261024",
            "filters": {"theaterCode": "0056"},
            "movies": [{"movieCode": movie_id} for movie_id in movie_ids],
        },
    }


def test_relay_payload_is_converted_to_common_showtime():
    rows = parse_relay_schedule(
        payload(),
        movie_id="30001367",
        branch_id="0056",
        show_date=date(2026, 10, 24),
    )
    assert len(rows) == 1
    assert rows[0].provider == "cgv"
    assert rows[0].starts_at.isoformat() == "2026-10-24T10:00:00+09:00"
    assert rows[0].remaining_seats_hint == 93
    assert rows[0].booking_status == BookingStatus.UNKNOWN
    assert rows[0].screen_id == "2026102400561"


def test_relay_payload_can_omit_seat_count():
    response = payload()
    del response["data"]["timetable"][0]["remainingSeats"]
    rows = parse_relay_schedule(
        response,
        movie_id="30001367",
        branch_id="0056",
        show_date=date(2026, 10, 24),
    )
    assert rows[0].remaining_seats_hint is None


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"success": False, "data": {}},
        {"success": True, "data": {"timetable": None}},
    ],
)
def test_relay_response_validation_rejects_failed_or_incomplete_payload(bad_payload):
    with pytest.raises(ResponseValidationError):
        parse_relay_schedule(
            bad_payload,
            movie_id="30001367",
            branch_id="0056",
            show_date=date(2026, 10, 24),
        )


def test_relay_provider_sends_only_public_query_parameters():
    def handler(request):
        assert request.url.host == "mcp.aka.page"
        assert "cookie" not in request.headers
        assert request.url.path == "/api/cgv/timetable"
        assert dict(request.url.params) == {
            "playDate": "20261024",
            "theaterCode": "0056",
            "movieCode": "30001367",
            "limit": "50",
        }
        return httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(),
        )
        assert len(provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))) == 1


def test_relay_provider_retries_transient_error():
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/movies"):
            return httpx.Response(200, json=movie_list_payload())
        return httpx.Response(502) if len(calls) == 1 else httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=2),
            sleep=delays.append,
        )
        assert len(provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))) == 1
    assert len(calls) == 3
    assert delays == [0.5]


@pytest.mark.parametrize("failure", [502, "read_timeout"])
def test_failed_timetable_is_empty_only_after_confirming_unpublished_movie(failure):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/timetable"):
            if failure == "read_timeout":
                raise httpx.ReadTimeout("synthetic slow timetable", request=request)
            return httpx.Response(failure)
        assert request.url.path.endswith("/movies")
        assert dict(request.url.params) == {"playDate": "20261024", "theaterCode": "0056"}
        return httpx.Response(200, json=movie_list_payload(("30001605",)))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        assert provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24)) == []
    assert calls == ["/api/cgv/timetable", "/api/cgv/movies"]


def test_relay_502_remains_error_when_movie_is_listed():
    def handler(request):
        if request.url.path.endswith("/timetable"):
            return httpx.Response(502)
        return httpx.Response(200, json=movie_list_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError, match="HTTP 502"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))


def test_relay_provider_does_not_hide_upstream_error():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(502))) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError, match="HTTP 502"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))


def test_relay_network_failure_is_service_outage():
    def handler(request):
        raise httpx.ConnectError("synthetic connection failure", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError, match="연결에 실패"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))


def test_read_timeout_with_published_movie_remains_error_without_retries():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/timetable"):
            raise httpx.ReadTimeout("synthetic response delay", request=request)
        return httpx.Response(200, json=movie_list_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError, match="ReadTimeout"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
    assert calls == ["/api/cgv/timetable", "/api/cgv/movies"]


@pytest.mark.parametrize("path", ["/api/cgv/movies", "/api/cgv/timetable"])
@pytest.mark.parametrize(
    ("exception_type", "reason"),
    [
        (httpx.ConnectTimeout, "연결 시간 초과"),
        (httpx.ReadTimeout, "응답 대기 시간 초과"),
        (httpx.WriteTimeout, "요청 전송 시간 초과"),
        (httpx.PoolTimeout, "연결 풀 대기 시간 초과"),
        (httpx.ProxyError, "프록시 연결 실패"),
    ],
)
def test_network_failure_reports_phase_and_endpoint_without_raw_secrets(
    path, exception_type, reason
):
    def handler(request):
        if request.url.path == path:
            raise exception_type("https://proxy-user:private-token@proxy.example", request=request)
        if path == "/api/cgv/movies":
            return httpx.Response(502)
        return httpx.Response(200, json=movie_list_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError) as caught:
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
    message = str(caught.value)
    assert path in message
    assert reason in message
    assert exception_type.__name__ in message
    assert "초]" in message
    assert "private-token" not in message
    assert "proxy.example" not in message


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (socket.gaierror("private-dns-detail"), "DNSLookupError"),
        (ssl.SSLCertVerificationError("private-cert-detail"), "TLSCertificateError"),
    ],
)
def test_connection_error_preserves_dns_and_certificate_failure_category(cause, code):
    def handler(request):
        try:
            raise cause
        except OSError as error:
            raise httpx.ConnectError("private-connection-detail", request=request) from error

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderUnavailableError) as caught:
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
    assert code in str(caught.value)
    assert "private-" not in str(caught.value)


@pytest.mark.parametrize("wrong_context", ["date", "branch", "movie"])
def test_invalid_movie_list_cannot_be_mistaken_for_unpublished_movie(wrong_context):
    response = movie_list_payload(())
    if wrong_context == "date":
        response["data"]["playDate"] = "20261031"
    elif wrong_context == "branch":
        response["data"]["filters"]["theaterCode"] = "0001"
    else:
        response["data"]["movies"] = [{"movieName": "missing code"}]

    def handler(request):
        if request.url.path.endswith("/timetable"):
            return httpx.Response(502)
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ResponseValidationError):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
