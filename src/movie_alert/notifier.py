"""Discord Webhook delivery without hidden retries or secret-bearing errors."""

import logging
import math
import re
from dataclasses import dataclass

import httpx

from movie_alert.models import SEOUL, BookingStatus, Showtime

_WEBHOOK_PATH = re.compile(r"/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9._-]+/?")
_WEBHOOK_IN_LOG = re.compile(
    r"https://(?:discord\.com|discordapp\.com)/api(?:/v[0-9]+)?/webhooks/[^\s\"'<>]+"
)


class NotificationError(Exception):
    """A sanitized delivery failure; the caller owns state and retry decisions."""

    retryable = False
    delivery_unknown = False


class NotificationRejected(NotificationError):
    """Invalid input or a definite rejection; do not retry unchanged input."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotificationConnectionError(NotificationError):
    """No connection was established; a bounded later retry is possible."""

    retryable = True


class NotificationRateLimited(NotificationError):
    """Discord rejected the request; wait at least retry_after_seconds if supplied."""

    retryable = True

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("Discord 요청 제한(HTTP 429). 재시도 대기 시간을 확인하세요.")
        self.retry_after_seconds = retry_after_seconds


class NotificationDeliveryUnknown(NotificationError):
    """A message may already exist; do not blindly repeat the POST."""

    delivery_unknown = True


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: str


class _WebhookLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs its full request URL at INFO, including the webhook token.
        message = record.getMessage()
        redacted = _WEBHOOK_IN_LOG.sub("[Discord Webhook URL 비공개]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def _protect_httpx_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _WebhookLogFilter) for item in logger.filters):
        logger.addFilter(_WebhookLogFilter())


def _webhook_url(value: str) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, TypeError):
        raise NotificationRejected("Discord Webhook URL 형식이 올바르지 않습니다.") from None
    if (
        url.scheme != "https"
        or url.host not in {"discord.com", "discordapp.com"}
        or url.port not in {None, 443}
        or url.userinfo
        or url.query
        or url.fragment
        or not _WEBHOOK_PATH.fullmatch(url.path)
    ):
        raise NotificationRejected("Discord의 HTTPS Webhook URL이 필요합니다.")
    return url


def _label(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def notification_content(showtime: Showtime) -> str:
    """Report a detected schedule without claiming that booking has opened."""
    if showtime.starts_at.tzinfo is None or showtime.starts_at.utcoffset() is None:
        raise NotificationRejected("상영 시작 시각에는 시간대가 필요합니다.")
    starts_at = showtime.starts_at.astimezone(SEOUL)
    booking_label = {
        BookingStatus.UNKNOWN: "미확인",
        BookingStatus.AVAILABLE: "가능",
        BookingStatus.UNAVAILABLE: "불가",
    }[showtime.booking_status]
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    weekday = weekdays[starts_at.weekday()]

    lines = [
        f"🎬 {starts_at:%m/%d}({weekday}) 상영시간표 발견",
        f"{_label(showtime.movie_name, 300)} (CGV {_label(showtime.branch_name, 150)})",
        f"1회차: {starts_at:%H:%M}",
        f"예매 가능 여부: {booking_label}",
    ]
    if starts_at.date() != showtime.business_date:
        lines.append(f"상영 영업일: {showtime.business_date:%Y-%m-%d}")
    return "\n".join(lines)


def _retry_after(response: httpx.Response) -> float | None:
    candidates: list[object] = [response.headers.get("Retry-After")]
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        candidates.append(body.get("retry_after"))
    delays = []
    for value in candidates:
        if isinstance(value, bool) or not isinstance(value, (str, float, int)):
            continue
        try:
            seconds = float(value)
        except (ValueError, OverflowError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            delays.append(seconds)
    return max(delays) if delays else None


class DiscordNotifier:
    """Send once with wait=true; obtain a state-store claim before calling send."""

    def __init__(
        self,
        client: httpx.Client,
        webhook_url: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise NotificationRejected("Discord 요청 제한 시간은 양수여야 합니다.")
        self._client = client
        self._url = _webhook_url(webhook_url).copy_merge_params({"wait": "true"})
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        _protect_httpx_logs()

    def send(self, showtime: Showtime) -> DeliveryReceipt:
        payload = {
            "content": notification_content(showtime),
            "allowed_mentions": {"parse": []},
        }
        try:
            response = self._client.post(
                self._url,
                json=payload,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
            raise NotificationConnectionError(
                "Discord에 연결하지 못했습니다. 제한된 재시도 정책을 적용하세요."
            ) from None
        except httpx.RequestError:
            raise NotificationDeliveryUnknown(
                "Discord 전송 결과를 확인하지 못했습니다. 중복 가능성이 있어 재전송하지 않습니다."
            ) from None
        if response.status_code == 429:
            raise NotificationRateLimited(_retry_after(response))
        if response.status_code >= 500 or response.status_code == 408:
            raise NotificationDeliveryUnknown(
                f"Discord 서버 응답으로 전송을 확정할 수 없습니다(HTTP {response.status_code})."
            )
        if response.status_code != 200:
            if 200 <= response.status_code < 300:
                raise NotificationDeliveryUnknown(
                    "Discord 메시지 생성 확인 응답을 받지 못했습니다."
                )
            raise NotificationRejected(
                f"Discord가 알림을 거부했습니다(HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise NotificationDeliveryUnknown(
                "Discord 메시지 생성 응답이 JSON이 아닙니다."
            ) from None
        message_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(message_id, str) or not message_id.isascii() or not message_id.isdigit():
            raise NotificationDeliveryUnknown(
                "Discord 응답에서 생성된 메시지 ID를 확인하지 못했습니다."
            )
        return DeliveryReceipt(message_id=message_id)
