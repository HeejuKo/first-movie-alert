import json
import threading
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from movie_alert.config import ConfigError, load_config
from movie_alert.main import main, run_live
from movie_alert.models import SEOUL
from movie_alert.providers.base import (
    AccessDeniedError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/watch.example.toml"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('branch_id = "0056"', "branch_id = 56"),
        ("[watch_defaults]", "[watch_defaults]\nweekdays = [0]"),
        (
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            "dates = []",
        ),
        ("[watch_defaults]", "[[watch_defaults]]"),
        ("2026-10-28", "2026-02-30"),
        ("active_until = 2026-11-08T00:00:00+09:00", "active_until = 2026-11-08T00:00:00"),
        ("max_retries = 0", "max_retries = 50"),
        ("max_parallel_requests = 3", "max_parallel_requests = 7"),
        ("timeout_seconds = 10.0", "timeout_seconds = nan"),
        ('provider = "cgv"', 'provider = "lotte"'),
        ('source = "mcp_relay"', 'source = "unknown"'),
    ],
)
def test_invalid_config_fails_before_requests(tmp_path, old, new):
    path = tmp_path / "watch.toml"
    path.write_text(EXAMPLE.read_text().replace(old, new))
    with pytest.raises(ConfigError):
        load_config(path)


class FakeProvider:
    def __init__(self, rows, fail=False):
        self.rows = rows
        self.fail = fail
        self.calls = []

    def fetch_showtimes(self, movie_id, branch_id, show_date):
        self.calls.append((movie_id, branch_id, show_date))
        if self.fail:
            raise ProviderError("synthetic failure")
        return self.rows


def test_shared_dates_apply_to_all_branches_and_only_selected_future_dates_are_queried(tmp_path):
    path = tmp_path / "watch.toml"
    path.write_text(
        EXAMPLE.read_text().replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-20", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-24"]',
        )
    )
    config = load_config(path)
    assert all(rule.weekdays == frozenset(range(1, 8)) for rule in config.watches)
    provider = FakeProvider([])
    assert run_live(config, provider, datetime(2026, 10, 22, tzinfo=SEOUL)) == ([], [])
    expected = {
        ("30001367", branch_id, day)
        for branch_id in ("0056", "0001", "0300", "0276", "0292", "0191")
        for day in (date(2026, 10, 22),)
    }
    assert set(provider.calls) == expected
    assert len(provider.calls) == len(expected)


def test_individual_watch_can_override_shared_dates_and_weekdays(tmp_path):
    path = tmp_path / "watch.toml"
    path.write_text(
        EXAMPLE.read_text().replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-24"]',
        )
        + '\ndates = ["2026-10-28"]\nweekdays = [3]\n'
    )
    config = load_config(path)
    assert all(rule.dates == (date(2026, 10, 24),) for rule in config.watches[:-1])
    assert config.watches[-1].dates == (date(2026, 10, 28),)
    assert config.watches[-1].weekdays == frozenset({3})


def test_live_queries_all_branches_by_date_before_moving_to_next_date(config, showtimes):
    config = replace(
        config,
        watches=tuple(
            replace(rule, dates=(date(2026, 10, 24), date(2026, 10, 28))) for rule in config.watches
        ),
    )

    class OrderedProvider(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            self.calls.append((movie_id, branch_id, show_date))
            return [replace(showtimes[0], branch_id=branch_id, business_date=show_date)]

    provider = OrderedProvider([])
    selected, errors = run_live(config, provider, datetime(2026, 10, 22, tzinfo=SEOUL))
    assert errors == []
    assert len(selected) == 12
    assert [call[2] for call in provider.calls] == [
        date(2026, 10, 24),
        date(2026, 10, 24),
        date(2026, 10, 24),
        date(2026, 10, 24),
        date(2026, 10, 24),
        date(2026, 10, 24),
        date(2026, 10, 28),
        date(2026, 10, 28),
        date(2026, 10, 28),
        date(2026, 10, 28),
        date(2026, 10, 28),
        date(2026, 10, 28),
    ]


def test_date_first_query_skips_rules_without_that_date(config, showtimes):
    config = replace(
        config,
        watches=(
            replace(config.watches[0], dates=(date(2026, 10, 24),)),
            *(replace(rule, dates=(date(2026, 10, 28),)) for rule in config.watches[1:]),
        ),
    )

    class ProviderWithRows(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            self.calls.append((movie_id, branch_id, show_date))
            return [replace(showtimes[0], branch_id=branch_id, business_date=show_date)]

    provider = ProviderWithRows([])
    run_live(config, provider, datetime(2026, 10, 22, tzinfo=SEOUL))
    assert provider.calls[0][1:] == ("0056", date(2026, 10, 24))
    assert {call[1:] for call in provider.calls[1:]} == {
        (branch_id, date(2026, 10, 28)) for branch_id in ("0001", "0300", "0276", "0292", "0191")
    }


def test_config_selects_public_cgv_relay():
    config = load_config(EXAMPLE)
    assert config.cgv.source == "mcp_relay"
    assert config.cgv.relay_base_url == "https://mcp.aka.page"
    assert config.http.max_parallel_requests == 3


def test_same_date_branches_are_queried_in_bounded_parallel(config):
    barrier = threading.Barrier(config.http.max_parallel_requests)

    class ConcurrentProvider(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            self.calls.append((movie_id, branch_id, show_date))
            barrier.wait(timeout=2)
            return []

    provider = ConcurrentProvider([])
    assert run_live(config, provider, datetime(2026, 10, 22, tzinfo=SEOUL)) == ([], [])
    assert len(provider.calls) == len(config.watches)


def test_unselected_date_in_saved_response_is_excluded(tmp_path, capsys):
    path = tmp_path / "watch.toml"
    path.write_text(
        EXAMPLE.read_text().replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-23"]',
        )
    )
    fixture = ROOT / "tests/fixtures/cgv_gangnam_20261024.json"
    assert main(["--config", str(path), "--response", str(fixture)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["first_showtimes"] == []
    assert output["notifications"]["previews"] == []


def test_watch_can_run_on_thursday_to_check_saturday(config, showtimes):
    provider = FakeProvider(showtimes)
    selected, errors = run_live(config, provider, datetime(2026, 10, 22, 12, tzinfo=SEOUL))
    assert selected == [showtimes[0]]
    assert errors == []
    assert len(provider.calls) == len(config.watches)


@pytest.mark.parametrize(
    "day", [datetime(2026, 10, 20, tzinfo=SEOUL), datetime(2026, 11, 1, tzinfo=SEOUL)]
)
def test_monitoring_boundaries_make_no_requests(config, day):
    provider = FakeProvider([])
    assert run_live(config, provider, day) == ([], [])
    assert provider.calls == []


def test_one_failed_watch_does_not_hide_other_watch_results(config, showtimes):
    class PartialProvider(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            if branch_id == "broken":
                raise ProviderError("synthetic failure")
            return self.rows

    other = replace(config.watches[0], name="다른 지점", branch_id="broken")
    config = replace(config, watches=(other, *config.watches))
    selected, errors = run_live(
        config, PartialProvider(showtimes), datetime(2026, 10, 21, tzinfo=SEOUL)
    )
    assert selected == [showtimes[0]]
    assert len(errors) == 1


@pytest.mark.parametrize("error_type", [AccessDeniedError, RateLimitError])
def test_access_restriction_stops_remaining_queries(config, error_type):
    class RestrictedProvider(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            self.calls.append((movie_id, branch_id, show_date))
            raise error_type("synthetic access restriction")

    another = replace(config.watches[0], name="다른 지점", branch_id="another")
    config = replace(config, watches=(*config.watches, another))
    provider = RestrictedProvider([])
    selected, errors = run_live(config, provider, datetime(2026, 10, 21, tzinfo=SEOUL))
    assert selected == []
    assert len(errors) == 1
    assert 1 <= len(provider.calls) <= config.http.max_parallel_requests


def test_relay_outage_stops_remaining_branches_and_later_dates(config):
    later = date(2026, 10, 28)
    config = replace(
        config,
        watches=tuple(replace(rule, dates=(date(2026, 10, 24), later)) for rule in config.watches),
    )

    class UnavailableProvider(FakeProvider):
        def fetch_showtimes(self, movie_id, branch_id, show_date):
            self.calls.append((movie_id, branch_id, show_date))
            raise ProviderUnavailableError("synthetic relay outage")

    provider = UnavailableProvider([])
    selected, errors = run_live(config, provider, datetime(2026, 10, 22, tzinfo=SEOUL))
    assert selected == []
    assert len(errors) == 1
    assert 1 <= len(provider.calls) <= config.http.max_parallel_requests
    assert {call[2] for call in provider.calls} == {date(2026, 10, 24)}


@pytest.mark.parametrize(
    ("fixture_name", "branch_id", "starts_at"),
    [
        ("cgv_gangnam_20261024.json", "0056", "2026-10-24T10:00:00+09:00"),
        ("cgv_sungshin_20261024.json", "0300", "2026-10-24T09:00:00+09:00"),
    ],
)
def test_offline_cli_replays_capture_without_network(
    capsys, monkeypatch, tmp_path, fixture_name, branch_id, starts_at
):
    monkeypatch.setattr(
        "movie_alert.main.current_time", lambda: datetime(2026, 10, 22, tzinfo=SEOUL)
    )
    fixture = ROOT / "tests/fixtures" / fixture_name
    config_path = tmp_path / "watch.toml"
    config_path.write_text(
        EXAMPLE.read_text().replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-24"]',
        )
    )
    assert main(["--config", str(config_path), "--response", str(fixture)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "offline"
    assert output["errors"] == []
    assert len(output["first_showtimes"]) == 1
    assert output["first_showtimes"][0]["branch_id"] == branch_id
    assert output["first_showtimes"][0]["starts_at"] == starts_at
    assert output["first_showtimes"][0]["booking_status"] == "unknown"
    assert "screen_id" not in output["first_showtimes"][0]
    assert "screen_name" not in output["first_showtimes"][0]
    assert output["notifications"]["mode"] == "preview"
    assert output["notifications"]["previews"][0]["eligible"] is True
    assert "상영시간표 발견" in output["notifications"]["previews"][0]["content"]
    assert "예매 가능 여부: 미확인" in output["notifications"]["previews"][0]["content"]
    assert "상영관:" not in output["notifications"]["previews"][0]["content"]
    assert output["notifications"]["outcomes"] == []


def test_cli_reports_missing_config(capsys, tmp_path):
    assert main(["--config", str(tmp_path / "missing.toml")]) == 2


def test_cli_rejects_malformed_response(tmp_path):
    path = tmp_path / "response.json"
    path.write_text("<html>not JSON</html>")
    assert main(["--config", str(EXAMPLE), "--response", str(path)]) == 1
