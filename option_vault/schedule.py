from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Slot:
    label: str
    at: datetime
    closing: bool = False


def day_slots(day: date, cfg: dict[str, Any]) -> list[Slot]:
    calendar = mcal.get_calendar(cfg["calendar"])
    schedule = calendar.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return []
    opening = schedule.iloc[0]["market_open"].to_pydatetime().astimezone(NY)
    closing = schedule.iloc[0]["market_close"].to_pydatetime().astimezone(NY)
    normal_open = datetime.combine(day, time(9, 30), NY)
    normal_close = datetime.combine(day, time(16), NY)
    ratio = (closing - opening) / (normal_close - normal_open)
    slots = []
    for value in cfg["regular_times"]:
        hour, minute = map(int, value.split(":"))
        nominal = datetime.combine(day, time(hour, minute), NY)
        if not normal_open < nominal < normal_close:
            raise ValueError(f"Configured time outside regular session: {value}")
        adjusted = opening + (nominal - normal_open) * ratio
        adjusted = adjusted.replace(second=0, microsecond=0)
        slots.append(Slot(adjusted.strftime("%H:%M"), adjusted))
    after = closing + timedelta(minutes=cfg["close_delay_minutes"])
    slots.append(Slot(after.strftime("%H:%M") + "-close", after, True))
    if len({s.label for s in slots}) != len(slots):
        raise ValueError("Duplicate schedule labels")
    return slots


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hourly_slots(day: date, cfg: dict[str, Any]) -> list[Slot]:
    calendar = mcal.get_calendar(cfg["calendar"])
    schedule = calendar.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return []
    opening = schedule.iloc[0]["market_open"].to_pydatetime().astimezone(NY)
    closing = schedule.iloc[0]["market_close"].to_pydatetime().astimezone(NY)
    cursor = opening.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    slots = []
    while cursor < closing:
        slots.append(Slot("watch-" + cursor.strftime("%H:%M"), cursor))
        cursor += timedelta(hours=1)
    return slots
