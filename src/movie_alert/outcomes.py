"""Notification result values shared by execution and operational reporting."""

from dataclasses import dataclass
from typing import Literal, get_args

AlertStatus = Literal[
    "sent", "not_claimed", "skipped", "deferred", "uncertain", "failed", "state_error"
]
ALERT_STATUSES: tuple[AlertStatus, ...] = get_args(AlertStatus)
FAILURE_STATUSES: frozenset[AlertStatus] = frozenset(
    {"deferred", "uncertain", "failed", "state_error"}
)


@dataclass(frozen=True)
class AlertOutcome:
    key: str
    status: AlertStatus
    detail: str
    message_id: str | None = None

    @property
    def is_failure(self) -> bool:
        return self.status in FAILURE_STATUSES
