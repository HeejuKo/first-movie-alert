"""Korean local-time polling windows, independent of screening dates."""

from datetime import datetime, timedelta

from movie_alert.config import PollingSettings
from movie_alert.models import SEOUL


def polling_allowed(now: datetime, settings: PollingSettings) -> bool:
    if now.utcoffset() is None:
        raise ValueError("now에는 시간대가 필요합니다.")
    return settings.start_hour <= now.astimezone(SEOUL).hour < settings.end_hour


def next_polling_start(now: datetime, settings: PollingSettings) -> datetime:
    if now.utcoffset() is None:
        raise ValueError("now에는 시간대가 필요합니다.")
    tomorrow = now.astimezone(SEOUL) + timedelta(days=1)
    return tomorrow.replace(hour=settings.start_hour, minute=0, second=0, microsecond=0)
