import tomllib
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from movie_alert.config import ConfigError, load_config
from movie_alert.main import run_live
from movie_alert.models import SEOUL

EXAMPLE = Path(__file__).parents[1] / "config/watch.example.toml"
NOW = datetime(2026, 10, 23, 12, tzinfo=SEOUL)


def test_example_selects_one_candidate_by_its_indices():
    raw = tomllib.loads(EXAMPLE.read_text())
    selection = raw["selection"]
    candidate = {**raw["watch_defaults"], **raw["watches"][selection["branch_index"]]}
    config = load_config(EXAMPLE)
    assert len(config.watches) == 1
    assert config.watches[0].branch_id == candidate["branch_id"]
    assert config.watches[0].dates == (
        date.fromisoformat(str(candidate["dates"][selection["date_index"]])),
    )


def test_changing_both_indices_changes_the_only_query(tmp_path, selection_text):
    path = tmp_path / "watch.toml"
    path.write_text(
        selection_text.replace("branch_index = 3", "branch_index = 2").replace(
            "date_index = 0", "date_index = 1"
        )
    )
    config = load_config(path)
    provider = Mock()
    provider.fetch_showtimes.return_value = []
    assert run_live(config, provider, NOW) == ([], [])
    provider.fetch_showtimes.assert_called_once_with("30001367", "0300", date(2026, 10, 31))


def test_date_index_uses_written_order_before_sorting_and_deduplication(tmp_path, selection_text):
    path = tmp_path / "watch.toml"
    path.write_text(
        selection_text.replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-31", "2026-10-28", "2026-10-31"]',
        )
    )
    assert load_config(path).watches[0].dates == (date(2026, 10, 31),)


def test_date_selection_respects_the_selected_branch_override(tmp_path, selection_text):
    path = tmp_path / "watch.toml"
    path.write_text(
        selection_text.replace("branch_index = 3", "branch_index = 5")
        + '\ndates = ["2026-11-07"]\n'
    )
    rule = load_config(path).watches[0]
    assert rule.branch_id == "0191"
    assert rule.dates == (date(2026, 11, 7),)


@pytest.mark.parametrize(
    "selection",
    [
        "[selection]",
        "[selection]\nbranch_index = 0",
        "[selection]\ndate_index = 0",
        "[selection]\nbranch_index = 0\ndate_index = 0\ndate_indxe = 0",
        "selection = []",
        *(
            f"[selection]\nbranch_index = {value}\ndate_index = 0"
            for value in ("-1", "6", "true", "1.0", '"0"')
        ),
        *(
            f"[selection]\nbranch_index = 0\ndate_index = {value}"
            for value in ("-1", "4", "false", "0.0", '"0"')
        ),
    ],
)
def test_invalid_selection_fails_instead_of_querying_everything(tmp_path, config_text, selection):
    path = tmp_path / "watch.toml"
    path.write_text(selection + "\n" + config_text)
    with pytest.raises(ConfigError, match="selection"):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-22", "2026-10-31"]',
        ),
        ("enabled = true", "enabled = false"),
    ],
)
def test_inactive_selection_does_not_advance_to_other_candidates(
    tmp_path, selection_text, old, new
):
    path = tmp_path / "watch.toml"
    path.write_text(selection_text.replace(old, new))
    provider = Mock()
    assert run_live(load_config(path), provider, NOW) == ([], [])
    provider.fetch_showtimes.assert_not_called()
