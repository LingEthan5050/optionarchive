"""Trading-day and session-time logic.

Once a scheduler fires this job unattended, the guard here is what stops the
archive filling with weekend and holiday partitions full of stale Friday
quotes. Uses the NYSE calendar from pandas_market_calendars rather than
hardcoded holidays, because hardcoded holiday lists rot silently.

The session targets are derived from the exchange's own open and close rather
than from fixed clock times:

    am target = market open  + 15 minutes   (09:45 on a normal day)
    pm target = market close - 15 minutes   (15:45, or 12:45 on an early close)

That derivation is what makes the early-close shift automatic. A scheduler can
fire the pm job at both 12:45 and 15:45 every day; on a normal day the 12:45
firing is outside the tolerance window and becomes a no-op, and on an early
close day the 15:45 firing does. Neither needs to know the calendar.
"""

from __future__ import annotations

import functools
from datetime import date, datetime, timedelta

import pandas_market_calendars as mcal

from chain_archiver.config import EASTERN

#: Minutes after the open / before the close that each session targets.
#: 09:45 avoids the opening auction, whose quotes are unreliable for several
#: minutes. 15:45 avoids the bell, into which quotes widen and go stale.
OPEN_OFFSET = timedelta(minutes=15)
CLOSE_OFFSET = timedelta(minutes=15)

#: How far from the target a run may fire and still count. Wide enough to
#: absorb scheduler lag and a machine waking from sleep, narrow enough that a
#: pm trigger three hours from its target is correctly rejected.
TOLERANCE = timedelta(minutes=30)


class NotATradingDay(RuntimeError):
    """Raised when a snapshot is attempted on a day the market is closed."""


class WrongTimeForSession(RuntimeError):
    """Raised when a run fires too far from its session's target time."""


@functools.lru_cache(maxsize=8)
def _schedule_for_year(year: int):
    calendar = mcal.get_calendar("NYSE")
    return calendar.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")


def _row(day: date):
    schedule = _schedule_for_year(day.year)
    key = day.isoformat()
    if key not in schedule.index:
        return None
    return schedule.loc[key]


def is_trading_day(day: date) -> bool:
    return _row(day) is not None


def market_hours(day: date) -> tuple[datetime, datetime] | None:
    """(open, close) in US/Eastern, or None if the market is closed that day."""
    row = _row(day)
    if row is None:
        return None
    return (
        row["market_open"].tz_convert(EASTERN).to_pydatetime(),
        row["market_close"].tz_convert(EASTERN).to_pydatetime(),
    )


def is_early_close(day: date) -> bool:
    hours = market_hours(day)
    return hours is not None and hours[1].hour < 16


def target_time(day: date, session: str) -> datetime | None:
    """When `session` should be captured on `day`, in US/Eastern."""
    hours = market_hours(day)
    if hours is None:
        return None
    market_open, market_close = hours
    if session == "am":
        return market_open + OPEN_OFFSET
    return market_close - CLOSE_OFFSET


def check_runnable(session: str, now_et: datetime) -> datetime:
    """Raise unless `now_et` is a sane moment to capture `session`.

    Returns the session's target time on success, for logging.
    """
    day = now_et.date()
    if not is_trading_day(day):
        raise NotATradingDay(f"{day} is not an NYSE trading day")

    target = target_time(day, session)
    assert target is not None  # guaranteed by the trading-day check above
    drift = abs(now_et - target)
    if drift > TOLERANCE:
        raise WrongTimeForSession(
            f"{now_et:%H:%M} is {drift} from the {session} target of "
            f"{target:%H:%M} ET"
            + (" (early close)" if is_early_close(day) else "")
        )
    return target
