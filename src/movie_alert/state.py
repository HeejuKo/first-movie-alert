"""Atomic notification claims in a pre-existing DynamoDB table."""

from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError


class StateError(RuntimeError):
    """The notification state could not be safely read or changed."""


class StateStore(Protocol):
    def claim(self, key: str, owner: str, now_epoch: int, lease_seconds: int) -> bool: ...

    def ensure_owned(self, key: str, owner: str, now_epoch: int) -> None: ...

    def mark_sent(self, key: str, owner: str, now_epoch: int, retention_seconds: int) -> None: ...

    def mark_uncertain(
        self, key: str, owner: str, now_epoch: int, retention_seconds: int
    ) -> None: ...

    def mark_failed(self, key: str, owner: str, now_epoch: int, retention_seconds: int) -> None: ...

    def release_for_retry(
        self,
        key: str,
        owner: str,
        now_epoch: int,
        retry_after_seconds: int,
        retention_seconds: int,
    ) -> None: ...


class DynamoDBClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...


class DynamoDBPollLock:
    """A separate expiring execution lease; notification records stay untouched."""

    def __init__(self, client: DynamoDBClient, table_name: str) -> None:
        _nonempty_string(table_name)
        self._client = client
        self._table_name = table_name

    def acquire(self, owner: str, now_epoch: int, lease_seconds: int) -> bool:
        _identity("polling-lock:v1", owner, now_epoch)
        _integer(lease_seconds, minimum=1)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"id": {"S": "polling-lock:v1"}},
                UpdateExpression="SET #owner = :owner, #lease = :lease, #expires = :expires",
                ConditionExpression="attribute_not_exists(#id) OR #lease <= :now",
                ExpressionAttributeNames={
                    "#id": "id",
                    "#owner": "owner",
                    "#lease": "lease_until",
                    "#expires": "expires_at",
                },
                ExpressionAttributeValues={
                    ":owner": {"S": owner},
                    ":now": _number(now_epoch),
                    ":lease": _number(now_epoch + lease_seconds),
                    ":expires": _number(now_epoch + lease_seconds + 86400),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise StateError("정기 조회 잠금을 확보하지 못했습니다.") from None
        except BotoCoreError:
            raise StateError("정기 조회 잠금을 확보하지 못했습니다.") from None
        return True

    def release(self, owner: str, *, not_before_epoch: int = 0) -> None:
        _nonempty_string(owner)
        _integer(not_before_epoch, minimum=0)
        names = {"#owner": "owner", "#lease": "lease_until"}
        values = {":owner": {"S": owner}, ":available_at": _number(not_before_epoch)}
        expression = "SET #lease = :available_at"
        if not_before_epoch:
            names["#expires"] = "expires_at"
            values[":expires"] = _number(not_before_epoch + 86400)
            expression += ", #expires = :expires"
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"id": {"S": "polling-lock:v1"}},
                UpdateExpression=expression,
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except (ClientError, BotoCoreError):
            raise StateError("정기 조회 잠금을 안전하게 해제하지 못했습니다.") from None


class DynamoDBStateStore:
    """Use an ``id`` string partition key and ``expires_at`` numeric TTL.

    Terminal SENT/UNCERTAIN/FAILED records block claims until DynamoDB removes them.
    TTL is only cleanup; leases and retry deadlines are checked explicitly.
    The injected low-level client must use bounded network timeouts/retries.
    """

    def __init__(
        self,
        client: DynamoDBClient,
        table_name: str,
        *,
        max_attempts: int = 3,
        pending_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        _nonempty_string(table_name)
        _integer(max_attempts, minimum=1)
        _integer(pending_retention_seconds, minimum=1)
        self._client = client
        self._table_name = table_name
        self._max_attempts = max_attempts
        self._pending_retention_seconds = pending_retention_seconds

    def claim(self, key: str, owner: str, now_epoch: int, lease_seconds: int) -> bool:
        """Acquire an absent or retryable PENDING record with one atomic write."""
        _identity(key, owner, now_epoch)
        _integer(lease_seconds, minimum=1)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"id": {"S": key}},
                UpdateExpression=(
                    "SET #status = :pending, #owner = :owner, #lease = :lease, "
                    "#retry = :now, #attempts = if_not_exists(#attempts, :zero) + :one, "
                    "#expires = :expires"
                ),
                ConditionExpression=(
                    "attribute_not_exists(#id) OR "
                    "(#status = :pending AND #lease <= :now AND #retry <= :now "
                    "AND #attempts < :max_attempts)"
                ),
                ExpressionAttributeNames={
                    "#id": "id",
                    "#status": "status",
                    "#owner": "owner",
                    "#lease": "lease_until",
                    "#retry": "next_retry_at",
                    "#attempts": "attempt_count",
                    "#expires": "expires_at",
                },
                ExpressionAttributeValues={
                    ":pending": {"S": "PENDING"},
                    ":owner": {"S": owner},
                    ":now": _number(now_epoch),
                    ":lease": _number(now_epoch + lease_seconds),
                    ":zero": _number(0),
                    ":one": _number(1),
                    ":max_attempts": _number(self._max_attempts),
                    ":expires": _number(
                        now_epoch + lease_seconds + self._pending_retention_seconds
                    ),
                },
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise StateError("알림 처리 권한을 확보하지 못했습니다.") from None
        except BotoCoreError:
            raise StateError("알림 처리 권한을 확보하지 못했습니다.") from None
        return True

    def ensure_owned(self, key: str, owner: str, now_epoch: int) -> None:
        """Check ownership atomically immediately before the external send.

        This check does not extend the lease. Its configured duration must
        cover the remaining bounded HTTP call and the subsequent state write.
        """
        self._update_owned(
            key,
            owner,
            now_epoch,
            expression="SET #owner = :owner",
        )

    def mark_sent(self, key: str, owner: str, now_epoch: int, retention_seconds: int) -> None:
        """Record a confirmed Discord success while the caller owns the lease."""
        self._mark_terminal(key, owner, now_epoch, retention_seconds, status="SENT")

    def mark_uncertain(self, key: str, owner: str, now_epoch: int, retention_seconds: int) -> None:
        """Block automatic retries when Discord may already have accepted a send."""
        self._mark_terminal(key, owner, now_epoch, retention_seconds, status="UNCERTAIN")

    def mark_failed(self, key: str, owner: str, now_epoch: int, retention_seconds: int) -> None:
        """Keep a terminal failure when retrying the request cannot help."""
        self._mark_terminal(key, owner, now_epoch, retention_seconds, status="FAILED")

    def release_for_retry(
        self,
        key: str,
        owner: str,
        now_epoch: int,
        retry_after_seconds: int,
        retention_seconds: int,
    ) -> None:
        """Release a known failed send, retaining the bounded attempt counter."""
        _integer(retry_after_seconds, minimum=0)
        _integer(retention_seconds, minimum=1)
        if retention_seconds <= retry_after_seconds:
            raise ValueError("상태 보관 기간은 재시도 대기 시간보다 길어야 합니다.")
        self._update_owned(
            key,
            owner,
            now_epoch,
            expression="SET #lease = :now, #retry = :retry, #expires = :expires",
            names={"#retry": "next_retry_at", "#expires": "expires_at"},
            values={
                ":retry": _number(now_epoch + retry_after_seconds),
                ":expires": _number(now_epoch + retention_seconds),
            },
        )

    def _mark_terminal(
        self,
        key: str,
        owner: str,
        now_epoch: int,
        retention_seconds: int,
        *,
        status: str,
    ) -> None:
        _integer(retention_seconds, minimum=1)
        self._update_owned(
            key,
            owner,
            now_epoch,
            expression="SET #status = :terminal, #lease = :now, #expires = :expires",
            names={"#expires": "expires_at"},
            values={
                ":terminal": {"S": status},
                ":expires": _number(now_epoch + retention_seconds),
            },
        )

    def _update_owned(
        self,
        key: str,
        owner: str,
        now_epoch: int,
        *,
        expression: str,
        names: dict[str, str] | None = None,
        values: dict[str, dict[str, str]] | None = None,
    ) -> None:
        _identity(key, owner, now_epoch)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key={"id": {"S": key}},
                UpdateExpression=expression,
                ConditionExpression="#status = :pending AND #owner = :owner AND #lease > :now",
                ExpressionAttributeNames={
                    "#status": "status",
                    "#owner": "owner",
                    "#lease": "lease_until",
                    **(names or {}),
                },
                ExpressionAttributeValues={
                    ":pending": {"S": "PENDING"},
                    ":owner": {"S": owner},
                    ":now": _number(now_epoch),
                    **(values or {}),
                },
            )
        except (BotoCoreError, ClientError):
            raise StateError("알림 상태를 안전하게 갱신하지 못했습니다.") from None


def _identity(key: str, owner: str, now_epoch: int) -> None:
    _nonempty_string(key)
    _nonempty_string(owner)
    _integer(now_epoch, minimum=0)
    if len(key.encode("utf-8")) > 2048:
        raise ValueError("알림 상태 키가 DynamoDB 제한을 초과합니다.")


def _nonempty_string(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("상태 저장소의 문자열 설정은 비어 있을 수 없습니다.")


def _integer(value: int, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("상태 저장소의 시간과 횟수는 유효한 정수여야 합니다.")


def _number(value: int) -> dict[str, str]:
    return {"N": str(value)}
