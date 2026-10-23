"""Shared response validation and request settings for cinema providers."""

from datetime import date
from typing import Any

import httpx

from movie_alert.providers.base import ResponseValidationError

RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


def required_text(row: dict[str, Any], key: str, *, source: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResponseValidationError(f"{source} 필드 {key}: 비어 있지 않은 문자열이 필요합니다.")
    return value.strip()


def parse_business_date(value: str, *, field: str) -> date:
    if len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ResponseValidationError(f"{field}: YYYYMMDD 형식이 필요합니다.")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        raise ResponseValidationError(f"{field}: 유효하지 않은 날짜입니다.") from None


def get_response(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str],
    timeout_seconds: float,
) -> httpx.Response:
    """Send one JSON GET request; providers own response validation and retries."""
    return client.get(
        url,
        params=params,
        headers={"Accept": "application/json", "User-Agent": "movie-alert/0.1"},
        timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
        follow_redirects=False,
    )
