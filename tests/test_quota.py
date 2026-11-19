from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Event, Lock
from unittest.mock import Mock

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from movie_alert.application import run_live
from movie_alert.config import CgvSettings, HttpSettings, PollingSettings
from movie_alert.models import SEOUL
from movie_alert.providers.base import DailyQuotaExceeded, PollingPausedError
from movie_alert.providers.cgv_relay import CgvRelayProvider
from movie_alert.quota import DynamoDBRequestBudget, quota_content
from movie_alert.state import StateError

NOW = datetime(2026, 11, 19, 19, 30, tzinfo=SEOUL)
SETTINGS = PollingSettings(7, 24, 3000)


class MemoryClient:
    """Atomic counter test double; no AWS or outbound network access."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.lock = Lock()

    def update_item(self, **request):
        with self.lock:
            self.calls.append(deepcopy(request))
            key = request["Key"]["id"]["S"]
            item = self.items.setdefault(key, {"used": {"N": "0"}})
            values = request["ExpressionAttributeValues"]
            if "ConditionExpression" in request:
                if int(item["used"]["N"]) >= int(values[":limit"]["N"]) or item.get(
                    "remote_exhausted", {}
                ).get("BOOL", False):
                    raise ClientError(
                        {
                            "Error": {"Code": "ConditionalCheckFailedException"},
                            "Item": deepcopy(item),
                        },
                        "UpdateItem",
                    )
                item["used"] = {"N": str(int(item["used"]["N"]) + 1)}
            if ":true" in values:
                item["remote_exhausted"] = {"BOOL": True}
            return {"Attributes": deepcopy(item)}


def budget(client=None, *, used=0, now=NOW):
    client = client or MemoryClient()
    key = f"relay-budget:v1:{now.date().isoformat()}"
    client.items.setdefault(key, {"used": {"N": str(used)}})
    result = DynamoDBRequestBudget(client, "table", SETTINGS, clock=lambda: now)
    result.load()
    return result


def test_only_one_concurrent_worker_can_reserve_the_last_request():
    counter = budget(used=2999)
    # Separate control objects model a race even if a polling lease expires.
    competitor = budget(counter.client)

    def reserve(control):
        try:
            control.before_request()
            return True
        except DailyQuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, [counter, competitor])) == [False, True]
    assert budget(counter.client).snapshot.exhausted
    claims = [call for call in counter.client.calls if "ConditionExpression" in call]
    assert all(call["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD" for call in claims)
    assert all("#used < :limit" in call["ConditionExpression"] for call in claims)
    assert all("#expires" not in call["ConditionExpression"] for call in claims)


@pytest.mark.parametrize("first_failure", [502, 503, "timeout"])
def test_retries_cross_checks_and_failed_connections_all_consume_requests(first_failure):
    counter = budget()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            if first_failure == "timeout":
                raise httpx.ReadTimeout("private-value", request=request)
            return httpx.Response(first_failure)
        if request.url.path.endswith("movies"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "playDate": "20261125",
                        "filters": {"theaterCode": "0300"},
                        "movies": [],
                    },
                },
            )
        return httpx.Response(200, json={"success": True, "data": {"timetable": []}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = CgvRelayProvider(
            client,
            CgvSettings("A420", "08"),
            HttpSettings(),
            sleep=lambda _: None,
            request_control=counter,
        )
        assert provider.fetch_showtimes("30001367", "0300", NOW.date().replace(day=25)) == []
    assert len(calls) == counter.snapshot.used == 2


def test_the_final_successful_response_is_processed_without_an_extra_request():
    counter = budget(used=2999)
    transport = Mock(
        return_value=httpx.Response(200, json={"success": True, "data": {"timetable": []}})
    )
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = CgvRelayProvider(
            client, CgvSettings("A420", "08"), HttpSettings(), request_control=counter
        )
        assert provider.fetch_showtimes("30001367", "0300", NOW.date()) == []
        with pytest.raises(DailyQuotaExceeded):
            provider.fetch_showtimes("30001367", "0276", NOW.date())
    assert transport.call_count == 1
    assert counter.snapshot.used == 3000


@pytest.mark.parametrize("status", [200, 429])
def test_remote_exhaustion_stops_early_and_survives_a_new_execution(status):
    counter = budget()
    day = counter.before_request()
    response = httpx.Response(status, headers={"X-RateLimit-Remaining": "0"})
    if status == 429:
        with pytest.raises(DailyQuotaExceeded):
            counter.after_response(response, day)
    else:
        counter.after_response(response, day)
    restored = budget(counter.client)
    assert restored.snapshot.remote_exhausted
    with pytest.raises(DailyQuotaExceeded):
        restored.before_request()
    assert restored.snapshot.used == 1


def test_daily_limit_error_code_is_honored_without_rate_limit_headers():
    counter = budget()
    day = counter.before_request()
    with pytest.raises(DailyQuotaExceeded):
        counter.after_response(
            httpx.Response(429, json={"error": {"code": "DAILY_RATE_LIMIT_EXCEEDED"}}), day
        )
    assert counter.snapshot.remote_exhausted


def test_other_429_responses_do_not_become_a_daily_pause():
    counter = budget()
    day = counter.before_request()
    counter.after_response(httpx.Response(429, headers={"Retry-After": "60"}), day)
    assert not counter.snapshot.exhausted


def test_new_day_uses_a_fresh_counter_and_night_hours_still_block_requests():
    previous = budget(used=3000)
    midnight = budget(previous.client, now=NOW.replace(hour=0) + timedelta(days=1))
    assert midnight.snapshot.used == 0
    with pytest.raises(PollingPausedError):
        midnight.before_request()
    morning = budget(previous.client, now=NOW.replace(hour=7) + timedelta(days=1))
    morning.before_request()
    assert morning.snapshot.used == 1
    assert previous.snapshot.used == 3000
    assert "2026-11-20 07:00 KST" in quota_content(previous.snapshot, SETTINGS)


def test_crossing_midnight_in_one_execution_cannot_use_a_new_days_budget():
    counter = budget()
    counter.clock = lambda: NOW.replace(hour=0) + timedelta(days=1)
    with pytest.raises(PollingPausedError):
        counter.before_request()
    assert counter.snapshot.used == 0


def test_pause_notice_does_not_promise_a_restart_after_the_watch_period_ends():
    counter = budget(used=3000)
    end = (NOW + timedelta(days=1)).replace(hour=0, minute=0)
    content = quota_content(counter.snapshot, SETTINGS, active_until=end)
    assert "자동 재개하지 않습니다" in content
    assert "다음 감시:" not in content


def test_storage_failure_prevents_an_unaccounted_relay_request():
    counter = budget()
    counter.client = Mock()
    counter.client.update_item.side_effect = EndpointConnectionError(
        endpoint_url="https://private-value.example"
    )
    with pytest.raises(StateError) as error:
        counter.before_request()
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize(
    "item", [{}, {"used": {"N": "invalid"}}, {"used": {"N": "-1"}}, {"used": {"N": "3001"}}]
)
def test_invalid_counter_is_not_treated_as_a_new_daily_budget(item):
    client = Mock()
    client.update_item.return_value = {"Attributes": item}
    counter = DynamoDBRequestBudget(client, "table", SETTINGS, clock=lambda: NOW)
    with pytest.raises(StateError):
        counter.load()


def test_quota_pause_drains_a_running_peer_and_retains_its_successful_showtime(config, showtimes):
    gate = Event()
    first = config.watches[0]
    configured = replace(config, watches=(first, replace(first, name="second", branch_id="0001")))

    class Provider:
        def fetch_showtimes(self, movie, branch, day):
            if branch == "0056":
                assert gate.wait(timeout=2)
                return showtimes
            gate.set()
            raise DailyQuotaExceeded("일일 한도 소진")

    found, errors = run_live(configured, Provider(), datetime(2026, 10, 23, 12, tzinfo=SEOUL))
    assert errors == []
    assert found
    assert {row.branch_id for row in found} == {"0056"}
