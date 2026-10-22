"""Hackathons and championships: stages are ordinary tasks and calendar events bound to a project."""
from datetime import datetime, timedelta
import re

STATUSES = {"registration": "Регистрация", "review": "Заявка на проверке", "accepted": "Участвую",
            "active": "Идёт работа", "submitted": "Решение сдано", "final": "Финал",
            "done": "Завершён", "dropped": "Не участвую"}
FLOW = ("registration", "review", "accepted", "active", "submitted", "final", "done")
CLOSED = ("done", "dropped")
KINDS = {"hackathon": "Хакатон", "championship": "Кейс-чемпионат", "meetup": "Митап"}
PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,23}")


def window(item):
    start = datetime.fromisoformat(item["due"])
    return start, start + timedelta(minutes=item["duration"])


def check_key(item):
    return f"plan:{item['id']}|{datetime.fromisoformat(item['due']).date().isoformat()}"


def is_done(store, item):
    # Tasks close like any deadline; events keep their calendar checkmark so they stay visible.
    return bool(item["done"]) if item["kind"] == "task" else store.checked(check_key(item))


def set_done(store, item, done):
    if item["kind"] == "task":
        with store.db:
            store.db.execute("UPDATE items SET done=? WHERE id=?", (int(done), item["id"]))
    else:
        store.check(check_key(item), done)


def when(item):
    start, end = window(item)
    if item["all_day"]:
        last = (end - timedelta(minutes=1)).date()
        return f"{start:%d.%m}" + (f"–{last:%d.%m}" if last != start.date() else "") + " · весь день"
    if item["kind"] == "task":
        return f"до {start:%d.%m %H:%M}"
    return f"{start:%d.%m %H:%M}–{end:%H:%M}"


def countdown(item, now):
    start, end = window(item)
    if item["kind"] == "task" and start < now:
        return "срок прошёл"
    if item["kind"] != "task" and start <= now < end:
        return "идёт сейчас"
    if end <= now:
        return "прошло"
    if start.date() == now.date():
        return "сегодня"
    days = (start.date() - now.date()).days
    if days == 1:
        return "завтра"
    return f"через {days} дн"


def overview(store, now):
    result = []
    for project in store.projects():
        stages = []
        for item in store.stages(project["id"]):
            start, end = window(item)
            stages.append(dict(item, start=start.isoformat(), end=end.isoformat(), done=is_done(store, item),
                               when=when(item), countdown=countdown(item, now), all_day=bool(item["all_day"])))
        upcoming = [s for s in stages if not s["done"] and (s["kind"] == "task" or datetime.fromisoformat(s["end"]) > now)]
        result.append(dict(project, stages=stages, next=upcoming[0] if upcoming else None,
                           done_count=sum(s["done"] for s in stages), closed=project.get("status") in CLOSED,
                           status_label=STATUSES.get(project.get("status"), project.get("status", "")),
                           kind_label=KINDS.get(project.get("kind"), "Хакатон")))
    far = datetime.max.replace(tzinfo=now.tzinfo)
    return sorted(result, key=lambda p: (p["closed"], datetime.fromisoformat(p["next"]["start"]) if p["next"] else far))


def outlook(store, now, days=14, limit=5):
    """Upcoming stages after today; today's ones are already in the day plan."""
    rows = []
    for project in overview(store, now):
        if project["closed"]:
            continue
        for stage in project["stages"]:
            start = datetime.fromisoformat(stage["start"])
            if not stage["done"] and now.date() < start.date() <= now.date() + timedelta(days=days):
                rows.append((start, f"{stage['when']} · {project['title']} — {stage['title']} · {stage['countdown']}"))
    rows.sort(key=lambda row: row[0])
    return "\n".join(text for _, text in rows[:limit])


def clashes(store, now):
    spans = []
    for project in overview(store, now):
        if project["closed"]:
            continue
        for stage in project["stages"]:
            start, end = datetime.fromisoformat(stage["start"]), datetime.fromisoformat(stage["end"])
            if stage["done"] or end <= now or (stage["kind"] == "task" and start < now):
                continue
            spans.append((start, end if stage["kind"] != "task" else start, project, stage))

    def meet(a, b):
        if a[0] == a[1]:
            return b[0] <= a[0] < b[1]
        if b[0] == b[1]:
            return a[0] <= b[0] < a[1]
        return a[0] < b[1] and b[0] < a[1]

    return [(max(a[0], b[0]), a, b) for i, a in enumerate(spans) for b in spans[i + 1:]
            if a[2]["id"] != b[2]["id"] and meet(a, b)]


def clash_lines(store, now):
    return [f"{day:%d.%m} · {a[2]['title']}: {a[3]['title']} ↔ {b[2]['title']}: {b[3]['title']}"
            for day, a, b in clashes(store, now)]


def progress(done, total, width=8):
    filled = round(width * done / total) if total else 0
    return "▰" * filled + "▱" * (width - filled) + f" {done}/{total}"


def overview_text(store, now):
    projects = overview(store, now)
    active = [p for p in projects if not p["closed"]]
    if not projects:
        return ("🏆 Хакатонов пока нет.\n/hack new alfa Alfa-Hack 2026 — добавить; этапы и ссылки — в карточке "
                "или во вкладке «Хакатоны» приложения."), None
    parts = [f"🏆 Хакатоны и чемпионаты · активных: {len(active)}"]
    for project in active:
        lines = [f"{project['title']} · {project['status_label']}"]
        if project["next"]:
            stage = project["next"]
            lines.append(f"⏭ {stage['title']} — {stage['when']} · {stage['countdown']}")
        lines.append(progress(project["done_count"], len(project["stages"])))
        parts.append("\n".join(lines))
    closed = [p["title"] for p in projects if p["closed"]]
    if closed:
        parts.append("Завершённые: " + ", ".join(closed))
    warnings = clash_lines(store, now)
    if warnings:
        parts.append("⚠️ Пересечения между хакатонами\n" + "\n".join(warnings))
    buttons = [[{"text": ("🏁 " if p["closed"] else "🏆 ") + p["title"], "callback_data": "hk:open:" + p["id"]}]
               for p in projects]
    return "\n\n".join(parts), {"inline_keyboard": buttons}


def card(store, uid, now):
    project = next((p for p in overview(store, now) if p["id"] == uid), None)
    if not project:
        return None, None
    head = [f"🏆 {project['title']}", f"{project['kind_label']} · {project.get('organizer', '')}".rstrip(" ·"),
            "Статус: " + project["status_label"] + (f" · {project['role']}" if project.get("role") else "")]
    parts = ["\n".join(head)]
    if project.get("summary"):
        parts.append(project["summary"])
    if project.get("prize"):
        parts.append("🎁 " + project["prize"])
    stages = [f"Этапы · {progress(project['done_count'], len(project['stages']))}"]
    for stage in project["stages"]:
        mark = "✅" if stage["done"] else "⬜"
        tail = "" if stage["done"] or stage["countdown"] == "прошло" else f" · {stage['countdown']}"
        stages.append(f"{mark} {stage['when']} · {stage['title']}{tail}")
        if stage["note"] and not stage["done"]:
            stages.append("    " + stage["note"])
    parts.append("\n".join(stages) if project["stages"] else "Этапов пока нет: /hack " + uid + " task 2026-11-16 14:00 Сдать решение")
    if project.get("links"):
        parts.append("🔗 Ссылки\n" + "\n".join(f"{l['title']}: {l['url']}" for l in project["links"]))
    parts.append("Новый этап или ссылка — /help, раздел «Хакатоны», или вкладка в приложении.")
    buttons = [[{"text": f"✓ {stage['title'][:30]}", "callback_data": f"hk:check:{stage['id']}"}]
               for stage in [s for s in project["stages"] if not s["done"]][:4]]
    if project["closed"]:
        buttons.append([{"text": "Вернуть в работу", "callback_data": f"hk:status:{uid}:active"}])
    else:
        status = project.get("status")
        following = FLOW[FLOW.index(status) + 1] if status in FLOW[:-1] else "done"
        buttons.append([{"text": "→ " + STATUSES[following], "callback_data": f"hk:status:{uid}:{following}"},
                        {"text": "Не участвую", "callback_data": f"hk:status:{uid}:dropped"}])
    buttons.append([{"text": "← Все хакатоны", "callback_data": "hk:list"}])
    return "\n\n".join(parts), {"inline_keyboard": buttons}
