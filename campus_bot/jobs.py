import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from .models import at
from .network import ServiceError

log = logging.getLogger(__name__)


def occurrences(item, first_day, last_day):
    due = datetime.fromisoformat(item["due"])
    recurrence = item["recurrence"]
    if recurrence == "once":
        return [due] if first_day <= due.date() <= last_day else []
    result = []
    day = max(first_day, due.date())
    while day <= last_day:
        if recurrence == "daily" or (recurrence == "weekly" and day.weekday() == due.weekday()):
            result.append(datetime.combine(day, due.timetz()))
        day += timedelta(days=1)
    return result


@dataclass(frozen=True)
class Job:
    key: str
    due: datetime
    text: str


def notification_jobs(now, events, items, alert_minutes):
    jobs = []
    for event in events:
        if event.cancelled or event.all_day or event.start <= now:
            continue
        for lead in alert_minutes:
            due = event.start - timedelta(minutes=lead)
            jobs.append(Job(f"event:{event.uid}:{event.start.isoformat()}:{lead}", due,
                            f"⏰ Через {lead} мин · {event.time_label}\n{event.title}\n{event.details}"))
    for item in items:
        if item["kind"] == "task":
            due = datetime.fromisoformat(item["due"])
            for hours in (24, 2, 0):
                when = due - timedelta(hours=hours)
                label = f"через {hours} ч" if hours else "сейчас"
                jobs.append(Job(f"task:{item['id']}:{due.isoformat()}:{hours}", when,
                                f"🎯 Дедлайн {label} · #{item['id']}\n{item['title']}\n{due:%d.%m %H:%M}"))
        elif item["kind"] == "reminder":
            for due in occurrences(item, now.date() - timedelta(days=1), now.date()):
                jobs.append(Job(f"reminder:{item['id']}:{due.isoformat()}", due,
                                f"🔔 Напоминание · #{item['id']}\n{item['title']}"))
    return jobs


class Scheduler:
    def __init__(self, bot):
        self.bot = bot
        self.next_sync = None
        self.retry_at = {}

    async def deliver(self, key, due, now, make_text, grace=10):
        store = self.bot.store
        if not timedelta(0) <= now - due <= timedelta(minutes=grace):
            return
        if store.delivered(key) or now < self.retry_at.get(key, now):
            return
        try:
            text = await make_text()
            await self.bot.telegram.send(self.bot.config.owner_id, text)
            store.mark(key, now.isoformat())
            self.retry_at.pop(key, None)
        except ServiceError as exc:
            self.retry_at[key] = now + timedelta(seconds=max(60, exc.retry_after))
            if exc.status == 403:
                store.set("subscribed", False)
            log.warning("Доставка не выполнена: %s", exc)

    async def tick(self, now=None):
        bot, store = self.bot, self.bot.store
        now = now or datetime.now(bot.config.timezone)
        if not store.get("subscribed") or not store.get("notifications") or not bot.config.owner_id:
            return
        force = self.next_sync is None or now >= self.next_sync
        today, note, _ = await bot.schedule.day(now.date(), force=force)
        tomorrow, note2, _ = await bot.schedule.day(now.date() + timedelta(days=1), force=force)
        if force:
            self.next_sync = now + timedelta(minutes=store.get("sync_minutes"))
        if store.get("pending_change"):
            group = store.get("pending_change")
            async def change_text():
                return f"🔄 Изменилось расписание {group}.\n" + await bot.day_text(now.date(), weather=False)
            # Stable across retries until successfully delivered.
            key = "change:" + str(store.get("change_sequence", 0))
            await self.deliver(key, now, now, change_text)
            if store.delivered(key):
                store.set("pending_change", None)
                store.set("change_sequence", store.get("change_sequence", 0) + 1)
        for clock in store.get("digest_times"):
            await self.deliver(f"digest:{now.date()}:{clock}", at(now.date(), clock, bot.config.timezone), now,
                               lambda: bot.day_text(now.date()), grace=30)
        for clock in store.get("weather_times"):
            await self.deliver(f"weather:{now.date()}:{clock}", at(now.date(), clock, bot.config.timezone), now,
                               lambda: bot.weather.today(now), grace=30)
        evening = store.get("evening_time")
        if evening:
            await self.deliver(f"evening:{now.date()}:{evening}", at(now.date(), evening, bot.config.timezone), now,
                               lambda: bot.day_text(now.date() + timedelta(days=1), weather=False), grace=30)
        unique = {(e.uid, e.start): e for e in today + tomorrow}
        for job in notification_jobs(now, list(unique.values()), store.items(), store.get("alert_minutes")):
            async def text_for(job=job):
                return job.text + ("\n⚠️ Расписание вуза из кэша; проверьте сайт." if
                                   job.key.startswith("event:uni:") and (note or note2) else "")
            await self.deliver(job.key, job.due, now, text_for)
            if job.key.startswith("reminder:") and store.delivered(job.key):
                uid = int(job.key.split(":")[1])
                item = next((i for i in store.items("reminder") if i["id"] == uid), None)
                if item and item["recurrence"] == "once":
                    store.finish(uid)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # No traceback or request URL: secrets must not appear in logs.
                log.error("Ошибка планировщика: %s", type(exc).__name__)
            await asyncio.sleep(20)
