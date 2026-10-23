import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from movie_alert.recovery import NotificationRecovery
from movie_alert.state import StateError

KEY = "showtime:v1:" + "a" * 64


@pytest.fixture
def recovery():
    client = Mock()
    client.get_item.return_value = {
        "Item": {
            "id": {"S": KEY},
            "status": {"S": "FAILED"},
            "owner": {"S": "owner-1"},
            "lease_until": {"N": "900"},
            "attempt_count": {"N": "3"},
            "next_retry_at": {"N": "950"},
            "expires_at": {"N": "9999999"},
        }
    }
    return NotificationRecovery(client, "test-table")


def test_inspect_never_writes_and_rejects_non_notification_keys(recovery):
    view = recovery.inspect(KEY).view()
    assert view["status"] == "FAILED"
    assert len(view["revision"]) == 64
    assert recovery.client.get_item.call_args.kwargs["ConsistentRead"] is True
    recovery.client.update_item.assert_not_called()
    for key in ["polling-lock:v1", "health:v1:checker", "operations:v1:example"]:
        with pytest.raises(ValueError):
            recovery.inspect(key)


@pytest.mark.parametrize("status,lease", [("SENT", "900"), ("PENDING", "1001")])
def test_completed_or_live_leases_are_never_recovered(recovery, status, lease):
    item = recovery.client.get_item.return_value["Item"]
    item["status"]["S"] = status
    item["lease_until"]["N"] = lease
    with pytest.raises(ValueError):
        recovery.prepare(
            recovery.inspect(KEY),
            action="retry",
            now_epoch=1000,
            operator="test",
            confirm_not_delivered=True,
        )
    recovery.client.update_item.assert_not_called()


def test_uncertain_retry_requires_explicit_duplicate_risk_acceptance(recovery):
    recovery.client.get_item.return_value["Item"]["status"]["S"] = "UNCERTAIN"
    snapshot = recovery.inspect(KEY)
    with pytest.raises(ValueError, match="accept-duplicate-risk"):
        recovery.prepare(
            snapshot, action="retry", now_epoch=1000, operator="test", confirm_not_delivered=True
        )
    changes = recovery.prepare(
        snapshot,
        action="retry",
        now_epoch=1000,
        operator="test",
        confirm_not_delivered=True,
        accept_duplicate_risk=True,
    )
    assert changes["status"] == {"S": "PENDING"}
    assert changes["attempt_count"] == {"N": "0"}


def test_retry_requires_delivery_check_and_does_not_shorten_retention(recovery):
    snapshot = recovery.inspect(KEY)
    with pytest.raises(ValueError, match="confirm-not-delivered"):
        recovery.prepare(snapshot, action="retry", now_epoch=1000, operator="test")
    changes = recovery.prepare(
        snapshot, action="retry", now_epoch=1000, operator="test", confirm_not_delivered=True
    )
    assert changes["expires_at"] == snapshot.attributes["expires_at"]
    assert changes["owner"] != snapshot.attributes["owner"]


def test_mark_sent_requires_actual_message_id_and_does_not_send(recovery):
    snapshot = recovery.inspect(KEY)
    with pytest.raises(ValueError):
        recovery.prepare(snapshot, action="mark-sent", now_epoch=1000, operator="test")
    changes = recovery.prepare(
        snapshot,
        action="mark-sent",
        now_epoch=1000,
        operator="test",
        message_id="1234567890123456789",
    )
    recovery.apply(snapshot, changes, now_epoch=1000)
    call = recovery.client.update_item.call_args.kwargs
    assert "#lease <= :now" in call["ConditionExpression"]
    assert "attribute_not_exists(#recovery_id)" in call["ConditionExpression"]
    assert all(name in call["ExpressionAttributeNames"].values() for name in snapshot.attributes)
    assert "Delete" not in call["UpdateExpression"]


def test_concurrent_writer_cancels_recovery_without_unconditional_fallback(recovery):
    snapshot = recovery.inspect(KEY)
    changes = recovery.prepare(
        snapshot,
        action="mark-sent",
        now_epoch=1000,
        operator="test",
        message_id="1234567890123456789",
    )
    recovery.client.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "secret"}}, "UpdateItem"
    )
    with pytest.raises(StateError) as error:
        recovery.apply(snapshot, changes, now_epoch=1000)
    assert "secret" not in str(error.value)
    recovery.client.update_item.assert_called_once()


def test_cli_mutation_defaults_to_dry_run(monkeypatch, recovery, capsys):
    spec = importlib.util.spec_from_file_location(
        "recover_cli", Path(__file__).parents[1] / "scripts/recover_notification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.boto3, "Session", Mock(return_value=Mock(client=Mock(return_value=recovery.client)))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["recover", "retry", "--key", KEY, "--operator", "test", "--confirm-not-delivered"],
    )
    module.main()
    assert '"mode": "dry-run"' in capsys.readouterr().out
    recovery.client.update_item.assert_not_called()


def test_cli_apply_requires_revision_before_creating_aws_client(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "recover_cli", Path(__file__).parents[1] / "scripts/recover_notification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = Mock()
    monkeypatch.setattr(module.boto3, "Session", factory)
    monkeypatch.setattr(sys, "argv", ["recover", "retry", "--key", KEY, "--apply"])
    with pytest.raises(SystemExit):
        module.main()
    factory.assert_not_called()


def test_cli_rejects_a_stale_revision_without_writing(monkeypatch, recovery):
    spec = importlib.util.spec_from_file_location(
        "recover_cli", Path(__file__).parents[1] / "scripts/recover_notification.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.boto3, "Session", Mock(return_value=Mock(client=Mock(return_value=recovery.client)))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "recover",
            "retry",
            "--key",
            KEY,
            "--apply",
            "--expected-revision",
            "stale",
            "--operator",
            "test",
            "--confirm-not-delivered",
        ],
    )
    with pytest.raises(SystemExit):
        module.main()
    recovery.client.update_item.assert_not_called()
