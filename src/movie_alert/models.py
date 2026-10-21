"""Provider-independent screening data."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


class BookingStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Showtime:
    provider: str
    movie_id: str
    movie_name: str
    branch_id: str
    branch_name: str
    screen_id: str
    screen_name: str
    business_date: date
    sequence: str
    starts_at: datetime
    ends_at: datetime
    remaining_seats_hint: int | None
    booking_status: BookingStatus = BookingStatus.UNKNOWN
