from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable


@dataclass(frozen=True)
class MarketClockState:
    trade_date: str
    phase: str
    is_trade_day: bool
    is_open_session: bool


class MarketCalendarService:
    """A-share trading calendar and intra-day phase service (MVP)."""

    version = "ashare-calendar-v1"

    def __init__(self, holidays: Iterable[str] | None = None):
        self._holidays = set(str(x) for x in (holidays or []))

    def is_trade_date(self, value: str | date) -> bool:
        dt_date = date.fromisoformat(str(value)) if not isinstance(value, date) else value
        date_s = dt_date.isoformat()
        return bool(dt_date.weekday() < 5 and date_s not in self._holidays)

    def next_trade_date(self, value: str | date, *, include_current: bool = False) -> str:
        dt_date = date.fromisoformat(str(value)) if not isinstance(value, date) else value
        if not include_current:
            dt_date = dt_date + timedelta(days=1)
        for _ in range(366):
            if self.is_trade_date(dt_date):
                return dt_date.isoformat()
            dt_date = dt_date + timedelta(days=1)
        raise RuntimeError("failed to resolve next trade date within 366 days")

    def close_ts_ms(self, trade_date: str | date) -> int:
        dt_date = date.fromisoformat(str(trade_date)) if not isinstance(trade_date, date) else trade_date
        return int(datetime.combine(dt_date, time(15, 0)).timestamp() * 1000)

    def day_order_expiry_ts_ms(self, clock: MarketClockState) -> int:
        if clock.phase in {"after_close", "holiday"}:
            expiry_date = self.next_trade_date(clock.trade_date)
        else:
            expiry_date = self.next_trade_date(clock.trade_date, include_current=True)
        return self.close_ts_ms(expiry_date)

    def get_clock(self, *, ts_ms: int | None = None, trade_date: str | None = None) -> MarketClockState:
        if ts_ms is None:
            now = datetime.now()
        else:
            now = datetime.fromtimestamp(int(ts_ms) / 1000)

        dt_date = date.fromisoformat(str(trade_date)) if trade_date else now.date()
        is_weekday = dt_date.weekday() < 5
        date_s = dt_date.isoformat()
        is_trade_day = bool(is_weekday and date_s not in self._holidays)

        if not is_trade_day:
            return MarketClockState(
                trade_date=date_s,
                phase="holiday",
                is_trade_day=False,
                is_open_session=False,
            )

        t = now.time()
        if t < time(9, 30):
            phase = "pre_open"
            is_open = False
        elif t < time(11, 30):
            phase = "open_morning"
            is_open = True
        elif t < time(13, 0):
            phase = "lunch_break"
            is_open = False
        elif t < time(15, 0):
            phase = "open_afternoon"
            is_open = True
        else:
            phase = "after_close"
            is_open = False

        return MarketClockState(
            trade_date=date_s,
            phase=phase,
            is_trade_day=True,
            is_open_session=is_open,
        )
