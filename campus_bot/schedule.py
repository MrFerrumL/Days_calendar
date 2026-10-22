import asyncio
from datetime import date, datetime, timedelta
import hashlib
import json
from dataclasses import replace
from urllib.parse import urlencode

from .models import Event, at
from .network import ServiceError, get_json

API_BASE = "https://api.cube.nlabs.su/api"


def personal_events(day, rules, anchor, tz, skipped=lambda uid, day: False):
    cycle = "a" if ((day - anchor).days // 7) % 2 == 0 else "b"
    events = []
    for rule in rules:
        if day < anchor or day.weekday() not in rule["weekdays"]:
            continue
        if rule.get("cycle", "all") not in ("all", cycle):
            continue
        start = at(day, rule["time"], tz)
        events.append(Event(
            "personal:" + rule["id"], rule["title"], start,
            start + timedelta(minutes=rule["duration_minutes"]), "personal",
            rule["id"], skipped(rule["id"], day.isoformat()),
        ))
    return events


def parse_university(payload, tz, group_id):
    if not isinstance(payload, dict) or payload.get("status") not in ("AVAILABLE", "EMPTY"):
        raise ValueError("Расписание не опубликовано или изменился формат ответа")
    if not isinstance(payload.get("data"), list):
        raise ValueError("В ответе нет списка занятий")
    events = []
    for row in payload["data"]:
        if not any(g["id"] == group_id for g in row.get("groups", [])):
            raise ValueError("Источник вернул занятие другой группы")
        day = date.fromisoformat(row["date"])
        kind = row.get("type") or {}
        discipline = row.get("discipline") or {}
        all_day = bool(kind.get("all_day"))
        if all_day:
            start, end = at(day, "00:00", tz), at(day + timedelta(days=1), "00:00", tz)
        else:
            if not row.get("start_time") or not row.get("end_time"):
                raise ValueError("У занятия отсутствует время")
            start, end = at(day, row["start_time"], tz), at(day, row["end_time"], tz)
            if end <= start:
                raise ValueError("Неверный интервал занятия")
        title = discipline.get("name") or kind.get("name") or "Занятие"
        details = [kind.get("short_name") or kind.get("name")]
        place = row.get("place")
        if place:
            details.append("ауд. " + place["name"])
        details.extend(t["short_name"] for t in row.get("teachers", []))
        if row.get("is_remotely"):
            details.append("дистанционно")
        events.append(Event("uni:" + str(row["id"]), title, start, end, "university",
                            " · ".join(d for d in details if d), row.get("status") == 3, all_day))
    return events


def fingerprint(events):
    data = sorted((e.uid, e.title, e.start.isoformat(), e.end.isoformat(), e.details, e.cancelled)
                  for e in events)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


class Schedule:
    def __init__(self, store, config, fetch=get_json):
        self.store, self.config, self.fetch = store, config, fetch
        doc = json.loads(config.lessons_path.read_text(encoding="utf-8"))
        self.anchor = date.fromisoformat(doc["cycle_anchor"])
        if self.anchor.weekday() != 0:
            raise ValueError("Начало двухнедельного цикла должно быть понедельником")
        store.seed_rules(doc)
        self.lock = asyncio.Lock()
        self.retry_after = {}

    def fallback(self, cached, group_id):
        if cached:
            return parse_university(cached[0], self.config.timezone, group_id), (
                "⚠️ Сайт недоступен. Расписание из кэша от "
                + datetime.fromisoformat(cached[1]).strftime("%d.%m %H:%M")
                + "; проверьте изменения на сайте."), False
        return [], "⚠️ Не удалось получить расписание вуза. Это не означает, что пар нет.", False

    async def university(self, day: date, force=False):
        monday = day - timedelta(days=day.weekday())
        group_id = self.store.get("group_id")
        key = f"uni:{group_id}:{monday}"
        now = datetime.now(self.config.timezone)
        async with self.lock:
            cached = self.store.cache_get(key)
            ttl = timedelta(minutes=self.store.get("sync_minutes"))
            if cached and not force and now - datetime.fromisoformat(cached[1]) < ttl:
                return parse_university(cached[0], self.config.timezone, group_id), "", False
            if not force and now < self.retry_after.get(key, now):
                return self.fallback(cached, group_id)
            query = urlencode({"group": group_id, "start_date": monday.isoformat(),
                               "end_date": (monday + timedelta(days=6)).isoformat()})
            try:
                payload = await self.fetch(API_BASE + "/timetable/lessons/viewer?" + query, "Расписание вуза")
                events = parse_university(payload, self.config.timezone, group_id)
                if any(not monday <= e.start.date() <= monday + timedelta(days=6) for e in events):
                    raise ValueError("Источник вернул другой диапазон дат")
                changed = cached is not None and fingerprint(events) != fingerprint(
                    parse_university(cached[0], self.config.timezone, group_id))
                self.store.cache_put(key, payload, now.isoformat())
                self.retry_after.pop(key, None)
                if changed:
                    self.store.set("pending_change", self.store.get("group_name"))
                return events, "", changed
            except (ServiceError, ValueError, KeyError, TypeError):
                self.retry_after[key] = now + timedelta(minutes=5)
                return self.fallback(cached, group_id)

    async def day(self, day: date, force=False):
        university, note, changed = await self.university(day, force)
        result = [e for e in university if e.start.date() == day]
        anchor = date.fromisoformat(self.store.get("cycle_anchor", self.anchor.isoformat()))
        result += personal_events(day, self.store.rules(), anchor, self.config.timezone, self.store.skipped)
        for item in self.store.items("event"):
            start = datetime.fromisoformat(item["due"])
            end = start + timedelta(minutes=item["duration"])
            # Include events crossing midnight in both days.
            if start.date() <= day <= (end - timedelta(microseconds=1)).date():
                result.append(Event("plan:" + str(item["id"]), item["title"], start, end, "plan"))
        adjusted = []
        for event in result:
            patch = self.store.override(event.uid + '|' + event.start.date().isoformat())
            if patch:
                event = replace(event, title=patch['title'], start=datetime.fromisoformat(patch['start']),
                                end=datetime.fromisoformat(patch['end']), cancelled=patch.get('cancelled', False), all_day=False,
                                details=event.details + ' · личная правка')
            adjusted.append(event)
        return sorted(adjusted, key=lambda e: (e.start, e.title)), note, changed

    async def upcoming(self, now, days=7):
        events, notes = {}, []
        for offset in range(days):
            entries, note, _ = await self.day(now.date() + timedelta(days=offset))
            for entry in entries:
                if not entry.cancelled and not entry.all_day and entry.end > now:
                    events[(entry.uid, entry.start)] = entry
            if note and note not in notes:
                notes.append(note)
        return sorted(events.values(), key=lambda e: e.start), notes
