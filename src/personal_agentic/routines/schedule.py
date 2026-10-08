"""When routines run.

Front-matter forms (any of these, under `schedule:` or at the top level):

    schedule: "0 16 * * 1-5"          # cron, in your timezone
    schedule: {cron: "30 8 * * 1-5"}
    schedule: {at: "16:00", days: workdays}          # workdays | daily | [mon, wed, fri]
    schedule: {every: 15m, window: workday}          # window: workday | {start, end, days}

Workdays and the workday window come from [workday] in config.toml, and skip holidays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from ..config import WorkdayConfig, parse_duration

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _norm_days(days: Any, wd: WorkdayConfig) -> tuple[set[int], bool]:
    """-> (weekday numbers, skip holidays)."""
    if days in (None, "workdays", "workday", "weekdays"):
        return {DAY_NAMES.index(d.lower()[:3]) for d in wd.days}, True
    if days in ("daily", "every day", "all"):
        return set(range(7)), False
    if isinstance(days, str):
        days = [d.strip() for d in days.split(",")]
    return {DAY_NAMES.index(str(d).lower()[:3]) for d in days}, False


def _hm(value: str | time) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML reads 16:00 unquoted as minutes (960)
        return time(value // 60, value % 60)
    h, m = str(value).strip().split(":")[:2]
    return time(int(h), int(m))


@dataclass
class Window:
    start: time
    end: time
    days: set[int]
    skip_holidays: bool = True


@dataclass
class Schedule:
    tz: ZoneInfo
    holidays: set[date] = field(default_factory=set)
    cron: str | None = None
    at: time | None = None
    every: timedelta | None = None
    days: set[int] = field(default_factory=lambda: set(range(7)))
    window: Window | None = None
    skip_holidays: bool = False
    raw: Any = None

    def describe(self) -> str:
        if self.cron:
            return f"cron '{self.cron}'"
        dnames = ",".join(DAY_NAMES[d] for d in sorted(self.days))
        if self.at:
            return f"at {self.at.strftime('%H:%M')} on {dnames}"
        if self.every:
            mins = int(self.every.total_seconds() // 60)
            w = self.window
            span = (
                f" between {w.start.strftime('%H:%M')}-{w.end.strftime('%H:%M')} on "
                + ",".join(DAY_NAMES[d] for d in sorted(w.days))
                if w
                else ""
            )
            return f"every {mins} min{span}"
        return "manual"

    def _day_ok(self, d: date, days: set[int], skip_holidays: bool) -> bool:
        return d.weekday() in days and not (skip_holidays and d in self.holidays)

    def next_after(self, after: datetime) -> datetime | None:
        after = after.astimezone(self.tz)
        if self.cron:
            it = croniter(self.cron, after)
            for _ in range(2000):
                nxt = it.get_next(datetime)
                if not self.skip_holidays or nxt.date() not in self.holidays:
                    return nxt
            return None
        if self.at:
            for offset in range(0, 370):
                d = after.date() + timedelta(days=offset)
                cand = datetime.combine(d, self.at, tzinfo=self.tz)
                if cand > after and self._day_ok(d, self.days, self.skip_holidays):
                    return cand
            return None
        if self.every:
            return self._next_interval(after)
        return None

    def _next_interval(self, after: datetime) -> datetime | None:
        step = self.every
        assert step is not None
        w = self.window
        for offset in range(0, 370):
            d = after.date() + timedelta(days=offset)
            if w and not self._day_ok(d, w.days, w.skip_holidays):
                continue
            day_start = datetime.combine(d, w.start if w else time(0, 0), tzinfo=self.tz)
            day_end = datetime.combine(d, w.end, tzinfo=self.tz) if w else day_start + timedelta(days=1)
            if after < day_start:
                return day_start
            if after >= day_end:
                continue
            n = int((after - day_start) / step) + 1
            cand = day_start + n * step
            if cand < day_end:
                return cand
        return None


def parse_schedule(spec: Any, wd: WorkdayConfig) -> Schedule | None:
    if spec in (None, "", False, "manual"):
        return None
    tz = ZoneInfo(wd.timezone)
    sched = Schedule(tz=tz, holidays=set(wd.holidays), raw=spec)
    if isinstance(spec, str):
        spec = {"cron": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"Can't read schedule {spec!r}")
    if spec.get("cron"):
        sched.cron = str(spec["cron"])
        if not croniter.is_valid(sched.cron):
            raise ValueError(f"Invalid cron expression {sched.cron!r}")
        sched.skip_holidays = bool(spec.get("skip_holidays", True))
        return sched
    if spec.get("at") is not None:
        sched.at = _hm(spec["at"])
        sched.days, sched.skip_holidays = _norm_days(spec.get("days", "workdays"), wd)
        if "skip_holidays" in spec:
            sched.skip_holidays = bool(spec["skip_holidays"])
        return sched
    if spec.get("every"):
        sched.every = parse_duration(spec["every"])
        if sched.every < timedelta(minutes=1):
            raise ValueError("Schedules can't run more often than once a minute")
        window = spec.get("window")
        if window in ("workday", "workdays"):
            days, _ = _norm_days("workdays", wd)
            sched.window = Window(_hm(wd.start), _hm(wd.end), days, skip_holidays=True)
        elif isinstance(window, dict):
            days, skip = _norm_days(window.get("days", "workdays"), wd)
            sched.window = Window(
                _hm(window.get("start", wd.start)),
                _hm(window.get("end", wd.end)),
                days,
                skip_holidays=bool(window.get("skip_holidays", skip)),
            )
        elif window:
            raise ValueError(f"Unknown window {window!r}; use 'workday' or {{start, end, days}}")
        return sched
    raise ValueError(f"Schedule needs one of cron / at / every: {spec!r}")
