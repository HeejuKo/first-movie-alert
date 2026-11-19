from datetime import date
from typing import Protocol

from movie_alert.models import Showtime


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: int = 0) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ResponseValidationError(ProviderError):
    pass


class AccessDeniedError(ProviderError):
    pass


class RateLimitError(ProviderError):
    pass


class ProviderUnavailableError(ProviderError):
    """The provider service is unavailable, so related requests should stop."""


class PollingPausedError(ProviderError):
    """An intentional polling pause, rather than a failed provider query."""


class DailyQuotaExceeded(PollingPausedError):
    """No further relay requests are allowed during the current Korean day."""


class Provider(Protocol):
    def fetch_showtimes(self, movie_id: str, branch_id: str, show_date: date) -> list[Showtime]:
        """Return a complete, validated schedule or raise ProviderError."""
        ...
