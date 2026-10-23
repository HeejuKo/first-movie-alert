"""Inspect and conditionally recover one notification; never send from this tool."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from movie_alert.state import StateError

KEY_PATTERN = re.compile(r"showtime:v1:[0-9a-f]{64}")
FIELDS = ("status", "owner", "lease_until", "attempt_count", "next_retry_at", "expires_at")


class RecoveryClient(Protocol):
    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def update_item(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NotificationSnapshot:
    key: str
    attributes: dict[str, Any]

    @property
    def revision(self) -> str:
        encoded = json.dumps(self.attributes, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    def view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "revision": self.revision,
            **{
                name: self.attributes[name].get("S", self.attributes[name].get("N"))
                for name in FIELDS
            },
        }


class NotificationRecovery:
    def __init__(self, client: RecoveryClient, table: str) -> None:
        self.client = client
        self.table = table

    def inspect(self, key: str) -> NotificationSnapshot:
        if not KEY_PATTERN.fullmatch(key):
            raise ValueError("회차 알림의 showtime:v1: 해시 키만 복구할 수 있습니다.")
        try:
            item = self.client.get_item(
                TableName=self.table, Key={"id": {"S": key}}, ConsistentRead=True
            ).get("Item")
            if item is None:
                raise StateError("해당 회차 상태가 없습니다. 새 항목을 생성하지 않습니다.")
            if item["id"] != {"S": key} or item["status"]["S"] not in {
                "PENDING",
                "SENT",
                "FAILED",
                "UNCERTAIN",
            }:
                raise ValueError
            if not item["owner"]["S"]:
                raise ValueError
            for field in FIELDS[2:]:
                value = item[field]["N"]
                if not isinstance(value, str) or not value.isascii() or not value.isdigit():
                    raise ValueError
            # Only scalar attributes occur in the notification schema and audit fields.
            if not all(
                isinstance(value, dict) and set(value) in ({"S"}, {"N"}) for value in item.values()
            ):
                raise ValueError
            return NotificationSnapshot(key, item)
        except (BotoCoreError, ClientError, KeyError, TypeError, ValueError):
            raise StateError("회차 상태를 읽거나 검증하지 못했습니다.") from None

    def prepare(
        self,
        snapshot: NotificationSnapshot,
        *,
        action: str,
        now_epoch: int,
        operator: str,
        confirm_not_delivered: bool = False,
        accept_duplicate_risk: bool = False,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        item = snapshot.attributes
        status = item["status"]["S"]
        if status == "SENT" or int(item["lease_until"]["N"]) > now_epoch:
            raise ValueError("SENT 또는 유효한 처리 lease가 있는 항목은 변경하지 않습니다.")
        if not operator.strip() or len(operator) > 100:
            raise ValueError("1~100자의 operator가 필요합니다.")
        if action == "retry":
            if not confirm_not_delivered:
                raise ValueError("Discord 미수신 확인 후 --confirm-not-delivered가 필요합니다.")
            if status == "UNCERTAIN" and not accept_duplicate_risk:
                raise ValueError("결과 불명 항목은 --accept-duplicate-risk도 필요합니다.")
            changes = {
                "status": {"S": "PENDING"},
                "owner": {"S": "recovery-" + uuid4().hex},
                "lease_until": {"N": str(now_epoch)},
                "next_retry_at": {"N": str(now_epoch)},
                "attempt_count": {"N": "0"},
            }
        elif action == "mark-sent":
            if not message_id or not message_id.isascii() or not message_id.isdigit():
                raise ValueError("Discord에서 실제 확인한 --message-id가 필요합니다.")
            changes = {"status": {"S": "SENT"}, "confirmed_message_id": {"S": message_id}}
        else:
            raise ValueError("지원하지 않는 복구 작업입니다.")
        return {
            **changes,
            "expires_at": {"N": str(max(int(item["expires_at"]["N"]), now_epoch + 30 * 86400))},
            "recovered_at": {"N": str(now_epoch)},
            "recovery_operator": {"S": operator},
            "recovery_action": {"S": action},
            "recovery_id": {"S": uuid4().hex},
        }

    def apply(
        self, snapshot: NotificationSnapshot, changes: dict[str, Any], *, now_epoch: int
    ) -> None:
        # Compare every observed field, including any previous recovery audit marker.
        names = {f"#old{i}": name for i, name in enumerate(snapshot.attributes)}
        values = {f":old{i}": value for i, value in enumerate(snapshot.attributes.values())}
        conditions = [f"#old{i} = :old{i}" for i in range(len(snapshot.attributes))]
        if "recovery_id" not in snapshot.attributes:
            names["#recovery_id"] = "recovery_id"
            conditions.append("attribute_not_exists(#recovery_id)")
        names["#lease"] = "lease_until"
        values[":now"] = {"N": str(now_epoch)}
        conditions.append("#lease <= :now")
        assignments = []
        for i, (name, value) in enumerate(changes.items()):
            names[f"#new{i}"] = name
            values[f":new{i}"] = value
            assignments.append(f"#new{i} = :new{i}")
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"id": {"S": snapshot.key}},
                UpdateExpression="SET " + ", ".join(assignments),
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except (BotoCoreError, ClientError):
            raise StateError(
                "상태가 변경됐거나 갱신 권한이 없습니다. 다시 inspect하세요."
            ) from None
