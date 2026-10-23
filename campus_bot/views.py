from datetime import date, datetime

from .models import WEEKDAYS, overlaps


def event_line(event):
    prefix = "❌ " if event.cancelled else ""
    result = f"{prefix}{event.time_label} · {event.title}"
    return result + ("\n  " + event.details if event.details else "")


def agenda(day, events, note, store, weather="", outlook=""):
    anchor = date.fromisoformat(store.get("cycle_anchor", "2026-10-19"))
    cycle = "A" if ((day - anchor).days // 7) % 2 == 0 else "B"
    parts = [f"📅 {day:%d.%m.%Y} · {WEEKDAYS[day.weekday()]} · неделя {cycle}"]
    if weather:
        parts.append(weather)
    for source, label in [("university", "🎓 Вуз · " + store.get("group_name")),
                          ("personal", "📚 Занятия"), ("plan", "🗓 Мои планы"), ("hackathon", "🏆 Хакатоны")]:
        selected = [e for e in events if e.source == source]
        if source in ("plan", "hackathon") and not selected:
            continue
        text = "\n".join(event_line(e) for e in selected)
        if not text:
            text = "Данные не получены — проверьте сайт." if source == "university" and note else "Занятий нет."
        parts.append(label + "\n" + text)
    if note:
        parts.append(note)
    projects = {p["id"]: p for p in store.projects()}
    tasks = [t for t in store.items("task") if datetime.fromisoformat(t["due"]).date() <= day and (
        not t["project"] or projects.get(t["project"], {}).get("status") not in (None, "done", "dropped"))]
    if tasks:
        parts.append("🎯 Дедлайны и просроченные задачи\n" + "\n".join(
            f"#{t['id']} · {datetime.fromisoformat(t['due']):%d.%m %H:%M} · "
            + (projects[t["project"]]["title"] + " — " if t["project"] else "") + t["title"] for t in tasks))
    if outlook:
        parts.append("🏆 Скоро по хакатонам\n" + outlook)
    conflicts = overlaps(events)
    if conflicts:
        parts.append("⚠️ Пересечения\n" + "\n".join(
            f"{a.time_label} {a.title} ↔ {b.time_label} {b.title}" for a, b in conflicts))
    active = [e for e in events if not e.cancelled and not e.all_day]
    minutes = sum(int((e.end - e.start).total_seconds() / 60) for e in active)
    parts.append(f"📊 {len(active)} занятий/планов · суммарно {minutes // 60} ч {minutes % 60:02} мин"
                 + (" (пересечения посчитаны отдельно)" if conflicts else ""))
    return "\n\n".join(parts)


MENU = {"keyboard": [["📅 Сегодня", "🌅 Завтра"], ["⏭ Следующее", "🏆 Хакатоны"],
                     ["🎯 Задачи", "🕊 Свободное время"], ["🌤 Погода", "⚙️ Настройки"]],
        "resize_keyboard": True, "is_persistent": True}

HELP = """📔 Campus Day — команды

РАСПИСАНИЕ
/today — весь сегодняшний день + погода
/tomorrow — завтра
/day 2026-11-02 — произвольная дата
/week — ближайшие 7 дней
/next — текущее и следующее занятие
/free завтра 60 — свободные окна от 60 минут, 08:00–23:00
/weather — погода в Краснодаре
/sync — обновить расписание сейчас

ПЛАНЫ И ДЕДЛАЙНЫ
/add 2026-11-02 23:59 Сдать лабораторную
/plan 2026-11-02 15:00 60 Подготовка к зачёту
/tasks — список, кнопки «Готово» и «+1 час»
/done 3 — завершить задачу / отменить план или напоминание
/move 3 2026-11-03 18:00 — перенести
/focus 25 Математический анализ — таймер фокуса

ХАКАТОНЫ
/hack — все хакатоны, ближайшие этапы и пересечения
/hack hackchange — карточка: этапы, ссылки, кнопки статуса
/hack new alfa Alfa-Hack 2026 — новый хакатон (ID латиницей)
/hack alfa task 2026-11-16 14:00 Сдать решение — дедлайн
/hack alfa event 2026-11-09 14:00 120 Старт кейса — событие
/hack alfa day 2026-11-22 Финал — на весь день
/hack alfa link https://example.com Кабинет — ссылка
/hack alfa status final — registration, review, accepted,
  active, submitted, final, done, dropped
Дедлайны хакатонов напоминают за 3 дня, сутки, 2 часа и в срок.

ПОЧТА
/mail — статус и настройка: важные письма приходят в чат
/mail check — проверить сейчас
/mail vip example.com · /mail mute example.com
/mail off · /mail on · /set mail 5 — проверка раз в 5 минут

НАПОМИНАНИЯ
/remind 2026-11-02 18:00 Купить тетради
/remind daily 09:00 Выпить воды
/remind weekly mon 10:00 Проверить дедлайны
/reminders — список; /done ID — отключить

РЕГУЛЯРНЫЕ ЗАНЯТИЯ
/lesson list — правила и их ID
/lesson add mon,wed 17:00 120 all Моё занятие
/lesson add fri 16:00 120 a Мигалка
/lesson delete ID — удалить правило
/skip 2026-11-02 mon18 — отменить одно занятие
/unskip 2026-11-02 mon18 — вернуть его
Дни: mon tue wed thu fri sat sun. Цикл: all / a / b.

НАСТРОЙКИ
/set digest 07:30,13:00 — сводки на сегодня
/set weather 08:00,16:00 — погода отдельно
/set evening 21:00 — сводка на завтра
/set alerts 30,10 — за сколько минут до занятия
/set sync 180 — обновление сайта раз в N минут
/set anchor 2026-10-19 — понедельник начала цикла A
Вместо времени/интервалов можно off, кроме sync.
/group 1 или /group 2 — подгруппа ИИ21
/pause — выключить все автоматические сообщения
/resume — включить
/stop — отписаться; /start — снова подписаться
/id — ваш Telegram ID

Время московское (Краснодар, UTC+3). Бот работает, пока запущен процесс.
"""
