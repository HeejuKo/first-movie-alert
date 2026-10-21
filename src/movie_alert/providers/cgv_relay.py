"""CGV 시간표를 공개 제3자 릴레이의 정규화된 응답에서 읽는 Provider."""

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any

import httpx

from movie_alert.config import CgvSettings, HttpSettings
from movie_alert.models import SEOUL, Showtime
from movie_alert.providers.base import (
    AccessDeniedError,
    ProviderError,
    RateLimitError,
    ResponseValidationError,
)

TIMETABLE_PATH = "/api/cgv/timetable"
MOVIES_PATH = "/api/cgv/movies"
RETRYABLE_STATUSES = {500, 502, 503, 504}


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResponseValidationError(f"CGV 릴레이 필드 {key}: 비어 있지 않은 문자열이 필요합니다.")
    return value.strip()


def _business_date(value: str) -> date:
    if len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ResponseValidationError("CGV 릴레이 playDate: YYYYMMDD 형식이 필요합니다.")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        raise ResponseValidationError("CGV 릴레이 playDate: 유효하지 않은 날짜입니다.") from None


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
        """502가 영화 미공개인지 릴레이 장애인지 구분하기 위해 영화 목록을 확인한다."""
        try:
            response = self.client.get(
                f"{self.settings.relay_base_url}{MOVIES_PATH}",
                params={
                    "playDate": show_date.strftime("%Y%m%d"),
                    "theaterCode": branch_id,
                },
                headers={"Accept": "application/json", "User-Agent": "movie-alert/0.1"},
                timeout=httpx.Timeout(
                    self.http.timeout_seconds, connect=min(5.0, self.http.timeout_seconds)
                ),
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise ProviderError("CGV 릴레이 영화 목록 요청에 실패했습니다.") from None
        if response.status_code in (401, 403):
            raise AccessDeniedError(f"CGV 릴레이 접근 제한(HTTP {response.status_code})입니다.")
        if response.status_code == 429:
            raise RateLimitError("CGV 릴레이 요청 제한(HTTP 429)입니다.")
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
        for movie in movies:
            if not isinstance(movie, dict):
                raise ResponseValidationError("CGV 릴레이 영화 목록의 각 항목은 객체여야 합니다.")
            if movie.get("movieCode") == movie_id:
                return True
        return False

    def fetch_showtimes(self, movie_id: str, branch_id: str, show_date: date) -> list[Showtime]:
        url = f"{self.settings.relay_base_url}{TIMETABLE_PATH}"
        params = {
            "playDate": show_date.strftime("%Y%m%d"),
            "theaterCode": branch_id,
            "movieCode": movie_id,
            "limit": "50",
        }
        for attempt in range(self.http.max_retries + 1):
            try:
                response = self.client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/json", "User-Agent": "movie-alert/0.1"},
                    timeout=httpx.Timeout(
                        self.http.timeout_seconds, connect=min(5.0, self.http.timeout_seconds)
                    ),
                    follow_redirects=False,
                )
            except httpx.RequestError:
                if attempt == self.http.max_retries:
                    raise ProviderError(
                        "CGV 릴레이 요청 시간이 초과되었거나 연결에 실패했습니다."
                    ) from None
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise AccessDeniedError(f"CGV 릴레이 접근 제한(HTTP {response.status_code})입니다.")
            if response.status_code == 429:
                raise RateLimitError("CGV 릴레이 요청 제한(HTTP 429)입니다.")
            if response.status_code == 502:
                if not self._movie_is_listed(movie_id, branch_id, show_date):
                    return []
                if attempt < self.http.max_retries:
                    self.sleep(self.http.retry_delay_seconds * 2**attempt)
                    continue
                raise ProviderError("CGV 릴레이가 시간표를 반환하지 못했습니다(HTTP 502).")
            if response.status_code in RETRYABLE_STATUSES and attempt < self.http.max_retries:
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
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
        raise ProviderError("CGV 릴레이 시간표 조회를 완료하지 못했습니다.")
