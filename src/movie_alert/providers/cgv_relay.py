"""CGV 시간표를 공개 제3자 릴레이의 정규화된 응답에서 읽는 Provider."""

import logging
import re
import socket
import ssl
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from functools import partial
from typing import Any

import httpx

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.models import SEOUL, Showtime
from movie_alert.providers.base import (
    AccessDeniedError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ResponseValidationError,
)
from movie_alert.providers.common import (
    RETRYABLE_STATUSES,
    get_response,
    parse_business_date,
    required_text,
    retry_after_seconds,
)

TIMETABLE_PATH = "/api/cgv/timetable"
MOVIES_PATH = "/api/cgv/movies"
_text = partial(required_text, source="CGV 릴레이")
_business_date = partial(parse_business_date, field="CGV 릴레이 playDate")
logger = logging.getLogger(__name__)


def relay_request_limit(max_retries: int) -> int:
    """Count movie-list fallback and timetable retries in the same request budget."""
    return max(2, max_retries + 1)


def _unavailable(response: httpx.Response, operation: str) -> ProviderUnavailableError:
    code = ""
    try:
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        value = error.get("code") if isinstance(error, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
            code = f" code={value}"
    except ValueError:
        pass
    return ProviderUnavailableError(
        f"CGV 릴레이 {operation} 조회에 실패했습니다(HTTP {response.status_code}).{code}",
        retry_after_seconds=retry_after_seconds(response),
    )


def _network_error(
    operation: str, error: httpx.RequestError, elapsed: float
) -> ProviderUnavailableError:
    """Describe the failed phase without exposing raw exception URLs or credentials."""
    code = type(error).__name__
    if isinstance(error, httpx.ConnectTimeout):
        reason = "연결 시간 초과. 현재 네트워크의 연결 경로·방화벽을 확인하세요."
    elif isinstance(error, httpx.ReadTimeout):
        reason = "응답 대기 시간 초과. 릴레이 또는 중계 경로의 응답이 지연됐습니다."
    elif isinstance(error, httpx.WriteTimeout):
        reason = "요청 전송 시간 초과. 현재 네트워크의 연결 상태를 확인하세요."
    elif isinstance(error, httpx.PoolTimeout):
        reason = "HTTP 연결 풀 대기 시간 초과. 동시 요청 수와 연결 풀을 확인하세요."
    elif isinstance(error, httpx.ProxyError):
        reason = "프록시 연결 실패. HTTP_PROXY·HTTPS_PROXY 등 프록시 설정을 확인하세요."
    elif isinstance(error, httpx.ConnectError):
        cause: BaseException | None = error
        reason = "연결에 실패했습니다. DNS·TLS·방화벽·프록시 상태를 확인하세요."
        while cause is not None:
            if isinstance(cause, socket.gaierror):
                code = "DNSLookupError"
                reason = "DNS 이름 조회 실패. 현재 네트워크의 DNS 설정을 확인하세요."
                break
            if isinstance(cause, ssl.SSLCertVerificationError):
                code = "TLSCertificateError"
                reason = "TLS 인증서 검증 실패. 네트워크 인증서·CA 설정을 확인하세요."
                break
            cause = cause.__cause__
    else:
        reason = "통신에 실패했습니다. 현재 네트워크와 릴레이 상태를 확인하세요."
    return ProviderUnavailableError(f"CGV 릴레이 {operation}: {reason} [{code}, {elapsed:.2f}초]")


def _screening_time(day: date, value: str, field: str) -> datetime:
    parts = value.split(":")
    if (
        len(parts) != 2
        or not all(part.isascii() and part.isdigit() for part in parts)
        or int(parts[1]) >= 60
    ):
        raise ResponseValidationError(f"CGV 릴레이 {field}: HH:MM 형식이 필요합니다.")
    return datetime.combine(day, datetime_time(), SEOUL) + timedelta(
        hours=int(parts[0]), minutes=int(parts[1])
    )


def _seat_count(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ResponseValidationError(f"CGV 릴레이 필드 {key}: 음수가 아닌 정수가 필요합니다.")
    return value


def parse_relay_schedule(
    payload: object,
    *,
    movie_id: str,
    branch_id: str,
    show_date: date,
) -> list[Showtime]:
    """릴레이 응답을 공통 Showtime 모델로 변환한다."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ResponseValidationError("CGV 릴레이 응답이 성공 상태가 아닙니다.")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("timetable"), list):
        raise ResponseValidationError("CGV 릴레이 data.timetable은 목록이어야 합니다.")

    showtimes = []
    for row in data["timetable"]:
        if not isinstance(row, dict):
            raise ResponseValidationError("CGV 릴레이 시간표의 각 회차는 객체여야 합니다.")
        row_movie_id = _text(row, "movieCode")
        row_branch_id = _text(row, "theaterCode")
        row_date = _business_date(_text(row, "playDate"))
        if row_movie_id != movie_id or row_branch_id != branch_id or row_date != show_date:
            raise ResponseValidationError("CGV 릴레이 응답의 영화·지점·상영일이 요청과 다릅니다.")
        start = _screening_time(show_date, _text(row, "startTime"), "startTime")
        end = _screening_time(show_date, _text(row, "endTime"), "endTime")
        if end < start:
            raise ResponseValidationError("CGV 릴레이 종료 시각이 시작 시각보다 빠릅니다.")
        schedule_id = _text(row, "scheduleId")
        showtimes.append(
            Showtime(
                provider="cgv",
                movie_id=row_movie_id,
                movie_name=_text(row, "movieName"),
                branch_id=row_branch_id,
                branch_name=_text(row, "theaterName"),
                # 릴레이 응답에는 상영관 정보가 없으므로 scheduleId로 회차 식별자를 유지한다.
                screen_id=schedule_id,
                screen_name="상영관 정보 없음",
                business_date=row_date,
                sequence=schedule_id,
                starts_at=start,
                ends_at=end,
                remaining_seats_hint=_seat_count(row, "remainingSeats"),
            )
        )
    return showtimes


class CgvRelayProvider:
    def __init__(
        self,
        client: httpx.Client,
        settings: CgvSettings,
        http: HttpSettings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.settings = settings
        self.http = http
        self.sleep = sleep

    def _movie_is_listed(self, movie_id: str, branch_id: str, show_date: date) -> bool:
        """Cross-check a failed timetable without hiding errors as empty results."""
        started = time.monotonic()
        try:
            response = get_response(
                self.client,
                f"{self.settings.relay_base_url}{MOVIES_PATH}",
                params={
                    "playDate": show_date.strftime("%Y%m%d"),
                    "theaterCode": branch_id,
                },
                timeout_seconds=self.http.timeout_seconds,
            )
        except httpx.RequestError as error:
            raise _network_error(
                "영화 목록 /api/cgv/movies", error, time.monotonic() - started
            ) from None
        if response.status_code in (401, 403):
            raise AccessDeniedError(f"CGV 릴레이 접근 제한(HTTP {response.status_code})입니다.")
        if response.status_code == 429:
            raise RateLimitError(
                "CGV 릴레이 요청 제한(HTTP 429)입니다.",
                retry_after_seconds=retry_after_seconds(response),
            )
        if 500 <= response.status_code < 600:
            raise _unavailable(response, "영화 목록")
        if response.status_code != 200:
            raise ProviderError(
                f"CGV 릴레이 영화 목록 조회에 실패했습니다(HTTP {response.status_code})."
            )
        try:
            payload = response.json()
        except ValueError:
            raise ResponseValidationError(
                "CGV 릴레이 영화 목록 응답이 유효한 JSON이 아닙니다."
            ) from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise ResponseValidationError("CGV 릴레이 영화 목록 응답이 성공 상태가 아닙니다.")
        data = payload.get("data")
        movies = data.get("movies") if isinstance(data, dict) else None
        if not isinstance(movies, list):
            raise ResponseValidationError("CGV 릴레이 data.movies는 목록이어야 합니다.")
        if data.get("playDate") != show_date.strftime("%Y%m%d"):
            raise ResponseValidationError("CGV 릴레이 영화 목록의 상영일이 요청과 다릅니다.")
        filters = data.get("filters")
        if not isinstance(filters, dict) or filters.get("theaterCode") != branch_id:
            raise ResponseValidationError("CGV 릴레이 영화 목록의 지점이 요청과 다릅니다.")
        movie_ids = set()
        for movie in movies:
            if not isinstance(movie, dict):
                raise ResponseValidationError("CGV 릴레이 영화 목록의 각 항목은 객체여야 합니다.")
            movie_ids.add(_text(movie, "movieCode"))
        if movie_id not in movie_ids:
            logger.info(
                "CGV 릴레이 영화 목록에 대상 영화 없음: 지점=%s · %s · 영화=%s",
                branch_id,
                show_date,
                movie_id,
            )
        return movie_id in movie_ids

    def fetch_showtimes(self, movie_id: str, branch_id: str, show_date: date) -> list[Showtime]:
        url = f"{self.settings.relay_base_url}{TIMETABLE_PATH}"
        params = {
            "playDate": show_date.strftime("%Y%m%d"),
            "theaterCode": branch_id,
            "movieCode": movie_id,
            "limit": "50",
        }
        remaining_requests = relay_request_limit(self.http.max_retries)
        for attempt in range(self.http.max_retries + 1):
            remaining_requests -= 1
            started = time.monotonic()
            try:
                response = get_response(
                    self.client,
                    url,
                    params=params,
                    timeout_seconds=self.http.timeout_seconds,
                )
            except httpx.RequestError as error:
                elapsed = time.monotonic() - started
                if isinstance(error, httpx.ReadTimeout) and remaining_requests:
                    logger.warning(
                        "CGV 릴레이 시간표 응답 대기 시간 초과 [ReadTimeout, %.2f초]: "
                        "지점=%s · %s; 영화 목록 확인",
                        elapsed,
                        branch_id,
                        show_date,
                    )
                    remaining_requests -= 1
                    if not self._movie_is_listed(movie_id, branch_id, show_date):
                        return []
                if attempt == self.http.max_retries or not remaining_requests:
                    raise _network_error("시간표 /api/cgv/timetable", error, elapsed) from None
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise AccessDeniedError(f"CGV 릴레이 접근 제한(HTTP {response.status_code})입니다.")
            if response.status_code == 429:
                raise RateLimitError(
                    "CGV 릴레이 요청 제한(HTTP 429)입니다.",
                    retry_after_seconds=retry_after_seconds(response),
                )
            if 500 <= response.status_code < 600 and retry_after_seconds(response):
                raise _unavailable(response, "시간표")
            if response.status_code == 502:
                if remaining_requests:
                    remaining_requests -= 1
                    if not self._movie_is_listed(movie_id, branch_id, show_date):
                        return []
                if attempt < self.http.max_retries and remaining_requests:
                    self.sleep(self.http.retry_delay_seconds * 2**attempt)
                    continue
                raise _unavailable(response, "시간표")
            if (
                response.status_code in RETRYABLE_STATUSES
                and attempt < self.http.max_retries
                and remaining_requests
            ):
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
            if 500 <= response.status_code < 600:
                raise _unavailable(response, "시간표")
            if response.status_code != 200:
                raise ProviderError(
                    f"CGV 릴레이 시간표 조회에 실패했습니다(HTTP {response.status_code})."
                )
            try:
                payload = response.json()
            except ValueError:
                raise ResponseValidationError("CGV 릴레이 응답이 유효한 JSON이 아닙니다.") from None
            return parse_relay_schedule(
                payload, movie_id=movie_id, branch_id=branch_id, show_date=show_date
            )
        raise ProviderUnavailableError("CGV 릴레이 시간표 조회를 완료하지 못했습니다.")
