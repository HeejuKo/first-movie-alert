"""CGV adapter based on the user's captured searchSchByMov request/response."""

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
    RateLimitError,
    ResponseValidationError,
)
from movie_alert.providers.common import (
    RETRYABLE_STATUSES,
    get_response,
    parse_business_date,
    required_text,
)

SCHEDULE_URL = "https://cgv.co.kr/api/v1/booking/searchSchByMov"
_text = partial(required_text, source="CGV")
_business_date = partial(parse_business_date, field="CGV scnYmd")


def _screening_time(day: date, value: str) -> datetime:
    if len(value) != 4 or not value.isascii() or not value.isdigit() or int(value[2:]) >= 60:
        raise ResponseValidationError("CGV 상영 시각: HHMM 형식이 필요합니다.")
    # Observed scnendTm=2509 belongs to the following calendar day.
    return datetime.combine(day, datetime_time(), SEOUL) + timedelta(
        hours=int(value[:2]), minutes=int(value[2:])
    )


def _seat_hint(row: dict[str, Any]) -> int | None:
    value = row.get("frSeatCnt")
    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ResponseValidationError("CGV frSeatCnt: 음수가 아닌 정수 문자열이 필요합니다.")
    return int(value)


def parse_schedule(payload: object) -> list[Showtime]:
    if not isinstance(payload, dict) or type(payload.get("statusCode")) is not int:
        raise ResponseValidationError("CGV 응답에 숫자 statusCode가 필요합니다.")
    if payload["statusCode"] != 0:
        raise ResponseValidationError("CGV가 조회 실패 상태를 반환했습니다.")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ResponseValidationError("CGV data는 목록이어야 합니다.")
    showtimes = []
    for row in rows:
        if not isinstance(row, dict):
            raise ResponseValidationError("CGV data의 각 회차는 객체여야 합니다.")
        day = _business_date(_text(row, "scnYmd"))
        start = _screening_time(day, _text(row, "scnsrtTm"))
        end = _screening_time(day, _text(row, "scnendTm"))
        if end < start:
            raise ResponseValidationError("CGV 종료 시각이 시작 시각보다 빠릅니다.")
        showtimes.append(
            Showtime(
                provider="cgv",
                movie_id=_text(row, "movNo"),
                movie_name=_text(row, "movNm"),
                branch_id=_text(row, "siteNo"),
                branch_name=_text(row, "siteNm"),
                screen_id=_text(row, "scnsNo"),
                screen_name=_text(row, "scnsNm"),
                business_date=day,
                sequence=_text(row, "scnSseq"),
                starts_at=start,
                ends_at=end,
                remaining_seats_hint=_seat_hint(row),
            )
        )
    # Do not skip malformed rows: the missing row might be the earliest session.
    return showtimes


class CgvProvider:
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

    def fetch_showtimes(self, movie_id: str, branch_id: str, show_date: date) -> list[Showtime]:
        for attempt in range(self.http.max_retries + 1):
            try:
                response = get_response(
                    self.client,
                    SCHEDULE_URL,
                    params={
                        "coCd": self.settings.co_cd,
                        "siteNo": branch_id,
                        "scnYmd": show_date.strftime("%Y%m%d"),
                        "movNo": movie_id,
                        "rtctlScopCd": self.settings.rtctl_scop_cd,
                    },
                    timeout_seconds=self.http.timeout_seconds,
                )
            except httpx.RequestError:
                if attempt == self.http.max_retries:
                    raise ProviderError(
                        "CGV 요청 시간이 초과되었거나 연결에 실패했습니다."
                    ) from None
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise AccessDeniedError(
                    f"CGV 접근 제한(HTTP {response.status_code}). 허용된 조회 방법을 확인하세요."
                )
            if response.status_code == 429:
                raise RateLimitError(
                    "CGV 요청 제한(HTTP 429). 요청을 중지하고 재시도 지침을 확인하세요."
                )
            if response.status_code in RETRYABLE_STATUSES and attempt < self.http.max_retries:
                self.sleep(self.http.retry_delay_seconds * 2**attempt)
                continue
            if response.status_code != 200:
                raise ProviderError(f"CGV 시간표 조회에 실패했습니다(HTTP {response.status_code}).")
            try:
                payload = response.json()
            except ValueError:
                raise ResponseValidationError("CGV 응답이 유효한 JSON이 아닙니다.") from None
            showtimes = parse_schedule(payload)
            if any(
                row.movie_id != movie_id
                or row.branch_id != branch_id
                or row.business_date != show_date
                for row in showtimes
            ):
                raise ResponseValidationError("CGV 응답의 영화·지점·상영일이 요청과 다릅니다.")
            return showtimes
        raise ProviderError("CGV 시간표 조회를 완료하지 못했습니다.")
