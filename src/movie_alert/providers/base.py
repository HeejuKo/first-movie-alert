from datetime import date
from typing import Protocol

from movie_alert.models import Showtime


class ProviderError(RuntimeError):
    pass


class ResponseValidationError(ProviderError):
    pass


class AccessDeniedError(ProviderError):
    pass


class RateLimitError(ProviderError):
    pass


class Provider(Protocol):
    def fetch_showtimes(self, movie_id: str, branch_id: str, show_date: date) -> list[Showtime]:
        """Return a complete, validated schedule or raise ProviderError."""
        ...
