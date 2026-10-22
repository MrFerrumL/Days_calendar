import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from campus_bot.bot import Bot
from campus_bot.config import Config, ROOT
from campus_bot.jobs import Scheduler, notification_jobs, occurrences
from campus_bot.models import Event, at, free_slots, overlaps
from campus_bot.network import ServiceError, Telegram
from campus_bot.schedule import Schedule, fingerprint, parse_university, personal_events
from campus_bot.storage import Store
from campus_bot.views import agenda
from campus_bot.weather import Weather

TZ = ZoneInfo("Europe/Moscow")
ANCHOR = date(2026, 10, 19)
NOW = at(date(2026, 10, 22), "17:30", TZ)
FIXTURES = Path(__file__).parent / "fixtures"
CUBE = json.loads((FIXTURES / "cube.json").read_text(encoding="utf-8"))
MET = json.loads((FIXTURES / "met.json").read_text(encoding="utf-8"))
PERSONAL = json.loads((ROOT / "data/personal_schedule.json").read_text(encoding="utf-8"))


def config():
    return Config("123:fake", 42, TZ, Path("runtime"), ROOT / "data/personal_schedule.json")


def lesson(start="18:00", end="20:00", uid="test"):
    return Event(uid, "Занятие", at(NOW.date(), start, TZ), at(NOW.date(), end, TZ), "personal")


class Fetch:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail
        self.payload = deepcopy(CUBE)

    async def __call__(self, url, service):
        self.calls.append(url)
        if self.fail:
            raise ServiceError(service, 503)
        await asyncio.sleep(0)
        q = parse_qs(urlsplit(url).query)
        payload = deepcopy(self.payload)
        payload["data"] = [r for r in payload["data"] if q["start_date"][0] <= r["date"] <= q["end_date"][0]]
        return payload


class FakeTelegram:
    def __init__(self):
        self.messages, self.calls = [], []
        self.fail = False

    async def send(self, chat_id, text, **kwargs):
        if self.fail:
            raise ServiceError("Telegram", 429, 90)
        self.messages.append((chat_id, text, kwargs))

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        return True


class PersonalTests(unittest.TestCase):
    def events(self, day):
        return personal_events(day, PERSONAL["lessons"], ANCHOR, TZ)

    def test_full_two_week_cycle(self):
        a = [len(self.events(ANCHOR + timedelta(days=i))) for i in range(7)]
        b = [len(self.events(ANCHOR + timedelta(days=7+i))) for i in range(7)]
        self.assertEqual(a, [3, 2, 3, 4, 3, 2, 5])
        self.assertEqual(b, [3, 2, 3, 4, 3, 2, 4])

    def test_repeat_across_month(self):
        for i in range(14):
            a, b = self.events(ANCHOR + timedelta(days=i)), self.events(ANCHOR + timedelta(days=i+14))
            self.assertEqual([(e.uid, e.start.time()) for e in a], [(e.uid, e.start.time()) for e in b])

    def test_all_sessions_and_lecture_last_two_hours(self):
        for i in range(14):
            self.assertTrue(all(e.end-e.start == timedelta(hours=2) for e in self.events(ANCHOR + timedelta(days=i))))

    def test_friday_shift_and_sunday_variation(self):
        friday = self.events(date(2026, 10, 30))
        self.assertEqual([e.start.strftime("%H:%M") for e in friday], ["14:30", "16:30", "18:30"])
        self.assertIn("Маркин", self.events(date(2026, 11, 1))[-1].title)

    def test_no_unconfirmed_history(self):
        self.assertEqual(self.events(ANCHOR - timedelta(days=1)), [])

    def test_skip_is_one_date_only(self):
        skip = lambda uid, day: uid == "mon18" and day == "2026-10-19"
        events = personal_events(ANCHOR, PERSONAL["lessons"], ANCHOR, TZ, skip)
        self.assertEqual(sum(e.cancelled for e in events), 1)
        self.assertFalse(any(e.cancelled for e in self.events(ANCHOR + timedelta(days=7))))


class UniversityTests(unittest.TestCase):
    def test_real_response_and_null_discipline_all_day(self):
        events = parse_university(CUBE, TZ, 2491)
        self.assertEqual(len(events), 37)
        k = next(e for e in events if e.start.date() == date(2026, 10, 22))
        self.assertTrue(k.all_day)
        self.assertEqual(k.title, "КСРС")
        self.assertEqual(events[0].end-events[0].start, timedelta(minutes=80))
        self.assertIn("128", events[0].details)

    def test_cancelled_status_and_no_false_changes_for_completed(self):
        doc = deepcopy(CUBE)
        doc["data"][0]["status"] = 3
        self.assertTrue(parse_university(doc, TZ, 2491)[0].cancelled)
        self.assertNotEqual(fingerprint(parse_university(CUBE, TZ, 2491)), fingerprint(parse_university(doc, TZ, 2491)))
        doc["data"][0]["status"] = 1
        self.assertEqual(fingerprint(parse_university(CUBE, TZ, 2491)), fingerprint(parse_university(doc, TZ, 2491)))

    def test_different_group_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_university(CUBE, TZ, 2492)

    def test_unknown_schema_or_private_schedule_is_rejected(self):
        for doc in ({"status": "CLOSED", "data": []}, {"status": "AVAILABLE"}, [], {}):
            with self.assertRaises(ValueError):
                parse_university(doc, TZ, 2491)


class TimeTests(unittest.TestCase):
    def test_merge_overlapping_busy_slots(self):
        events = [lesson("10:00", "12:00"), lesson("11:00", "13:00", "b")]
        slots = free_slots(events, NOW.date(), TZ, 30)
        self.assertEqual([(a.hour, b.hour) for a, b in slots], [(8, 10), (13, 23)])
        self.assertEqual(len(overlaps(events)), 1)

    def test_cancelled_and_all_day_do_not_block_gaps(self):
        e = lesson()
        events = [replace(e, cancelled=True), replace(e, all_day=True)]
        self.assertEqual(len(overlaps(events)), 0)
        self.assertEqual(free_slots(events, NOW.date(), TZ), [(at(NOW.date(), "08:00", TZ), at(NOW.date(), "23:00", TZ))])

    def test_adjacent_lessons_have_no_conflict(self):
        self.assertFalse(overlaps([lesson("10:00", "12:00"), lesson("12:00", "14:00")]))

    def test_free_slots_exclude_elapsed_time(self):
        slots = free_slots([lesson()], NOW.date(), TZ, 30, NOW)
        self.assertEqual(slots[0], (NOW, at(NOW.date(), "18:00", TZ)))

    def test_cross_midnight_reminder(self):
        event = Event("night", "Ночь", at(NOW.date()+timedelta(days=1), "00:10", TZ),
                      at(NOW.date()+timedelta(days=1), "02:10", TZ), "personal")
        jobs = notification_jobs(at(NOW.date(), "23:40", TZ), [event], [], [30])
        self.assertEqual(jobs[0].due, at(NOW.date(), "23:40", TZ))

    def test_all_day_cancelled_and_started_have_no_alert(self):
        events = [replace(lesson(), all_day=True), replace(lesson(), cancelled=True), lesson("16:00", "18:00")]
        self.assertEqual(notification_jobs(NOW, events, [], [30]), [])

    def test_daily_weekly_occurrences_respect_start_date(self):
        item = dict(due=at(date(2026, 10, 26), "09:00", TZ).isoformat(), recurrence="weekly")
        self.assertEqual(occurrences(item, date(2026, 10, 20), date(2026, 11, 2)),
                         [at(date(2026, 10, 26), "09:00", TZ), at(date(2026, 11, 2), "09:00", TZ)])
        item["recurrence"] = "daily"
        self.assertEqual(len(occurrences(item, date(2026, 10, 26), date(2026, 10, 28))), 3)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.fetch = Fetch()
        self.schedule = Schedule(self.store, config(), self.fetch)

    async def asyncTearDown(self):
        self.store.close()

    async def test_cache_and_concurrent_reads(self):
        a, b = await asyncio.gather(self.schedule.day(NOW.date()), self.schedule.day(NOW.date()))
        self.assertEqual(len(self.fetch.calls), 1)
        self.assertEqual(a, b)
        self.assertEqual(len(a[0]), 5)

    async def test_failed_refresh_retains_exact_week(self):
        await self.schedule.day(NOW.date())
        self.fetch.fail = True
        events, note, changed = await self.schedule.day(NOW.date(), force=True)
        self.assertIn("кэша", note)
        self.assertEqual(len(events), 5)
        self.assertFalse(changed)
        _, next_note, _ = await self.schedule.day(date(2026, 11, 16))
        self.assertIn("не означает", next_note)

    async def test_failed_source_is_throttled(self):
        self.fetch.fail = True
        await self.schedule.day(NOW.date())
        await self.schedule.day(NOW.date())
        self.assertEqual(len(self.fetch.calls), 1)

    async def test_change_notification_flag(self):
        await self.schedule.day(NOW.date())
        self.fetch.payload["data"][0]["place"]["name"] = "999"
        _, _, changed = await self.schedule.day(NOW.date(), force=True)
        self.assertTrue(changed)
        self.assertEqual(self.store.get("pending_change"), "ИИ21/1")

    async def test_unavailable_does_not_erase_cache(self):
        await self.schedule.day(NOW.date())
        self.fetch.payload["status"] = "CLOSED"
        events, note, _ = await self.schedule.day(NOW.date(), force=True)
        self.assertIn("кэша", note)
        self.assertEqual(len(events), 5)

    async def test_cross_midnight_plan_visible_both_days(self):
        self.store.add_item("event", "Ночной план", at(NOW.date(), "23:00", TZ).isoformat(), 120)
        for day in (NOW.date(), NOW.date()+timedelta(days=1)):
            events, _, _ = await self.schedule.day(day)
            self.assertEqual(sum(e.source == "plan" for e in events), 1)


class WeatherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")

    async def asyncTearDown(self):
        self.store.close()

    async def test_met_forecast_and_cache(self):
        calls = []
        async def fetch(url, service):
            calls.append(url)
            return deepcopy(MET)
        w = Weather(self.store, TZ, fetch)
        text = await w.today(NOW)
        self.assertIn("MET Norway", text)
        self.assertIn("Краснодар", text)
        self.assertIn("Ближайший прогноз", text)
        await w.today(NOW + timedelta(minutes=5))
        self.assertEqual(len(calls), 1)

    async def test_weather_failure_does_not_break_agenda(self):
        async def fetch(url, service):
            raise ServiceError(service)
        result = await Weather(self.store, TZ, fetch).today(NOW)
        self.assertIn("временно недоступна", result)

    async def test_fallback_openmeteo_when_met_fails(self):
        async def fetch(url, service):
            if "api.met.no" in url:
                raise ServiceError(service, 503)
            return dict(current=dict(temperature_2m=20, apparent_temperature=19, weather_code=61, wind_speed_10m=2),
                        daily=dict(time=["2026-10-22"], temperature_2m_min=[12], temperature_2m_max=[21],
                                   precipitation_probability_max=[80]))
        text = await Weather(self.store, TZ, fetch).today(NOW)
        self.assertIn("Open-Meteo", text)
        self.assertIn("зонт", text)

    async def test_stale_weather_is_labelled(self):
        self.store.cache_put("weather:2026-10-22", MET, (NOW-timedelta(hours=1)).isoformat())
        async def fetch(url, service):
            raise ServiceError(service)
        text = await Weather(self.store, TZ, fetch).today(NOW)
        self.assertIn("Кэш погоды", text)

    async def test_wrong_day_rejected(self):
        async def fetch(url, service):
            return MET
        text = await Weather(self.store, TZ, fetch).today(NOW + timedelta(days=10))
        self.assertIn("временно недоступна", text)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.telegram = FakeTelegram()
        self.bot = Bot(config(), self.store, Schedule(self.store, config(), Fetch()),
                       SimpleNamespace(today=self.fake_weather), self.telegram)
        self.bot.now = lambda: NOW

    async def fake_weather(self, *args):
        return "Тестовая погода"

    async def asyncTearDown(self):
        self.store.close()

    async def message(self, text, sender=42, chat_type="private"):
        await self.bot.handle({"message": {"text": text, "from": {"id": sender},
                                          "chat": {"id": sender, "type": chat_type}}})

    async def test_unauthorized_cannot_read_or_write(self):
        await self.message("/today", 99)
        await self.message("/add 2026-12-01 23:00 Secret", 99)
        await self.message("/today", chat_type="group")
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.store.items(), [])

    async def test_id_bootstrap_does_not_open_personal_data(self):
        self.bot.config = replace(config(), owner_id=0)
        await self.message("/id", 99)
        self.assertIn("99", self.telegram.messages[-1][1])
        await self.message("/today", 99)
        self.assertEqual(len(self.telegram.messages), 1)

    async def test_task_plan_move_and_complete(self):
        await self.message("/add 2026-11-02 23:59 Сдать лабораторную")
        await self.message("/plan 2026-11-02 15:00 60 Подготовка")
        self.assertEqual(len(self.store.items()), 2)
        await self.message("/move 1 2026-11-03 20:00")
        self.assertIn("2026-11-03T20:00", self.store.items()[0]["due"] if self.store.items()[0]["id"] == 1 else self.store.items()[1]["due"])
        await self.message("/done 1")
        self.assertEqual(len(self.store.items()), 1)

    async def test_malformed_and_past_tasks_do_not_write(self):
        for text in ("/add nonsense", "/add 2020-01-01 12:00 old", "/plan 2026-12-01 12:00 -1 nope"):
            await self.message(text)
        self.assertFalse(self.store.items())

    async def test_recurring_reminders(self):
        await self.message("/remind daily 09:00 Вода")
        await self.message("/remind weekly mon 10:00 Дедлайны")
        items = self.store.items()
        self.assertEqual(items[0]["recurrence"], "daily")
        self.assertIn("2026-10-23T09:00", items[0]["due"])
        self.assertEqual(items[1]["recurrence"], "weekly")

    async def test_lesson_management_and_skip(self):
        await self.message("/lesson add mon,wed 17:00 120 a Custom")
        self.assertEqual(len(self.store.rules()), 29)
        uid = next(r["id"] for r in self.store.rules() if r["title"] == "Custom")
        await self.message("/skip 2026-11-02 " + uid)
        self.assertTrue(self.store.skipped(uid, "2026-11-02"))
        await self.message("/lesson delete " + uid)
        self.assertEqual(len(self.store.rules()), 28)

    async def test_settings_validation_and_pause(self):
        await self.message("/set digest 07:00,13:30")
        await self.message("/set alerts 60,15")
        await self.message("/set sync 1")
        self.assertEqual(self.store.get("digest_times"), ["07:00", "13:30"])
        self.assertEqual(self.store.get("alert_minutes"), [60, 15])
        self.assertEqual(self.store.get("sync_minutes"), 180)
        await self.message("/pause")
        self.assertFalse(self.store.get("notifications"))
        await self.message("/resume")
        self.assertTrue(self.store.get("notifications"))

    async def test_group_switch(self):
        await self.message("/group 2")
        self.assertEqual(self.store.get("group_id"), 2492)

    async def test_task_pagination_and_next_recurring_date(self):
        for i in range(7):
            self.store.add_item("task", "Task " + str(i), (NOW+timedelta(days=1)).isoformat())
        await self.message("/tasks")
        self.assertEqual(len(self.telegram.messages), 1)
        self.assertIn("1/2", self.telegram.messages[-1][1])
        await self.message("/tasks 2")
        self.assertIn("2/2", self.telegram.messages[-1][1])
        self.store.add_item("reminder", "Daily", (NOW-timedelta(days=10)).isoformat(), recurrence="daily")
        await self.message("/reminders")
        self.assertIn("22.10.2026", self.telegram.messages[-1][1])

    async def test_scheduler_dedup_after_restart(self):
        async def text():
            return "Напоминание"
        await self.bot.scheduler.deliver("test", NOW, NOW, text)
        restarted = Scheduler(self.bot)
        await restarted.deliver("test", NOW, NOW, text)
        self.assertEqual(len(self.telegram.messages), 1)

    async def test_failed_delivery_not_marked_and_backed_off(self):
        async def text():
            return "Напоминание"
        self.telegram.fail = True
        await self.bot.scheduler.deliver("test", NOW, NOW, text)
        self.assertFalse(self.store.delivered("test"))
        self.telegram.fail = False
        await self.bot.scheduler.deliver("test", NOW, NOW+timedelta(seconds=20), text)
        self.assertEqual(self.telegram.messages, [])
        await self.bot.scheduler.deliver("test", NOW, NOW+timedelta(seconds=91), text)
        self.assertTrue(self.store.delivered("test"))

    async def test_disabled_notifications_send_nothing(self):
        self.store.set("subscribed", True)
        self.store.set("notifications", False)
        await self.bot.scheduler.tick(NOW)
        self.assertFalse(self.telegram.messages)

    async def test_task_deadline_offsets(self):
        uid = self.store.add_item("task", "Deadline", (NOW+timedelta(hours=24)).isoformat())
        jobs = notification_jobs(NOW, [], self.store.items(), [30])
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0].due, NOW)
        self.assertIn(str(uid), jobs[0].key)

    async def test_tick_completes_one_time_but_keeps_daily(self):
        self.store.set("subscribed", True)
        once = self.store.add_item("reminder", "Once", NOW.isoformat())
        daily = self.store.add_item("reminder", "Daily", NOW.isoformat(), recurrence="daily")
        await self.bot.scheduler.tick(NOW)
        ids = {i["id"] for i in self.store.items()}
        self.assertNotIn(once, ids)
        self.assertIn(daily, ids)
        before = len(self.telegram.messages)
        await self.bot.scheduler.tick(NOW+timedelta(seconds=20))
        self.assertEqual(len(self.telegram.messages), before)


class PersistenceTests(unittest.TestCase):
    def test_tasks_settings_and_deliveries_survive_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bot.db"
            s = Store(path)
            s.add_item("task", "Тест", NOW.isoformat())
            s.set("digest_times", ["08:15"])
            s.mark("sent", NOW.isoformat())
            s.close()
            s = Store(path)
            self.assertEqual(s.items()[0]["title"], "Тест")
            self.assertEqual(s.get("digest_times"), ["08:15"])
            self.assertTrue(s.delivered("sent"))
            s.close()

    def test_deleted_seed_rule_is_not_recreated_on_restart(self):
        store = Store(":memory:")
        store.seed_rules(PERSONAL)
        store.delete_rule("mon18")
        store.seed_rules(PERSONAL)
        self.assertEqual(len(store.rules()), 27)
        store.close()

    def test_unavailable_source_is_not_shown_as_no_classes(self):
        store = Store(":memory:")
        text = agenda(NOW.date(), [], "⚠️ Данные недоступны", store)
        self.assertIn("Данные не получены", text)
        store.close()


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_unicode_messages_are_split_without_data_loss(self):
        class Capture(Telegram):
            async def call(self, method, **kwargs):
                captured.append(kwargs["text"])
        captured = []
        text = ("📚 Кириллица\n" * 1000) + "конец"
        await Capture("123:fake").send(42, text)
        self.assertEqual("".join(captured), text)
        self.assertTrue(all(len(t) <= 3500 for t in captured))

    async def test_emoji_limit_uses_utf16_units(self):
        class Capture(Telegram):
            async def call(self, method, **kwargs):
                captured.append(kwargs["text"])
        captured = []
        await Capture("123:fake").send(42, "📚" * 4000)
        self.assertTrue(all(len(t.encode("utf-16-le"))//2 <= 3500 for t in captured))
        self.assertEqual("".join(captured), "📚" * 4000)


if __name__ == "__main__":
    unittest.main()
