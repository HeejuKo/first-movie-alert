"""Create notification dependencies only for an explicit live notification run."""

import os
from dataclasses import dataclass, field

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError

from movie_alert.config import ConfigError
from movie_alert.notifier import DiscordNotifier
from movie_alert.pipeline import AlertDispatcher
from movie_alert.state import DynamoDBStateStore


@dataclass(frozen=True)
class NotificationEnvironment:
    webhook_url: str = field(repr=False)
    table_name: str
    region: str


def notification_environment() -> NotificationEnvironment:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    table = os.environ.get("MOVIE_ALERT_DYNAMODB_TABLE", "").strip()
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
    if not webhook or not table or not region:
        raise ConfigError(
            "실제 알림에는 DISCORD_WEBHOOK_URL, MOVIE_ALERT_DYNAMODB_TABLE, "
            "AWS_REGION이 필요합니다."
        )
    return NotificationEnvironment(webhook, table, region)


def create_dispatcher(
    environment: NotificationEnvironment,
    client: httpx.Client,
    timeout_seconds: float,
    retention_seconds: int,
) -> AlertDispatcher:
    notifier = DiscordNotifier(client, environment.webhook_url, timeout_seconds)
    try:
        dynamodb = boto3.client(
            "dynamodb",
            region_name=environment.region,
            config=Config(
                connect_timeout=5,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
    except BotoCoreError:
        raise ConfigError("AWS 리전과 단기 자격 증명 구성을 확인하세요.") from None
    state = DynamoDBStateStore(
        dynamodb,
        environment.table_name,
        pending_retention_seconds=retention_seconds,
    )
    return AlertDispatcher(
        state,
        notifier,
        lease_seconds=max(120, int(timeout_seconds) + 90),
        retention_seconds=retention_seconds,
    )
