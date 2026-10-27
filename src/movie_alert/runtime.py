"""Create notification dependencies only for an explicit live notification run."""

import os
from dataclasses import dataclass, field
from functools import lru_cache

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import BotoCoreError

from movie_alert.config import ConfigError
from movie_alert.health import DynamoDBHealthStore, HealthMonitor
from movie_alert.notifier import DiscordNotifier
from movie_alert.pipeline import AlertDispatcher
from movie_alert.state import DynamoDBClient, DynamoDBPollLock, DynamoDBStateStore


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
    dynamodb = _dynamodb_client(environment.region)
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


def create_poll_lock() -> DynamoDBPollLock:
    table = os.environ.get("MOVIE_ALERT_DYNAMODB_TABLE", "").strip()
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "")).strip()
    if not table or not region:
        raise ConfigError(
            "Lambda 조회 잠금에는 MOVIE_ALERT_DYNAMODB_TABLE과 AWS_REGION이 필요합니다."
        )
    return DynamoDBPollLock(_dynamodb_client(region), table)


def create_health_monitor(
    environment: NotificationEnvironment,
    client: httpx.Client,
    threshold: int,
    recovery_threshold: int = 5,
) -> HealthMonitor:
    # Health reporting must fit in the remaining invocation budget, without SDK retries.
    dynamodb = _dynamodb_client(environment.region, timeout_seconds=2, max_attempts=1)
    return HealthMonitor(
        DynamoDBHealthStore(dynamodb, environment.table_name),
        AlertDispatcher(
            DynamoDBStateStore(dynamodb, environment.table_name),
            DiscordNotifier(client, environment.webhook_url, 5),
        ),
        threshold,
        recovery_threshold,
    )


@lru_cache(maxsize=2)
def _dynamodb_client(
    region: str, *, timeout_seconds: float = 5, max_attempts: int = 2
) -> DynamoDBClient:
    try:
        # Credential refresh creates its own regional Sign-In client from this session.
        session = boto3.Session(region_name=region)
        return session.client(
            "dynamodb",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"mode": "standard", "total_max_attempts": max_attempts},
            ),
        )
    except BotoCoreError:
        raise ConfigError("AWS 리전과 단기 자격 증명 구성을 확인하세요.") from None
