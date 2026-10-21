from dataclasses import replace
from datetime import date, datetime

import pytest

from movie_alert.checker import select_first_showtimes
from movie_alert.models import SEOUL, BookingStatus


def test_unsorted_schedule_selects_1000(showtimes, rule):
    assert select_first_showtimes(reversed(showtimes), rule) == [showtimes[0]]


def test_unavailable_first_session_never_promotes_available_later_one(showtimes, rule):
    first = replace(showtimes[0], booking_status=BookingStatus.UNAVAILABLE, remaining_seats_hint=0)
    later = replace(showtimes[1], booking_status=BookingStatus.AVAILABLE)
    assert select_first_showtimes([later, first], rule) == [first]


def test_first_session_is_based_on_start_time_across_screens(showtimes, rule):
    earlier = replace(
        showtimes[0],
        screen_id="002",
        sequence="7",
        starts_at=datetime(2026, 10, 24, 9, tzinfo=SEOUL),
    )
    assert select_first_showtimes([showtimes[0], earlier], rule) == [earlier]


def test_captured_sungshin_selects_0900_across_screens(sungshin_showtimes, config):
    rule = next(rule for rule in config.watches if rule.branch_id == "0300")
    assert sungshin_showtimes[0].starts_at.hour == 14
    selected = select_first_showtimes(sungshin_showtimes, rule)
    assert len(selected) == 1
    first = selected[0]
    assert first.starts_at == datetime(2026, 10, 24, 9, tzinfo=SEOUL)
    assert first.screen_id == "004"
    assert first.screen_name == "2관 (리클라이너,Laser)"
    assert first.remaining_seats_hint == 46


def test_simultaneous_first_sessions_are_preserved(showtimes, rule):
    other_screen = replace(showtimes[0], screen_id="002")
    assert select_first_showtimes([showtimes[1], showtimes[0], other_screen], rule) == [
        other_screen,
        showtimes[0],
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"movie_id": "other"},
        {"branch_id": "other"},
        {"provider": "other"},
        {"business_date": date(2026, 10, 25)},
    ],
)
def test_movie_branch_provider_and_date_filtering(showtimes, rule, changes):
    assert select_first_showtimes([replace(showtimes[0], **changes)], rule) == []


def test_configured_weekdays_apply_to_show_date(showtimes, rule):
    assert select_first_showtimes(showtimes, replace(rule, weekdays=frozenset({3}))) == []
    wednesday = date(2026, 10, 28)
    row = replace(
        showtimes[0],
        business_date=wednesday,
        starts_at=showtimes[0].starts_at.replace(day=28),
        ends_at=showtimes[0].ends_at.replace(day=28),
    )
    assert select_first_showtimes([row], replace(rule, dates=(wednesday,))) == [row]


def test_disabled_rule_is_ignored(showtimes, rule):
    assert select_first_showtimes(showtimes, replace(rule, enabled=False)) == []
