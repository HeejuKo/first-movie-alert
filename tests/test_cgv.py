from datetime import date

import httpx
import pytest

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.models import BookingStatus
from movie_alert.providers.base import Provider, ProviderError, ResponseValidationError
from movie_alert.providers.cgv import CgvProvider, parse_schedule


def fetch(handler, *, settings=None, delays=None):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider: Provider = CgvProvider(
            client,
            CgvSettings("A420", "08"),
            settings or HttpSettings(),
            sleep=(delays if delays is not None else []).append,
        )
        return provider.fetch_showtimes("30001367", "0056", date(2026, 10, 24))


def test_captured_schedule_and_extended_midnight_time(showtimes):
    assert len(showtimes) == 7
    assert showtimes[0].branch_id == "0056"
    assert showtimes[0].screen_id == "003"
    assert showtimes[0].starts_at.isoformat() == "2026-10-24T10:00:00+09:00"
    assert showtimes[0].remaining_seats_hint == 83
    assert showtimes[-1].business_date == date(2026, 10, 24)
    assert showtimes[-1].ends_at.isoformat() == "2026-10-25T01:09:00+09:00"
    assert all(row.booking_status == BookingStatus.UNKNOWN for row in showtimes)


def test_captured_sungshin_schedule_preserves_all_screens(sungshin_showtimes):
    assert len(sungshin_showtimes) == 9
    assert {row.branch_id for row in sungshin_showtimes} == {"0300"}
    assert {row.movie_id for row in sungshin_showtimes} == {"30001367"}
    assert {row.business_date for row in sungshin_showtimes} == {date(2026, 10, 24)}
    assert {row.screen_id for row in sungshin_showtimes} == {"003", "004"}
    assert len({(row.screen_id, row.sequence) for row in sungshin_showtimes}) == 9
    assert all(row.booking_status == BookingStatus.UNKNOWN for row in sungshin_showtimes)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"statusCode": "0", "data": []},
        {"statusCode": 1, "data": []},
        {"statusCode": 0, "data": None},
        {"statusCode": 0, "data": [None]},
    ],
)
def test_malformed_or_failed_response_is_not_an_empty_schedule(payload):
    with pytest.raises(ResponseValidationError):
        parse_schedule(payload)


def test_successful_empty_schedule():
    assert parse_schedule({"statusCode": 0, "data": []}) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scnsrtTm", "1099"),
        ("scnYmd", "20260230"),
        ("frSeatCnt", "-1"),
        ("scnendTm", "0900"),
        ("siteNo", 56),
    ],
)
def test_invalid_first_row_fails_the_whole_response(payload, field, value):
    payload["data"][0][field] = value
    with pytest.raises(ResponseValidationError):
        parse_schedule(payload)


def test_missing_first_row_start_cannot_promote_second_session(payload):
    del payload["data"][0]["scnsrtTm"]
    with pytest.raises(ResponseValidationError):
        parse_schedule(payload)


def test_captured_query_is_sent_with_timeouts_and_without_browser_credentials(payload):
    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "cgv.co.kr"
        assert request.url.path == "/api/v1/booking/searchSchByMov"
        assert dict(request.url.params) == {
            "coCd": "A420",
            "siteNo": "0056",
            "scnYmd": "20261024",
            "movNo": "30001367",
            "rtctlScopCd": "08",
        }
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        assert request.headers["user-agent"] == "movie-alert/0.1"
        assert request.extensions["timeout"]["connect"] == 5
        assert request.extensions["timeout"]["read"] == 10
        return httpx.Response(200, json=payload)

    assert len(fetch(handler)) == 7


def test_server_error_retries_then_returns_data(payload):
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=payload)

    assert len(fetch(handler, delays=delays)) == 7
    assert len(calls) == 2
    assert delays == [0.5]


def test_timeout_retry_limit_and_safe_error_message():
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private-cookie-value", request=request)

    with pytest.raises(ProviderError) as error:
        fetch(handler, settings=HttpSettings(max_retries=2), delays=delays)
    assert len(calls) == 3
    assert delays == [0.5, 1.0]
    assert "private-cookie-value" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_denied_and_rate_limit_stop_without_retry(status):
    calls = []
    delays = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "60"})

    with pytest.raises(ProviderError, match=str(status)):
        fetch(handler, delays=delays)
    assert len(calls) == 1
    assert delays == []


def test_html_success_response_is_not_a_schedule():
    with pytest.raises(ResponseValidationError):
        fetch(lambda request: httpx.Response(200, text="<html>not a schedule</html>"))


def test_response_scope_must_match_query(payload):
    payload["data"][0]["siteNo"] = "another-branch"
    with pytest.raises(ResponseValidationError, match="요청과 다릅니다"):
        fetch(lambda request: httpx.Response(200, json=payload))
