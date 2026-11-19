from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from movie_alert.application import run_live
from movie_alert.config import ConfigError, PollingSettings, load_config
from movie_alert.models import SEOUL
from movie_alert.polling import next_polling_start, polling_allowed

HOURS = PollingSettings(7, 24)


@pytest.mark.parametrize(("hour", "allowed"), [(0, False), (6, False), (7, True), (23, True)])
def test_korean_polling_hours_include_seven_but_exclude_midnight(hour, allowed):
    now = datetime(2026, 11, 19, hour, 59, 59, tzinfo=SEOUL)
    assert polling_allowed(now, HOURS) is allowed
    assert polling_allowed(now.astimezone(UTC), HOURS) is allowed


def test_daily_exhaustion_resumes_the_next_korean_day_at_seven():
    now = datetime(2026, 12, 31, 19, 30, tzinfo=SEOUL)
    assert next_polling_start(now.astimezone(UTC), HOURS) == datetime(2027, 1, 1, 7, tzinfo=SEOUL)


def test_quiet_cli_does_not_query_or_report_a_provider_failure(config):
    provider = Mock()
    now = datetime(2026, 10, 23, 6, 59, tzinfo=SEOUL)
    assert run_live(replace(config, polling=HOURS), provider, now) == ([], [])
    provider.fetch_showtimes.assert_not_called()


@pytest.mark.parametrize(
    "settings",
    [
        "start_hour = true",
        "start_hour = -1",
        "end_hour = 25",
        "start_hour = 7\nend_hour = 7",
        "unknown = 1",
        "daily_request_limit = true",
        "daily_request_limit = -1",
        "daily_request_limit = 3001",
    ],
)
def test_invalid_polling_hours_fail_before_io(tmp_path, config_text, settings):
    path = tmp_path / "watch.toml"
    path.write_text(config_text + "\n[polling]\n" + settings)
    with pytest.raises(ConfigError, match="polling"):
        load_config(path)
