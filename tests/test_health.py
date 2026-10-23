import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from movie_alert.health import (
    HEALTH_KEY,
    DynamoDBHealthStore,
    HealthMonitor,
    HealthSnapshot,
    advance,
    event_content,
)
from movie_alert.models import SEOUL
from movie_alert.pipeline import AlertOutcome
from movie_alert.state import StateError

NOW = datetime(2026, 10, 23, 22, tzinfo=SEOUL)


class MemoryHealth:
    def __init__(self):
        self.state = HealthSnapshot()

    def load(self):
        return self.state

    def save(self, state, now_epoch):
        if state.revision != self.state.revision:
            raise StateError("concurrent update")
        self.state = replace(state, revision=state.revision + 1)
        return self.state


def test_only_threshold_failures_notify_once_then_recovery_once():
    store = MemoryHealth()
    dispatcher = Mock()
    dispatcher.dispatch_message.side_effect = lambda key, *a, **k: AlertOutcome(key, "sent", "ok")
    monitor = HealthMonitor(store, dispatcher, 3)
    for success in [False, False, False, False, False, True, True]:
        monitor.record(success=success, now=NOW, active_until=NOW + timedelta(days=1))
    calls = dispatcher.dispatch_message.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0].endswith(":failure")
    assert calls[1].args[0].endswith(":recovery")
    assert calls[0].args[0].split(":")[-2] == calls[1].args[0].split(":")[-2]
    assert store.state.failures == 0


def test_a_success_breaks_the_failure_streak_and_new_incident_gets_a_new_key():
    store = MemoryHealth()
    dispatcher = Mock()
    dispatcher.dispatch_message.side_effect = lambda key, *a, **k: AlertOutcome(key, "sent", "ok")
    monitor = HealthMonitor(store, dispatcher, 3)
    for success in [False, False, True, False, False]:
        monitor.record(success=success, now=NOW, active_until=NOW + timedelta(days=1))
    dispatcher.dispatch_message.assert_not_called()
    for success in [False, True, False, False, False]:
        monitor.record(success=success, now=NOW, active_until=NOW + timedelta(days=1))
    keys = [call.args[0] for call in dispatcher.dispatch_message.call_args_list]
    assert len(keys) == 3
    assert keys[0] != keys[2]


@pytest.mark.parametrize("status", ["deferred", "state_error"])
def test_pending_recovery_survives_a_transient_delivery_failure(status):
    store = MemoryHealth()
    dispatcher = Mock()
    monitor = HealthMonitor(store, dispatcher, 2)
    dispatcher.dispatch_message.return_value = AlertOutcome("ops", "sent", "ok")
    for _ in range(2):
        monitor.record(success=False, now=NOW, active_until=NOW + timedelta(days=1))
    dispatcher.dispatch_message.return_value = AlertOutcome("ops", status, "retry later")
    monitor.record(success=True, now=NOW, active_until=NOW + timedelta(days=1))
    pending = store.state.pending
    assert pending.kind == "recovery"
    dispatcher.dispatch_message.return_value = AlertOutcome("ops", "sent", "ok")
    monitor.record(success=True, now=NOW, active_until=NOW + timedelta(days=1))
    assert dispatcher.dispatch_message.call_args.args[0] == pending.key
    assert store.state.pending is None


@pytest.mark.parametrize("status", ["uncertain", "failed"])
def test_held_alert_does_not_trigger_fresh_keys_or_unlimited_retries(status):
    store = MemoryHealth()
    dispatcher = Mock()
    dispatcher.dispatch_message.return_value = AlertOutcome("ops", status, "held")
    monitor = HealthMonitor(store, dispatcher, 3)
    for _ in range(8):
        monitor.record(success=False, now=NOW, active_until=NOW + timedelta(days=1))
    dispatcher.dispatch_message.assert_called_once()


def test_waiting_claim_does_not_drop_a_pending_rate_limited_alert():
    store = MemoryHealth()
    dispatcher = Mock()
    dispatcher.dispatch_message.side_effect = [
        AlertOutcome("ops", "deferred", "rate limited"),
        AlertOutcome("ops", "not_claimed", "retry deadline not reached"),
        AlertOutcome("ops", "sent", "ok"),
    ]
    monitor = HealthMonitor(store, dispatcher, 2)
    for _ in range(4):
        monitor.record(success=False, now=NOW, active_until=NOW + timedelta(days=1))
    keys = [call.args[0] for call in dispatcher.dispatch_message.call_args_list]
    assert len(set(keys)) == 1
    assert len(keys) == 3
    assert store.state.pending is None


def test_state_failure_prevents_unclaimed_discord_send():
    store = Mock()
    store.load.side_effect = StateError("storage unavailable")
    dispatcher = Mock()
    with pytest.raises(StateError):
        HealthMonitor(store, dispatcher, 3).record(
            success=False, now=NOW, active_until=NOW + timedelta(days=1)
        )
    dispatcher.dispatch_message.assert_not_called()


def test_store_is_strongly_consistent_and_revision_conditional():
    client = Mock()
    client.get_item.return_value = {}
    store = DynamoDBHealthStore(client, "test-table")
    state = store.load()
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True
    assert client.get_item.call_args.kwargs["Key"] == {"id": {"S": HEALTH_KEY}}
    state = store.save(state, 1000)
    assert "attribute_not_exists" in client.update_item.call_args.kwargs["ConditionExpression"]
    store.save(state, 1060)
    assert client.update_item.call_args.kwargs["ConditionExpression"] == "#revision = :previous"


def test_concurrent_state_update_is_not_silently_overwritten():
    client = Mock()
    client.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "secret"}}, "UpdateItem"
    )
    with pytest.raises(StateError) as error:
        DynamoDBHealthStore(client, "table").save(HealthSnapshot(revision=1), 1000)
    assert "secret" not in str(error.value)


def test_store_round_trips_the_pending_notification():
    state = advance(HealthSnapshot(), success=False, now_epoch=1000, threshold=2)
    state = advance(state, success=False, now_epoch=1060, threshold=2)
    data = asdict(state)
    data.pop("revision")
    client = Mock()
    client.get_item.return_value = {
        "Item": {"payload": {"S": json.dumps(data)}, "revision": {"N": "2"}}
    }
    restored = DynamoDBHealthStore(client, "table").load()
    assert restored.pending == state.pending
    assert "운영 장애" in event_content(restored.pending)
    assert "1970-01-01 09:16:40 KST" in event_content(restored.pending)


@pytest.mark.parametrize(
    "item", [{}, {"payload": {"S": "not json"}}, {"revision": {"N": "1"}, "payload": {"S": "[]"}}]
)
def test_malformed_health_state_is_an_error(item):
    client = Mock()
    client.get_item.return_value = {"Item": item}
    with pytest.raises(StateError):
        DynamoDBHealthStore(client, "table").load()
