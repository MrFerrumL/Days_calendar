from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
DAY_CODES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    source: str
    details: str = ""
    cancelled: bool = False
    all_day: bool = False

    @property
    def time_label(self):
        return "весь день" if self.all_day else f"{self.start:%H:%M}–{self.end:%H:%M}"


def at(day: date, clock: str, tz) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=tz)


def clock(value: str) -> str:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.strftime("%H:%M")


def parse_day(value: str, today: date) -> date:
    aliases = {"сегодня": today, "today": today, "завтра": today + timedelta(days=1),
               "tomorrow": today + timedelta(days=1)}
    return aliases[value.lower()] if value.lower() in aliases else date.fromisoformat(value)


def overlaps(events: list[Event]):
    active = sorted((e for e in events if not e.cancelled and not e.all_day), key=lambda e: e.start)
    return [(a, b) for i, a in enumerate(active) for b in active[i + 1:] if b.start < a.end]


def free_slots(events: list[Event], day: date, tz, minutes=30, after=None):
    lower, upper = at(day, "08:00", tz), at(day, "23:00", tz)
    cursor = max(lower, after) if after else lower
    result = []
    for event in sorted(events, key=lambda e: e.start):
        if event.cancelled or event.all_day or event.end <= cursor or event.start >= upper:
            continue
        if event.start - cursor >= timedelta(minutes=minutes):
            result.append((cursor, min(event.start, upper)))
        cursor = max(cursor, event.end)
    if upper - cursor >= timedelta(minutes=minutes):
        result.append((cursor, upper))
    return result
