import json
from pathlib import Path
import sqlite3

DEFAULTS = {
    "group_id": 2491, "group_name": "ИИ21/1", "subscribed": False,
    "digest_times": ["07:30"], "weather_times": ["08:00"], "evening_time": "21:00",
    "alert_minutes": [30, 10], "sync_minutes": 180, "notifications": True,
}


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skips (rule_id TEXT, day TEXT, PRIMARY KEY(rule_id, day));
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, title TEXT NOT NULL,
              due TEXT NOT NULL, duration INTEGER NOT NULL DEFAULT 0,
              recurrence TEXT NOT NULL DEFAULT 'once', done INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, sent TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checks (key TEXT PRIMARY KEY, done INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS overrides (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Databases created before hackathons lack these columns; existing rows keep their meaning.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
        for name, kind in (("project", "TEXT"), ("note", "TEXT NOT NULL DEFAULT ''"),
                           ("all_day", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {name} {kind}")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else DEFAULTS.get(key, default)

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def cache_get(self, key):
        row = self.db.execute("SELECT value, fetched FROM cache WHERE key=?", (key,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def cache_put(self, key, value, fetched):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                            (key, json.dumps(value, ensure_ascii=False), fetched))

    def seed_rules(self, document):
        if self.get("rules_seeded", False):
            return
        with self.db:
            for rule in document["lessons"]:
                self.db.execute("INSERT OR IGNORE INTO rules VALUES (?, ?)",
                                (rule["id"], json.dumps(rule, ensure_ascii=False)))
            self.db.execute("INSERT INTO settings VALUES ('rules_seeded', 'true')")

    def rules(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT value FROM rules ORDER BY id")]

    def put_rule(self, rule):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO rules VALUES (?, ?)",
                            (rule["id"], json.dumps(rule, ensure_ascii=False)))

    def delete_rule(self, uid):
        with self.db:
            return self.db.execute("DELETE FROM rules WHERE id=?", (uid,)).rowcount > 0

    def skip(self, uid, day, skip=True):
        with self.db:
            if skip:
                self.db.execute("INSERT OR IGNORE INTO skips VALUES (?, ?)", (uid, day))
            else:
                self.db.execute("DELETE FROM skips WHERE rule_id=? AND day=?", (uid, day))

    def skipped(self, uid, day):
        return self.db.execute("SELECT 1 FROM skips WHERE rule_id=? AND day=?", (uid, day)).fetchone() is not None

    def add_item(self, kind, title, due, duration=0, recurrence="once", project=None, note="", all_day=False):
        with self.db:
            return self.db.execute(
                "INSERT INTO items(kind,title,due,duration,recurrence,project,note,all_day) VALUES (?,?,?,?,?,?,?,?)",
                (kind, title, due, duration, recurrence, project, note, int(all_day))).lastrowid

    def items(self, kind=None, include_done=False):
        rows = [dict(r) for r in self.db.execute("SELECT * FROM items ORDER BY due, id")]
        return [r for r in rows if (include_done or not r["done"]) and (kind is None or r["kind"] == kind)]

    def finish(self, uid):
        with self.db:
            return self.db.execute("UPDATE items SET done=1 WHERE id=? AND done=0", (uid,)).rowcount > 0

    def move(self, uid, due):
        with self.db:
            return self.db.execute("UPDATE items SET due=? WHERE id=? AND done=0", (due, uid)).rowcount > 0

    def projects(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT value FROM projects ORDER BY id")]

    def project(self, uid):
        row = self.db.execute("SELECT value FROM projects WHERE id=?", (uid,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_project(self, project):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?)",
                            (project["id"], json.dumps(project, ensure_ascii=False)))

    def delete_project(self, uid):
        with self.db:
            self.db.execute("DELETE FROM items WHERE project=?", (uid,))
            return self.db.execute("DELETE FROM projects WHERE id=?", (uid,)).rowcount > 0

    def stages(self, uid):
        return [dict(r) for r in self.db.execute("SELECT * FROM items WHERE project=? ORDER BY due, id", (uid,))]

    def seed_projects(self, document, tz):
        """Adds hackathons from the JSON once; edits and deletions in the bot are never overwritten."""
        from datetime import date, datetime, time
        seeded = set(self.get("projects_seeded", []))
        with self.db:
            for source in document["hackathons"]:
                if source["id"] in seeded:
                    continue
                stages = source.get("stages", [])
                project = {k: v for k, v in source.items() if k != "stages"}
                self.db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?)",
                                (project["id"], json.dumps(project, ensure_ascii=False)))
                for stage in stages:
                    if stage["kind"] == "allday":
                        due = datetime.combine(date.fromisoformat(stage["date"]), time(), tzinfo=tz)
                        kind, duration, all_day = "event", 1440 * stage.get("days", 1), 1
                    else:
                        due = datetime.fromisoformat(stage.get("due") or stage["start"]).replace(tzinfo=tz)
                        kind, duration, all_day = stage["kind"], stage.get("duration", 0), 0
                    self.db.execute(
                        "INSERT INTO items(kind,title,due,duration,project,note,all_day) VALUES (?,?,?,?,?,?,?)",
                        (kind, stage["title"], due.isoformat(), duration, project["id"], stage.get("note", ""), all_day))
                seeded.add(project["id"])
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('projects_seeded', ?)",
                            (json.dumps(sorted(seeded)),))

    def delivered(self, key):
        return self.db.execute("SELECT 1 FROM deliveries WHERE key=?", (key,)).fetchone() is not None

    def mark(self, key, sent):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?)", (key, sent))

    def close(self):
        self.db.close()

    def checked(self, key):
        row = self.db.execute('SELECT done FROM checks WHERE key=?', (key,)).fetchone()
        return bool(row[0]) if row else False

    def check(self, key, done):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO checks VALUES (?,?)', (key, int(done)))

    def override(self, key):
        row = self.db.execute('SELECT value FROM overrides WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_override(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO overrides VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
