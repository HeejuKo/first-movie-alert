"""Persist consecutive failures and one pending operational notification."""

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from movie_alert.models import SEOUL
from movie_alert.outcomes import AlertOutcome
from movie_alert.pipeline import AlertDispatcher
from movie_alert.state import DynamoDBClient, StateError

HEALTH_KEY = "health:v1:checker"


@dataclass(frozen=True)
class HealthEvent:
    incident_id: str
    kind: str
    since: int
    failures: int

    @property
    def key(self) -> str:
        return f"operations:v1:{self.incident_id}:{self.kind}"


@dataclass(frozen=True)
class HealthSnapshot:
    revision: int = 0
    failures: int = 0
    incident_id: str = ""
    since: int = 0
    announced: bool = False
    pending: HealthEvent | None = None


def advance(
    state: HealthSnapshot, *, success: bool, now_epoch: int, threshold: int
) -> HealthSnapshot:
    if success:
        event = state.pending
        if state.failures >= threshold:
            event = HealthEvent(state.incident_id, "recovery", state.since, state.failures)
        return replace(state, failures=0, announced=False, pending=event)
    failures = state.failures + 1
    incident = state.incident_id if state.failures else uuid4().hex
    since = state.since if state.failures else now_epoch
    announced = state.announced if state.failures else False
    pending = state.pending
    if failures >= threshold and not announced:
        if pending is None or pending.incident_id != incident:
            pending = HealthEvent(incident, "failure", since, failures)
    return replace(
        state,
        failures=failures,
        incident_id=incident,
        since=since,
        announced=announced,
        pending=pending,
    )


def event_content(event: HealthEvent) -> str:
    since = datetime.fromtimestamp(event.since, SEOUL)
    if event.kind == "failure":
        return (
            "Movie Alert 운영 장애\n"
            f"조회·알림 처리가 {event.failures}회 연속 실패했습니다.\n"
            f"실패 시작: {since:%Y-%m-%d %H:%M:%S} KST\n"
            "CloudWatch의 실행 실패 유형과 알림 상태를 확인해 주세요."
        )
    return (
        "Movie Alert 운영 복구\n"
        f"연속 실패 {event.failures}회 이후 조회·알림 처리가 정상 완료됐습니다.\n"
        f"장애 시작: {since:%Y-%m-%d %H:%M:%S} KST\n"
        "새 회차 발견 또는 미전송 알림의 복구 완료를 뜻하지는 않습니다."
    )


class DynamoDBHealthStore:
    def __init__(self, client: DynamoDBClient, table: str) -> None:
        self.client = client
        self.table = table

    def load(self) -> HealthSnapshot:
        try:
            item = self.client.get_item(
                TableName=self.table, Key={"id": {"S": HEALTH_KEY}}, ConsistentRead=True
            ).get("Item")
            if item is None:
                return HealthSnapshot()
            values = json.loads(item["payload"]["S"])
            event = values.pop("pending")
            state = HealthSnapshot(
                revision=int(item["revision"]["N"]),
                pending=HealthEvent(**event) if event is not None else None,
                **values,
            )
            if (
                type(state.failures) is not int
                or state.failures < 0
                or state.revision < 1
                or type(state.announced) is not bool
                or type(state.since) is not int
                or not isinstance(state.incident_id, str)
                or (state.incident_id and not re.fullmatch(r"[0-9a-f]{32}", state.incident_id))
                or state.since < 0
                or (
                    state.pending is not None
                    and (
                        state.pending.kind not in {"failure", "recovery"}
                        or type(state.pending.since) is not int
                        or type(state.pending.failures) is not int
                        or not isinstance(state.pending.incident_id, str)
                        or not re.fullmatch(r"[0-9a-f]{32}", state.pending.incident_id)
                        or state.pending.failures < 1
                        or state.pending.since < 0
                    )
                )
            ):
                raise ValueError
            return state
        except (BotoCoreError, ClientError, ValueError, TypeError, KeyError, AttributeError):
            raise StateError("운영 상태를 읽거나 검증하지 못했습니다.") from None

    def save(self, state: HealthSnapshot, now_epoch: int) -> HealthSnapshot:
        data = asdict(state)
        data.pop("revision")
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"id": {"S": HEALTH_KEY}},
                UpdateExpression="SET #payload = :payload, #revision = :next, #expires = :expires",
                ConditionExpression=(
                    "attribute_not_exists(#id)" if state.revision == 0 else "#revision = :previous"
                ),
                ExpressionAttributeNames={
                    "#payload": "payload",
                    "#revision": "revision",
                    "#expires": "expires_at",
                    **({"#id": "id"} if state.revision == 0 else {}),
                },
                ExpressionAttributeValues={
                    ":payload": {"S": json.dumps(data, separators=(",", ":"))},
                    ":next": {"N": str(state.revision + 1)},
                    ":expires": {"N": str(now_epoch + 30 * 86400)},
                    **({":previous": {"N": str(state.revision)}} if state.revision else {}),
                },
            )
        except (BotoCoreError, ClientError):
            raise StateError("운영 상태가 변경됐거나 저장에 실패했습니다.") from None
        return replace(state, revision=state.revision + 1)


class HealthMonitor:
    def __init__(
        self, store: DynamoDBHealthStore, dispatcher: AlertDispatcher, threshold: int
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.threshold = threshold

    def record(
        self, *, success: bool, now: datetime, active_until: datetime
    ) -> AlertOutcome | None:
        state = advance(
            self.store.load(),
            success=success,
            now_epoch=int(now.timestamp()),
            threshold=self.threshold,
        )
        state = self.store.save(state, int(now.timestamp()))
        if state.pending is None:
            return None
        event = state.pending
        outcome = self.dispatcher.dispatch_message(
            event.key, event_content(event), active_until=active_until
        )
        if outcome.status in {"sent", "uncertain", "failed", "skipped"}:
            # A blocked claim can still be waiting for Retry-After or lease expiry.
            # Keep the same pending key until a terminal result or health transition.
            state = replace(
                state, pending=None, announced=state.announced or event.kind == "failure"
            )
            self.store.save(state, int(now.timestamp()))
        return outcome
