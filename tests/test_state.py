import re
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from movie_alert.state import DynamoDBPollLock, DynamoDBStateStore, StateError


@pytest.fixture
def client():
    return Mock(spec=["update_item"])


@pytest.fixture
def state(client):
    return DynamoDBStateStore(client, "movie-alert-state", pending_retention_seconds=700)


def conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "private-value"}},
        "UpdateItem",
    )


def test_poll_lock_uses_separate_key_and_expiry_condition(client):
    lock = DynamoDBPollLock(client, "movie-alert-state")
    assert lock.acquire("request-1", 1000, 240)
    request = client.update_item.call_args.kwargs
    assert request["Key"] == {"id": {"S": "polling-lock:v1"}}
    assert request["ConditionExpression"] == "attribute_not_exists(#id) OR #lease <= :now"
    assert request["ExpressionAttributeValues"][":lease"] == {"N": "1240"}
    assert "status" not in request["ExpressionAttributeNames"].values()
    assert "#expires" not in request["ConditionExpression"]


def test_competing_poll_skips_and_release_is_owner_conditional(client):
    lock = DynamoDBPollLock(client, "movie-alert-state")
    client.update_item.side_effect = [{}, conditional_failure(), {}]
    assert lock.acquire("request-1", 1000, 240)
    assert not lock.acquire("request-2", 1000, 240)
    lock.release("request-1")
    request = client.update_item.call_args.kwargs
    assert request["ConditionExpression"] == "#owner = :owner"
    assert request["ExpressionAttributeValues"][":owner"] == {"S": "request-1"}
    assert request["ExpressionAttributeValues"][":zero"] == {"N": "0"}


@pytest.mark.parametrize("operation", ["acquire", "release"])
def test_poll_storage_errors_are_not_treated_as_busy(client, operation):
    lock = DynamoDBPollLock(client, "movie-alert-state")
    client.update_item.side_effect = EndpointConnectionError(
        endpoint_url="https://private-value.example"
    )
    with pytest.raises(StateError) as error:
        if operation == "acquire":
            lock.acquire("request-1", 1000, 240)
        else:
            lock.release("request-1")
    assert "private-value" not in str(error.value)


def test_claim_is_one_conditional_write_and_sets_lease_attempt_and_cleanup_deadlines(state, client):
    assert state.claim("screening-key", "owner-1", 1000, 60)
    client.update_item.assert_called_once()
    request = client.update_item.call_args.kwargs
    assert request["TableName"] == "movie-alert-state"
    assert request["Key"] == {"id": {"S": "screening-key"}}
    assert request["ExpressionAttributeValues"][":lease"] == {"N": "1060"}
    assert request["ExpressionAttributeValues"][":expires"] == {"N": "1760"}
    assert request["ExpressionAttributeValues"][":pending"] == {"S": "PENDING"}
    assert request["ExpressionAttributeValues"][":owner"] == {"S": "owner-1"}
    assert "if_not_exists(#attempts, :zero) + :one" in request["UpdateExpression"]


def test_claim_recovery_requires_pending_expired_lease_retry_deadline_and_attempt_budget(
    state, client
):
    state.claim("screening-key", "owner-1", 1000, 60)
    request = client.update_item.call_args.kwargs
    condition = request["ConditionExpression"]
    assert "attribute_not_exists(#id) OR (" in condition
    assert "#status = :pending" in condition
    assert "#lease <= :now" in condition
    assert "#retry <= :now" in condition
    assert "#attempts < :max_attempts" in condition
    assert request["ExpressionAttributeValues"][":max_attempts"] == {"N": "3"}
    assert request["ExpressionAttributeNames"]["#lease"] == "lease_until"
    assert request["ExpressionAttributeNames"]["#retry"] == "next_retry_at"
    assert request["ExpressionAttributeNames"]["#attempts"] == "attempt_count"
    # TTL cleanup is asynchronous; it must never grant a second claim.
    assert "#expires" not in condition
    assert "SENT" not in condition
    assert "UNCERTAIN" not in condition
    assert "FAILED" not in condition


def test_retry_limit_is_configurable(client):
    state = DynamoDBStateStore(client, "movie-alert-state", max_attempts=1)
    state.claim("key", "owner", 1000, 60)
    assert client.update_item.call_args.kwargs["ExpressionAttributeValues"][":max_attempts"] == {
        "N": "1"
    }


def test_competing_claim_is_skipped_without_unconditional_fallback(state, client):
    client.update_item.side_effect = [{}, conditional_failure()]
    assert state.claim("same-key", "owner-1", 1000, 60)
    assert not state.claim("same-key", "owner-2", 1000, 60)
    assert client.update_item.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "private-value"}}, "x"),
        ClientError(
            {
                "Error": {
                    "Code": "ProvisionedThroughputExceededException",
                    "Message": "private-value",
                }
            },
            "x",
        ),
        EndpointConnectionError(endpoint_url="https://private-value.example"),
        NoCredentialsError(),
    ],
)
def test_claim_storage_failures_are_safe_errors_and_never_skipped(state, client, error):
    client.update_item.side_effect = error
    with pytest.raises(StateError, match="권한을 확보하지 못했습니다") as raised:
        state.claim("key", "owner", 1000, 60)
    assert "private-value" not in str(raised.value)
    assert raised.value.__suppress_context__
    client.update_item.assert_called_once()


@pytest.mark.parametrize(
    ("method", "args", "terminal"),
    [
        ("ensure_owned", (), None),
        ("mark_sent", (600,), "SENT"),
        ("mark_uncertain", (600,), "UNCERTAIN"),
        ("mark_failed", (600,), "FAILED"),
        ("release_for_retry", (30, 600), None),
    ],
)
def test_every_mutation_checks_current_pending_owner_and_unexpired_lease(
    state, client, method, args, terminal
):
    getattr(state, method)("key", "owner-1", 1000, *args)
    request = client.update_item.call_args.kwargs
    condition = request["ConditionExpression"]
    assert "#status = :pending" in condition
    assert "#owner = :owner" in condition
    assert "#lease > :now" in condition
    assert request["ExpressionAttributeValues"][":pending"] == {"S": "PENDING"}
    assert request["ExpressionAttributeValues"][":owner"] == {"S": "owner-1"}
    if terminal:
        assert request["ExpressionAttributeValues"][":terminal"] == {"S": terminal}
        assert request["ExpressionAttributeValues"][":expires"] == {"N": "1600"}


def test_known_failure_releases_lease_and_preserves_attempt_counter(state, client):
    state.release_for_retry("key", "owner-1", 1000, 30, 600)
    request = client.update_item.call_args.kwargs
    assert "#lease = :now" in request["UpdateExpression"]
    assert request["ExpressionAttributeValues"][":retry"] == {"N": "1030"}
    assert request["ExpressionAttributeValues"][":expires"] == {"N": "1600"}
    assert "attempt" not in request["UpdateExpression"]


def test_ownership_check_does_not_extend_the_lease(state, client):
    state.ensure_owned("key", "owner-1", 1000)
    assert "lease" not in client.update_item.call_args.kwargs["UpdateExpression"]


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("claim", (60,)),
        ("ensure_owned", ()),
        ("mark_sent", (600,)),
        ("mark_uncertain", (600,)),
        ("mark_failed", (600,)),
        ("release_for_retry", (30, 600)),
    ],
)
def test_expression_bindings_have_no_missing_or_unused_placeholders(state, client, method, args):
    getattr(state, method)("key", "owner", 1000, *args)
    request = client.update_item.call_args.kwargs
    expressions = request["ConditionExpression"] + " " + request["UpdateExpression"]
    assert set(re.findall(r"#\w+", expressions)) == set(request["ExpressionAttributeNames"])
    assert set(re.findall(r":\w+", expressions)) == set(request["ExpressionAttributeValues"])


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("ensure_owned", ()),
        ("mark_sent", (600,)),
        ("mark_uncertain", (600,)),
        ("mark_failed", (600,)),
        ("release_for_retry", (30, 600)),
    ],
)
@pytest.mark.parametrize("conditional", [True, False])
def test_lost_ownership_or_storage_failure_never_reports_a_successful_update(
    state, client, method, args, conditional
):
    client.update_item.side_effect = (
        conditional_failure()
        if conditional
        else EndpointConnectionError(endpoint_url="https://private-value.example")
    )
    with pytest.raises(StateError, match="안전하게") as raised:
        getattr(state, method)("key", "owner-1", 1000, *args)
    assert "private-value" not in str(raised.value)
    assert raised.value.__suppress_context__
    client.update_item.assert_called_once()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"table_name": ""},
        {"max_attempts": 0},
        {"max_attempts": True},
        {"pending_retention_seconds": 0},
        {"pending_retention_seconds": 1.5},
    ],
)
def test_invalid_store_settings_fail_before_any_aws_request(client, kwargs):
    settings = {"table_name": "movie-alert-state", **kwargs}
    with pytest.raises(ValueError):
        DynamoDBStateStore(client, **settings)
    client.update_item.assert_not_called()


@pytest.mark.parametrize(
    ("key", "owner", "now", "lease"),
    [
        ("", "owner", 1000, 60),
        ("key", "", 1000, 60),
        ("key", "owner", -1, 60),
        ("key", "owner", True, 60),
        ("key", "owner", 1000, 0),
        ("key", "owner", 1000, 1.5),
        ("한" * 700, "owner", 1000, 60),
    ],
)
def test_invalid_claim_fails_before_any_aws_request(state, client, key, owner, now, lease):
    with pytest.raises(ValueError):
        state.claim(key, owner, now, lease)
    client.update_item.assert_not_called()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("mark_sent", (0,)),
        ("mark_uncertain", (True,)),
        ("mark_failed", (-1,)),
        ("release_for_retry", (-1, 600)),
        ("release_for_retry", (30, 30)),
        ("release_for_retry", (60, 30)),
    ],
)
def test_invalid_retention_and_retry_settings_do_not_write(state, client, method, args):
    with pytest.raises(ValueError):
        getattr(state, method)("key", "owner", 1000, *args)
    client.update_item.assert_not_called()
