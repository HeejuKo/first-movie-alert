"""Pure filtering and first-session selection, independent of provider JSON."""

from collections.abc import Iterable
from datetime import date

from movie_alert.config import WatchRule
from movie_alert.models import Showtime


def select_first_showtimes(showtimes: Iterable[Showtime], rule: WatchRule) -> list[Showtime]:
    if not rule.enabled:
        return []
    groups: dict[date, list[Showtime]] = {}
    for showtime in showtimes:
        if (
            showtime.provider == rule.provider
            and showtime.movie_id == rule.movie_id
            and showtime.branch_id == rule.branch_id
            and showtime.business_date in rule.dates
            and showtime.business_date.isoweekday() in rule.weekdays
        ):
            groups.setdefault(showtime.business_date, []).append(showtime)
    selected = []
    for day in sorted(groups):
        earliest = min(showtime.starts_at for showtime in groups[day])
        # Keep simultaneous first sessions in separate screens; never promote a later one.
        selected.extend(showtime for showtime in groups[day] if showtime.starts_at == earliest)
    return sorted(selected, key=lambda item: (item.starts_at, item.screen_id, item.sequence))
