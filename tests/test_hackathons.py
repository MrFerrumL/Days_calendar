import json
from datetime import date, timedelta
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from campus_bot import hackathons
from campus_bot.bot import Bot
from campus_bot.config import ROOT
from campus_bot.jobs import notification_jobs
from campus_bot.miniapp import App
from campus_bot.models import at
from campus_bot.schedule import Schedule
from campus_bot.storage import Store
from test_bot import TZ, FakeTelegram, Fetch, config

SEED = json.loads((ROOT / "data/hackathons.json").read_text(encoding="utf-8"))
NOW = at(date(2026, 10, 23), "10:00", TZ)


def seeded():
    store = Store(":memory:")
    store.seed_projects(SEED, TZ)
    return store


def stage(store, project, text):
    return next(s for s in store.stages(project) if text in s["title"])


class SeedTests(unittest.TestCase):
    def test_all_hackathons_and_stages_are_seeded_once(self):
        store = seeded()
        self.assertEqual({p["id"] for p in store.projects()}, {"hackchange", "iikuznitsa", "mgts", "itone"})
        self.assertEqual(len(store.stages("hackchange")), 8)
        store.seed_projects(SEED, TZ)
        self.assertEqual(len(store.stages("hackchange")), 8)
        submit = stage(store, "hackchange", "Сдать решение")
        self.assertEqual(submit["due"], "2026-11-16T14:00:00+03:00")
        store.close()

    def test_deleted_hackathon_is_not_recreated(self):
        store = seeded()
        store.delete_project("mgts")
        store.seed_projects(SEED, TZ)
        self.assertIsNone(store.project("mgts"))
        self.assertEqual(store.stages("mgts"), [])
        store.close()

    def test_old_database_gets_new_columns_and_keeps_tasks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                title TEXT NOT NULL, due TEXT NOT NULL, duration INTEGER NOT NULL DEFAULT 0,
                recurrence TEXT NOT NULL DEFAULT 'once', done INTEGER NOT NULL DEFAULT 0);
                INSERT INTO items(kind,title,due) VALUES ('task','Старая задача','2026-11-01T12:00:00+03:00');""")
            db.commit()
            db.close()
            store = Store(path)
            try:
                item = store.items()[0]
                self.assertEqual((item["title"], item["project"], item["note"], item["all_day"]), ("Старая задача", None, "", 0))
                store.seed_projects(SEED, TZ)
                self.assertEqual(len(store.items("task")), 1 + 11)
            finally:
                store.close()


class TimelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = seeded()
        self.schedule = Schedule(self.store, config(), Fetch())

    async def asyncTearDown(self):
        self.store.close()

    async def test_events_land_in_calendar_including_multi_day(self):
        for day in (date(2026, 11, 26), date(2026, 11, 27)):
            events, _, _ = await self.schedule.day(day)
            coding = [e for e in events if e.source == "hackathon" and "48 часов" in e.title]
            self.assertEqual(len(coding), 1)
            self.assertTrue(coding[0].all_day)
            self.assertEqual(coding[0].details, "ИИкузница")
        events, _, _ = await self.schedule.day(date(2026, 11, 28))
        self.assertFalse(any("48 часов" in e.title for e in events))

    async def test_dropped_hackathon_leaves_calendar_and_reminders(self):
        project = self.store.project("itone")
        project["status"] = "dropped"
        self.store.put_project(project)
        events, _, _ = await self.schedule.day(date(2026, 11, 5))
        self.assertFalse(any(e.source == "hackathon" for e in events))
        projects = {p["id"]: p for p in self.store.projects()}
        jobs = notification_jobs(at(date(2026, 11, 5), "17:00", TZ), [], self.store.items(), [30], projects)
        self.assertFalse(any("ссылку" in j.text for j in jobs))

    def test_hackathon_deadlines_warn_three_days_ahead(self):
        projects = {p["id"]: p for p in self.store.projects()}
        item = stage(self.store, "hackchange", "Сдать решение")
        jobs = [j for j in notification_jobs(NOW, [], [item], [30], projects)]
        self.assertEqual([(j.due - at(date(2026, 11, 16), "14:00", TZ)) / timedelta(hours=1) for j in jobs], [-72, -24, -2, 0])
        self.assertIn("Hack&Change 2026", jobs[0].text)
        self.assertIn("строго до 14:00", jobs[0].text)

    def test_clash_between_hackathons(self):
        self.assertEqual(hackathons.clash_lines(self.store, NOW),
                         ["26.11 · ИИкузница: 48 часов разработки ↔ МГТС «Код города»: Финал"])

    def test_event_check_is_shared_with_calendar(self):
        meetup = stage(self.store, "itone", "Митап")
        hackathons.set_done(self.store, meetup, True)
        self.assertTrue(self.store.checked(f"plan:{meetup['id']}|2026-11-05"))
        project = next(p for p in hackathons.overview(self.store, NOW) if p["id"] == "itone")
        self.assertEqual(project["done_count"], 1)

    def test_outlook_skips_today_and_done(self):
        now = at(date(2026, 11, 5), "08:00", TZ)
        text = hackathons.outlook(self.store, now)
        self.assertNotIn("Митап", text)
        self.assertIn("08.11 23:59", text)
        hackathons.set_done(self.store, stage(self.store, "hackchange", "Зарегистрироваться"), True)
        self.assertNotIn("Зарегистрироваться", hackathons.outlook(self.store, now))

    def test_next_stage_and_countdown(self):
        project = next(p for p in hackathons.overview(self.store, NOW) if p["id"] == "iikuznitsa")
        self.assertEqual(project["next"]["title"], "Проверить, что заявка одобрена")
        self.assertEqual(project["next"]["countdown"], "через 7 дн")
        late = at(date(2026, 10, 31), "09:00", TZ)
        project = next(p for p in hackathons.overview(self.store, late) if p["id"] == "iikuznitsa")
        self.assertEqual(project["next"]["countdown"], "срок прошёл")


class HackCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = seeded()
        self.telegram = FakeTelegram()
        self.bot = Bot(config(), self.store, Schedule(self.store, config(), Fetch()),
                       SimpleNamespace(today=self.fake_weather), self.telegram)
        self.bot.now = lambda: NOW

    async def asyncTearDown(self):
        self.store.close()

    async def fake_weather(self, *args):
        return "Тестовая погода"

    async def message(self, text):
        await self.bot.handle({"message": {"text": text, "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}})

    async def callback(self, data, message_id=7):
        await self.bot.handle({"callback_query": {"id": "1", "data": data, "from": {"id": 42},
                                                  "message": {"message_id": message_id, "chat": {"id": 42, "type": "private"}}}})

    async def test_overview_card_and_menu_button(self):
        await self.message("🏆 Хакатоны")
        text, kwargs = self.telegram.messages[-1][1:]
        self.assertIn("активных: 4", text)
        self.assertIn("Пересечения между хакатонами", text)
        self.assertIn("hk:open:hackchange", json.dumps(kwargs["reply_markup"]))
        await self.message("/hack hackchange")
        self.assertIn("строго до 14:00", self.telegram.messages[-1][1])

    async def test_check_button_edits_card_in_place(self):
        uid = stage(self.store, "hackchange", "Зарегистрироваться")["id"]
        await self.callback(f"hk:check:{uid}")
        self.assertTrue(stage(self.store, "hackchange", "Зарегистрироваться")["done"])
        edits = [kwargs for method, kwargs in self.telegram.calls if method == "editMessageText"]
        self.assertEqual(edits[-1]["message_id"], 7)
        self.assertIn("✅", edits[-1]["text"])
        await self.callback("hk:status:hackchange:submitted")
        self.assertEqual(self.store.project("hackchange")["status"], "submitted")

    async def test_new_hackathon_with_stages_and_link(self):
        await self.message("/hack new alfa Alfa-Hack 2026")
        await self.message("/hack alfa task 2026-11-20 18:00 Сдать MVP")
        await self.message("/hack alfa event 2026-11-21 12:00 90 Демо")
        await self.message("/hack alfa day 2026-11-22 Финал")
        await self.message("/hack alfa link https://example.com Кабинет")
        await self.message("/hack alfa status active")
        stages = self.store.stages("alfa")
        self.assertEqual([(s["kind"], s["duration"], s["all_day"]) for s in stages],
                         [("task", 0, 0), ("event", 90, 0), ("event", 1440, 1)])
        project = self.store.project("alfa")
        self.assertEqual((project["status"], project["links"][0]["url"]), ("active", "https://example.com"))

    async def test_bad_ids_and_links_are_rejected(self):
        await self.message("/hack new Плохой ID")
        await self.message("/hack hackchange link javascript:alert(1) x")
        self.assertIsNone(self.store.project("Плохой"))
        self.assertEqual(len(self.store.project("hackchange")["links"]), 3)

    async def test_today_shows_hackathon_outlook_and_task_titles(self):
        await self.message("/day 2026-10-30")
        self.assertIn("ИИкузница — Проверить, что заявка одобрена", self.telegram.messages[-1][1])
        await self.message("/today")
        self.assertIn("Скоро по хакатонам", self.telegram.messages[-1][1])


class HackAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = seeded()
        self.app = App(config())
        Schedule(self.store, config(), Fetch())

    async def asyncTearDown(self):
        self.store.close()

    async def test_state_contains_hackathons_and_clashes(self):
        state = await self.app.state(self.store, date(2026, 11, 23), date(2026, 11, 29))
        self.assertEqual(len(state["hackathons"]), 4)
        self.assertTrue(state["clashes"])
        self.assertTrue(any(e["source"] == "hackathon" and e["all_day"] for e in state["events"]))

    async def test_stage_create_update_check_and_delete(self):
        self.app.mutate(self.store, "stage", dict(project="mgts", kind="allday", title="Хакатон-разминка",
                                                  date="2026-11-10", days=2, note="онлайн"))
        item = stage(self.store, "mgts", "разминка")
        self.assertEqual((item["duration"], item["all_day"], item["note"]), (2880, 1, "онлайн"))
        self.app.mutate(self.store, "stage", dict(id=item["id"], project="mgts", kind="event", title="Разминка",
                                                  start="2026-11-10T18:00", duration=90))
        item = stage(self.store, "mgts", "Разминка")
        self.assertEqual((item["due"], item["all_day"]), ("2026-11-10T18:00:00+03:00", 0))
        self.app.mutate(self.store, "stage-check", dict(id=item["id"], done=True))
        self.assertTrue(self.store.checked(f"plan:{item['id']}|2026-11-10"))
        self.app.mutate(self.store, "delete-stage", dict(id=item["id"]))
        self.assertEqual(len(self.store.stages("mgts")), 4)

    async def test_invalid_stage_and_project_rejected(self):
        with self.assertRaises(ValueError):
            self.app.mutate(self.store, "stage", dict(project="nope", kind="task", title="x", start="2026-11-10T18:00"))
        with self.assertRaises(ValueError):
            self.app.mutate(self.store, "stage", dict(project="mgts", kind="allday", title="x", date="2026-11-10", days=30))
        with self.assertRaises(ValueError):
            self.app.mutate(self.store, "project", dict(title="x", kind="hackathon", status="registration",
                                                        links=[dict(title="x", url="javascript:alert(1)")]))

    async def test_project_create_status_and_delete(self):
        uid = self.app.mutate(self.store, "project", dict(title="Alfa-Hack", kind="hackathon", status="registration",
                                                          role="капитан", links=[dict(title="Сайт", url="https://example.com")]))["id"]
        self.app.mutate(self.store, "stage", dict(project=uid, kind="task", title="MVP", start="2026-11-20T18:00"))
        self.app.mutate(self.store, "hack-status", dict(id=uid, status="dropped"))
        state = await self.app.state(self.store, date(2026, 11, 16), date(2026, 11, 22))
        self.assertFalse(any(t["project"] == uid for t in state["tasks"]))
        self.app.mutate(self.store, "delete-project", dict(id=uid))
        self.assertIsNone(self.store.project(uid))
        self.assertFalse(any(i["project"] == uid for i in self.store.items(include_done=True)))


if __name__ == "__main__":
    unittest.main()
