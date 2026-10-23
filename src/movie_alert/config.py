"""Load explicit watch rules from TOML before making external requests."""

import math
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from movie_alert.models import SEOUL


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class HttpSettings:
    timeout_seconds: float = 10.0
    max_retries: int = 1
    retry_delay_seconds: float = 0.5
    max_parallel_requests: int = 3


@dataclass(frozen=True)
class CgvSettings:
    co_cd: str
    rtctl_scop_cd: str
    source: str = "direct"
    relay_base_url: str = "https://mcp.aka.page"


@dataclass(frozen=True)
class WatchRule:
    name: str
    provider: str
    movie_name: str
    movie_id: str
    branch_name: str
    branch_id: str
    dates: tuple[date, ...]
    weekdays: frozenset[int]
    session_policy: str
    enabled: bool
    active_from: datetime
    active_until: datetime


@dataclass(frozen=True)
class AppConfig:
    cgv: CgvSettings
    http: HttpSettings
    watches: tuple[WatchRule, ...]


def _text(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}: 비어 있지 않은 문자열이 필요합니다. ID도 문자열로 적으세요.")
    return value.strip()


def _url(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}: HTTP 또는 HTTPS URL이 필요합니다.")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query:
        raise ConfigError(f"{key}: 쿼리 문자열이 없는 HTTP 또는 HTTPS URL이 필요합니다.")
    return value.rstrip("/")


def _timestamp(table: dict[str, Any], key: str) -> datetime:
    value = table.get(key)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ConfigError(f"{key}: 올바른 ISO 날짜·시간이 필요합니다.") from None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ConfigError(f"{key}: +09:00 등 시간대가 있는 날짜·시간이 필요합니다.")
    return value.astimezone(SEOUL)


def _rule(table: dict[str, Any]) -> WatchRule:
    provider = _text(table, "provider")
    if provider != "cgv":
        raise ConfigError(f"지원하지 않는 Provider입니다: {provider}")
    policy = _text(table, "session_policy")
    if policy != "earliest":
        raise ConfigError("현재 session_policy는 earliest만 지원합니다.")
    enabled = table.get("enabled", True)
    if type(enabled) is not bool:
        raise ConfigError("enabled: true 또는 false가 필요합니다.")
    weekdays = table.get("weekdays", list(range(1, 8)))
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or any(type(day) is not int or day not in range(1, 8) for day in weekdays)
    ):
        raise ConfigError("weekdays: ISO 요일 숫자(월=1, 일=7)의 비어 있지 않은 목록이 필요합니다.")
    raw_dates = table.get("dates")
    if not isinstance(raw_dates, list) or not raw_dates:
        raise ConfigError("dates: 조회할 날짜 목록을 명시하세요.")
    dates = []
    for value in raw_dates:
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                raise ConfigError("dates: YYYY-MM-DD 형식의 날짜가 필요합니다.") from None
        if type(value) is not date:
            raise ConfigError("dates: 시간 없이 날짜만 적으세요.")
        dates.append(value)
    active_from = _timestamp(table, "active_from")
    active_until = _timestamp(table, "active_until")
    if active_from >= active_until:
        raise ConfigError("active_until은 active_from보다 늦어야 합니다.")
    return WatchRule(
        name=_text(table, "name"),
        provider=provider,
        movie_name=_text(table, "movie_name"),
        movie_id=_text(table, "movie_id"),
        branch_name=_text(table, "branch_name"),
        branch_id=_text(table, "branch_id"),
        dates=tuple(sorted(set(dates))),
        weekdays=frozenset(weekdays),
        session_policy=policy,
        enabled=enabled,
        active_from=active_from,
        active_until=active_until,
    )


def load_config(path: Path) -> AppConfig:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("설정 파일을 읽을 수 없거나 TOML 형식이 잘못되었습니다.") from None
    cgv = raw.get("cgv")
    http = raw.get("http", {})
    defaults = raw.get("watch_defaults", {})
    watches = raw.get("watches")
    if not isinstance(cgv, dict) or not isinstance(http, dict):
        raise ConfigError("[cgv]와 [http] 설정은 TOML table이어야 합니다.")
    if not isinstance(defaults, dict):
        raise ConfigError("[watch_defaults] 설정은 TOML table이어야 합니다.")
    if (
        not isinstance(watches, list)
        or not watches
        or not all(isinstance(rule, dict) for rule in watches)
    ):
        raise ConfigError("하나 이상의 [[watches]] 설정이 필요합니다.")
    timeout = http.get("timeout_seconds", 10.0)
    retries = http.get("max_retries", 1)
    delay = http.get("retry_delay_seconds", 0.5)
    parallel_requests = http.get("max_parallel_requests", 3)
    for name, value, lower, upper in [
        ("timeout_seconds", timeout, 0.1, 60),
        ("retry_delay_seconds", delay, 0, 5),
    ]:
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not lower <= value <= upper
        ):
            raise ConfigError(f"{name}: {lower}~{upper} 범위의 숫자가 필요합니다.")
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ConfigError("max_retries: 0~2 사이의 정수가 필요합니다.")
    if type(parallel_requests) is not int or not 1 <= parallel_requests <= 6:
        raise ConfigError("max_parallel_requests: 1~6 사이의 정수가 필요합니다.")
    rules = tuple(_rule({**defaults, **rule}) for rule in watches)
    if len({rule.name for rule in rules}) != len(rules):
        raise ConfigError("각 watch의 name은 고유해야 합니다.")
    source = cgv.get("source", "direct")
    if not isinstance(source, str) or source not in {"direct", "mcp_relay"}:
        raise ConfigError("[cgv].source는 direct 또는 mcp_relay여야 합니다.")
    return AppConfig(
        cgv=CgvSettings(
            _text(cgv, "co_cd"),
            _text(cgv, "rtctl_scop_cd"),
            source=source,
            relay_base_url=_url(cgv, "relay_base_url", "https://mcp.aka.page"),
        ),
        http=HttpSettings(
            timeout_seconds=float(timeout),
            max_retries=retries,
            retry_delay_seconds=float(delay),
            max_parallel_requests=parallel_requests,
        ),
        watches=rules,
    )
