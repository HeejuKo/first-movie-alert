from datetime import date

import httpx
import pytest

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.models import BookingStatus
from movie_alert.providers.base import ProviderError, ResponseValidationError
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
        assert request.url.path == "/api/cgv/timetable"
        assert dict(request.url.params) == {
            "playDate": "20261024",
            "theaterCode": "0056",
            "movieCode": "30001367",
            "limit": "50",
        }
        assert "cookie" not in request.headers
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
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"movies": [{"movieCode": "30001367", "movieName": "치이카와"}]},
                },
            )
        return httpx.Response(502) if len(calls) == 1 else httpx.Response(200, json=payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(),
            sleep=delays.append,
        )
        assert len(provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))) == 1
    assert len(calls) == 3
    assert delays == [0.5]


def test_relay_502_is_empty_when_movie_is_not_published_yet():
    def handler(request):
        if request.url.path.endswith("/timetable"):
            return httpx.Response(502)
        assert request.url.path.endswith("/movies")
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"movies": [{"movieCode": "30001605", "movieName": "다른 영화"}]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        assert provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24)) == []


def test_relay_502_remains_error_when_movie_is_listed():
    def handler(request):
        if request.url.path.endswith("/timetable"):
            return httpx.Response(502)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"movies": [{"movieCode": "30001367", "movieName": "치이카와"}]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderError, match="HTTP 502"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))


def test_relay_provider_does_not_hide_upstream_error():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(502))) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08", source="mcp_relay"),
            HttpSettings(max_retries=0),
        )
        with pytest.raises(ProviderError, match="HTTP 502"):
            provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))
