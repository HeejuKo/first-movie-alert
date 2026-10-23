from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from movie_alert.models import SEOUL, BookingStatus
from movie_alert.notifier import (
    DeliveryReceipt,
    NotificationConnectionError,
    NotificationDeliveryUnknown,
    NotificationRateLimited,
    NotificationRejected,
)
from movie_alert.pipeline import (
    AlertDispatcher,
    Notifier,
    notification_preview,
    showtime_key,
)
from movie_alert.state import StateError, StateStore

NOW = datetime(2026, 10, 21, 12, tzinfo=SEOUL)
END = datetime(2026, 11, 1, tzinfo=SEOUL)


@pytest.fixture
def available(showtimes):
    # Synthetic availability: the original captured status is still UNKNOWN.
    return replace(showtimes[0], booking_status=BookingStatus.AVAILABLE)


@pytest.fixture
def dependencies():
    state = Mock(spec=StateStore)
    state.claim.return_value = True
    notifier = Mock(spec=Notifier)
    notifier.send.return_value = DeliveryReceipt("123456")
    return state, notifier


def dispatcher(dependencies, *, clock=lambda: NOW):
    state, notifier = dependencies
    return AlertDispatcher(state, notifier, clock=clock)


@pytest.mark.parametrize("status", [BookingStatus.UNKNOWN, BookingStatus.UNAVAILABLE])
def test_detected_schedule_sends_regardless_of_booking_status(available, dependencies, status):
    showtime = replace(available, booking_status=status)
    result = dispatcher(dependencies).dispatch(showtime, active_until=END)
    assert result.status == "sent"
    dependencies[0].claim.assert_called_once()
    dependencies[0].mark_sent.assert_called_once()
    dependencies[1].send.assert_called_once_with(showtime)


def test_started_session_is_skipped(available, dependencies):
    result = dispatcher(dependencies, clock=lambda: available.starts_at).dispatch(
        available, active_until=END
    )
    assert result.status == "skipped"
    assert dependencies[0].mock_calls == []
    dependencies[1].send.assert_not_called()


@pytest.mark.parametrize(
    "error,expected",
    [
        (None, "sent"),
        (NotificationDeliveryUnknown("lost"), "uncertain"),
        (NotificationConnectionError("offline"), "deferred"),
    ],
)
def test_operational_messages_use_existing_claim_and_failure_policy(dependencies, error, expected):
    state, notifier = dependencies
    notifier.send_text.return_value = DeliveryReceipt("123456")
    notifier.send_text.side_effect = error
    result = dispatcher(dependencies).dispatch_message(
        "operations:v1:incident:failure", "운영 장애", active_until=END
    )
    assert result.status == expected
    state.claim.assert_called_once()
    state.ensure_owned.assert_called_once()
    notifier.send.assert_not_called()
    notifier.send_text.assert_called_once_with("운영 장애")


def test_operational_send_cannot_use_a_showtime_state_key(dependencies):
    with pytest.raises(ValueError):
        dispatcher(dependencies).dispatch_message("showtime:v1:key", "운영 장애", active_until=END)
    dependencies[0].claim.assert_not_called()


def test_monitoring_end_is_exclusive(available, dependencies):
    result = dispatcher(dependencies).dispatch(available, active_until=NOW)
    assert result.status == "skipped"
    assert dependencies[0].mock_calls == []


def test_claim_and_ownership_precede_send_and_completion(available, dependencies):
    events = Mock()
    events.attach_mock(dependencies[0], "state")
    events.attach_mock(dependencies[1], "notifier")
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "sent"
    assert result.message_id == "123456"
    assert [event[0] for event in events.mock_calls] == [
        "state.claim",
        "state.ensure_owned",
        "notifier.send",
        "state.mark_sent",
    ]


@pytest.mark.parametrize("status", list(BookingStatus))
def test_second_claim_is_skipped_and_has_a_new_owner_token(available, dependencies, status):
    available = replace(available, booking_status=status)
    state, notifier = dependencies
    state.claim.side_effect = [True, False]
    service = dispatcher(dependencies)
    assert service.dispatch(available, active_until=END).status == "sent"
    assert service.dispatch(available, active_until=END).status == "not_claimed"
    assert state.claim.call_args_list[0].args[1] != state.claim.call_args_list[1].args[1]
    assert notifier.send.call_count == 1


@pytest.mark.parametrize("operation", ["claim", "ensure_owned"])
def test_state_failure_before_send_never_posts(available, dependencies, operation):
    getattr(dependencies[0], operation).side_effect = StateError("synthetic state failure")
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "state_error"
    dependencies[1].send.assert_not_called()


@pytest.mark.parametrize(
    ("error", "delay"),
    [
        (NotificationConnectionError("synthetic connection failure"), 60),
        (NotificationRateLimited(None), 60),
        (NotificationRateLimited(2.5), 60),
        (NotificationRateLimited(120.5), 121),
    ],
)
def test_known_failure_is_released_for_bounded_later_retry(available, dependencies, error, delay):
    state, notifier = dependencies
    notifier.send.side_effect = error
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "deferred"
    assert notifier.send.call_count == 1
    assert state.release_for_retry.call_args.args[3] == delay
    state.mark_sent.assert_not_called()
    state.mark_uncertain.assert_not_called()


def test_retry_beyond_session_start_is_terminal(available, dependencies):
    dependencies[1].send.side_effect = NotificationRateLimited(10**20)
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "failed"
    dependencies[0].mark_failed.assert_called_once()
    dependencies[0].release_for_retry.assert_not_called()


def test_ambiguous_send_is_held_without_automatic_retry(available, dependencies):
    state, notifier = dependencies
    notifier.send.side_effect = NotificationDeliveryUnknown("synthetic response loss")
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "uncertain"
    state.mark_uncertain.assert_called_once()
    state.release_for_retry.assert_not_called()
    assert notifier.send.call_count == 1


def test_permanent_rejection_is_terminal(available, dependencies):
    dependencies[1].send.side_effect = NotificationRejected("synthetic rejection", status_code=404)
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "failed"
    dependencies[0].mark_failed.assert_called_once()
    dependencies[0].release_for_retry.assert_not_called()


def test_success_followed_by_state_failure_keeps_receipt_and_holds(available, dependencies):
    state, notifier = dependencies
    state.mark_sent.side_effect = StateError("synthetic state failure")
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "uncertain"
    assert result.message_id == "123456"
    state.mark_uncertain.assert_called_once()
    assert notifier.send.call_count == 1


def test_failed_hold_reports_manual_review(available, dependencies):
    state, notifier = dependencies
    notifier.send.side_effect = NotificationDeliveryUnknown("synthetic response loss")
    state.mark_uncertain.side_effect = StateError("synthetic state failure")
    result = dispatcher(dependencies).dispatch(available, active_until=END)
    assert result.status == "state_error"
    assert "수동" in result.detail
    assert notifier.send.call_count == 1


def test_expiration_during_claim_prevents_send(available, dependencies):
    times = iter([NOW, available.starts_at])
    result = dispatcher(dependencies, clock=lambda: next(times)).dispatch(
        available, active_until=END
    )
    assert result.status == "skipped"
    dependencies[1].send.assert_not_called()


@pytest.mark.parametrize("boundary", ["start", "monitoring_end", "lease_end"])
def test_slow_ownership_check_cannot_send_after_a_deadline(available, dependencies, boundary):
    current = {"now": NOW}
    until = END
    after = NOW + timedelta(seconds=121)
    if boundary == "start":
        current["now"] = available.starts_at - timedelta(seconds=1)
        after = available.starts_at + timedelta(seconds=1)
    elif boundary == "monitoring_end":
        until = NOW + timedelta(seconds=1)
        after = NOW + timedelta(seconds=2)
    dependencies[0].ensure_owned.side_effect = lambda *args: current.update(now=after)
    result = dispatcher(dependencies, clock=lambda: current["now"]).dispatch(
        available, active_until=until
    )
    assert result.status in {"skipped", "state_error"}
    dependencies[1].send.assert_not_called()


@pytest.mark.parametrize("retry_after", [None, 120.9])
def test_fractional_response_time_cannot_shorten_retry_after(available, dependencies, retry_after):
    now = NOW.replace(microsecond=900000)
    dependencies[1].send.side_effect = NotificationRateLimited(retry_after)
    result = dispatcher(dependencies, clock=lambda: now).dispatch(available, active_until=END)
    assert result.status == "deferred"
    args = dependencies[0].release_for_retry.call_args.args
    stored_retry_epoch = args[2] + args[3]
    assert stored_retry_epoch >= now.timestamp() + max(60, retry_after or 0)


def test_seat_and_status_changes_do_not_create_new_identity(available):
    changed = replace(available, remaining_seats_hint=0, booking_status=BookingStatus.UNAVAILABLE)
    assert showtime_key(changed) == showtime_key(available)
    utc = replace(available, starts_at=available.starts_at.astimezone(UTC))
    assert showtime_key(utc) == showtime_key(available)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "another"},
        {"movie_id": "another"},
        {"branch_id": "another"},
        {"screen_id": "another"},
        {"sequence": "another"},
    ],
)
def test_distinct_screening_has_distinct_key(available, changes):
    assert showtime_key(replace(available, **changes)) != showtime_key(available)


def test_unknown_preview_never_claims_booking_open(showtimes):
    preview = notification_preview(showtimes[0], NOW)
    assert preview["eligible"] is True
    assert preview["reason"] is None
    assert "상영시간표 발견" in preview["content"]
    assert "예매 가능 여부: 미확인" in preview["content"]
    assert "예매 오픈 감지" not in preview["content"]
