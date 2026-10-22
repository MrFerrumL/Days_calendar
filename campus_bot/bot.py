import asyncio
from datetime import date, datetime, timedelta
import logging
import os
from uuid import uuid4

from .jobs import Scheduler, occurrences
from .models import DAY_CODES, WEEKDAYS, at, clock, free_slots, parse_day
from .network import ServiceError
from .views import HELP, MENU, agenda, event_line

log = logging.getLogger(__name__)
BUTTONS = {"📅 Сегодня": "/today", "🌅 Завтра": "/tomorrow", "⏭ Следующее": "/next",
           "🌤 Погода": "/weather", "🕊 Свободное время": "/free", "🎯 Задачи": "/tasks",
           "➕ Задача": "/add", "⚙️ Настройки": "/settings"}


def title(value):
    value = value.strip()
    if not 1 <= len(value) <= 300:
        raise ValueError("Название должно содержать от 1 до 300 символов")
    return value


class Bot:
    def __init__(self, config, store, schedule, weather, telegram):
        self.config, self.store, self.schedule = config, store, schedule
        self.weather, self.telegram = weather, telegram
        self.scheduler = Scheduler(self)

    def now(self):
        return datetime.now(self.config.timezone)

    async def day_text(self, day, weather=True, force=False):
        events, note, _ = await self.schedule.day(day, force)
        forecast = await self.weather.today() if weather and day == self.now().date() else ""
        return agenda(day, events, note, self.store, forecast)

    async def reply(self, text, **kwargs):
        await self.telegram.send(self.config.owner_id, text, **kwargs)

    async def handle(self, update):
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        if not message or message.get("chat", {}).get("type") != "private":
            return
        sender = (callback or message).get("from", {}).get("id")
        text = message.get("text", "").strip()
        if not callback and text.split(" ")[0].split("@")[0] == "/id":
            await self.telegram.send(message["chat"]["id"], f"Ваш Telegram ID: {sender}")
            return
        if not self.config.owner_id:
            if text == "/start":
                await self.telegram.send(message["chat"]["id"],
                                         "Для настройки отправьте /id, запишите число в OWNER_ID файла .env "
                                         "и перезапустите бота. Доступ к расписанию пока закрыт.")
            return
        if sender != self.config.owner_id or message["chat"]["id"] != self.config.owner_id:
            if callback:
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"], text="Личный бот")
            return
        if callback:
            try:
                await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
            except ServiceError as exc:
                if exc.status != 400:  # An expired callback can still contain a useful action.
                    raise
            data = callback.get("data", "")
            if data.startswith("done:"):
                text = "/done " + data.split(":")[1]
            elif data.startswith("snooze:"):
                uid = int(data.split(":")[1])
                due = self.now() + timedelta(hours=1)
                text = f"/move {uid} {due:%Y-%m-%d %H:%M}"
            elif data in ("/today", "/tomorrow", "/settings", "/pause", "/resume") or data.startswith(("/tasks ", "/reminders ")):
                text = data
            else:
                return
        text = BUTTONS.get(text, text)
        cmd, _, args = text.partition(" ")
        cmd, args = cmd.split("@")[0].lower(), args.strip()
        try:
            await self.command(cmd, args)
        except (ValueError, IndexError, KeyError):
            await self.reply("Не удалось разобрать команду. Проверьте дату ГГГГ-ММ-ДД, время ЧЧ:ММ "
                             "и аргументы. Примеры: /help")

    async def command(self, cmd, args):
        now, tz = self.now(), self.config.timezone
        today = now.date()
        if cmd == "/start":
            self.store.set("subscribed", True)
            self.store.set("notifications", True)
            await self.reply("Привет! Я собрал вуз, занятия и планы в один день.\n"
                             "Группа: " + self.store.get("group_name") + ". Время: Москва / Краснодар.\n"
                             "Время сводок, погоды и напоминаний можно менять в /settings.\n"
                             "Настройка: /settings · все возможности: /help", reply_markup=MENU)
            await self.reply(await self.day_text(today))
        elif cmd == "/app":
            url = self.store.get('mini_app_url') or os.getenv('MINI_APP_URL','')
            if url.startswith('https://'):
                await self.reply('📅 Ваш календарь, задачи и занятия',reply_markup={'inline_keyboard':[[
                    {'text':'Открыть приложение','web_app':{'url':url}}]]})
            else:
                await self.reply('Адрес Mini App ещё не настроен.')
        elif cmd == "/help":
            await self.reply(HELP, reply_markup=MENU)
        elif cmd in ("/today", "/tomorrow", "/day"):
            day = today if cmd == "/today" else today + timedelta(days=1) if cmd == "/tomorrow" else parse_day(args, today)
            await self.reply(await self.day_text(day), reply_markup={"inline_keyboard": [[
                {"text": "Сегодня", "callback_data": "/today"},
                {"text": "Завтра", "callback_data": "/tomorrow"}]]})
        elif cmd == "/week":
            for offset in range(7):
                await self.reply(await self.day_text(today + timedelta(days=offset), weather=False))
        elif cmd == "/weather":
            await self.reply(await self.weather.today())
        elif cmd == "/next":
            events, notes = await self.schedule.upcoming(now)
            current = [e for e in events if e.start <= now]
            following = next((e for e in events if e.start > now), None)
            parts = ["▶️ Сейчас\n" + "\n".join(event_line(e) for e in current)] if current else []
            if following:
                mins = int((following.start - now).total_seconds() / 60)
                parts.append(f"⏭ Следующее · {following.start:%d.%m} · через {mins} мин\n" + event_line(following))
            await self.reply("\n\n".join(parts + notes) or "В ближайшие 7 дней занятий и планов нет.")
        elif cmd == "/free":
            tokens = args.split()
            day = parse_day(tokens[0], today) if tokens else today
            minutes = int(tokens[1]) if len(tokens) > 1 else 30
            if not 5 <= minutes <= 900:
                raise ValueError("Недопустимая длительность")
            events, note, _ = await self.schedule.day(day)
            slots = free_slots(events, day, tz, minutes, now if day == today else None)
            lines = [f"{a:%H:%M}–{b:%H:%M} · {int((b-a).total_seconds()/60)} мин" for a, b in slots]
            result = f"🕊 Окна на {day:%d.%m}, от {minutes} мин\n" + ("\n".join(lines) or "Подходящих окон нет.")
            await self.reply(result + ("\n" + note + "\nОкна могут быть заняты парами, которые не удалось проверить." if note else ""))
        elif cmd == "/sync":
            _, note, changed = await self.schedule.day(today, force=True)
            _, note2, changed2 = await self.schedule.day(today + timedelta(days=7), force=True)
            await self.reply(note or note2 or ("🔄 Расписание обновлено, есть изменения." if changed or changed2 else "✅ Расписание проверено."))
        elif cmd in ("/add", "/plan"):
            if not args:
                await self.reply("Введите команду с датой, временем и названием:\n"
                                 "/add 2026-11-02 23:59 Сдать лабораторную\n"
                                 "/plan 2026-11-02 15:00 60 Подготовиться к зачёту")
                return
            tokens = args.split(maxsplit=3 if cmd == "/plan" else 2)
            due = at(parse_day(tokens[0], today), clock(tokens[1]), tz)
            duration = int(tokens[2]) if cmd == "/plan" else 0
            if due <= now or (cmd == "/plan" and not 5 <= duration <= 1440):
                await self.reply("Укажите будущее время и длительность плана от 5 до 1440 минут.")
                return
            text = title(tokens[3] if cmd == "/plan" else tokens[2])
            uid = self.store.add_item("event" if cmd == "/plan" else "task", text, due.isoformat(), duration)
            await self.reply(f"✅ Добавлено #{uid}: {text}\n{due:%d.%m.%Y %H:%M}")
            if cmd == "/plan":
                events, _, _ = await self.schedule.day(due.date())
                from .models import overlaps
                conflicts = [pair for pair in overlaps(events) if any(e.uid == f"plan:{uid}" for e in pair)]
                if conflicts:
                    await self.reply("⚠️ План пересекается с занятиями. Проверьте /day " + due.date().isoformat())
        elif cmd in ("/tasks", "/reminders"):
            items = self.store.items("reminder") if cmd == "/reminders" else [
                i for i in self.store.items() if i["kind"] == "task" or (
                    i["kind"] == "event" and datetime.fromisoformat(i["due"])+timedelta(minutes=i["duration"]) > now)]
            if not items:
                await self.reply("Пока ничего нет. /add — дедлайн, /plan — план, /remind — напоминание.")
                return
            page = int(args) if args else 1
            pages = (len(items)+5)//6
            if not 1 <= page <= pages:
                raise ValueError("Страница не найдена")
            lines, buttons = [f"📋 Страница {page}/{pages}"], []
            for item in items[(page-1)*6:page*6]:
                due = datetime.fromisoformat(item["due"])
                if item["recurrence"] != "once":
                    future = [d for d in occurrences(item, today, today+timedelta(days=7)) if d >= now]
                    due = future[0] if future else due
                label = {"task": "🎯 Дедлайн", "event": "🗓 План", "reminder": "🔔 Напоминание"}[item["kind"]]
                repeat = {"once": "", "daily": " · ежедневно", "weekly": " · еженедельно"}[item["recurrence"]]
                suffix = " · просрочено" if item["kind"] == "task" and due < now else ""
                lines.append(f"{label} #{item['id']}\n{item['title']}\n{due:%d.%m.%Y %H:%M}{repeat}{suffix}")
                buttons.append([{"text": f"✓ #{item['id']}", "callback_data": f"done:{item['id']}"},
                                {"text": f"#{item['id']} +1 час", "callback_data": f"snooze:{item['id']}"}])
            navigation = []
            if page > 1:
                navigation.append({"text": "← Назад", "callback_data": f"{cmd} {page-1}"})
            if page < pages:
                navigation.append({"text": "Дальше →", "callback_data": f"{cmd} {page+1}"})
            if navigation:
                buttons.append(navigation)
            await self.reply("\n\n".join(lines), reply_markup={"inline_keyboard": buttons})
        elif cmd == "/done":
            await self.reply("✅ Готово / отключено." if self.store.finish(int(args)) else "Активная запись не найдена.")
        elif cmd == "/move":
            uid, day, value = args.split()
            due = at(parse_day(day, today), clock(value), tz)
            if due <= now:
                raise ValueError("Дата в прошлом")
            await self.reply("✅ Перенесено." if self.store.move(int(uid), due.isoformat()) else "Активная запись не найдена.")
        elif cmd == "/remind":
            tokens = args.split()
            recurrence = tokens[0].lower()
            if recurrence == "daily":
                due = at(today, clock(tokens[1]), tz)
                if due <= now:
                    due += timedelta(days=1)
                text = title(" ".join(tokens[2:]))
            elif recurrence == "weekly":
                weekday = DAY_CODES[tokens[1].lower()]
                due = at(today + timedelta(days=(weekday - today.weekday()) % 7), clock(tokens[2]), tz)
                if due <= now:
                    due += timedelta(days=7)
                text = title(" ".join(tokens[3:]))
            else:
                due = at(parse_day(tokens[0], today), clock(tokens[1]), tz)
                recurrence, text = "once", title(" ".join(tokens[2:]))
                if due <= now:
                    raise ValueError("Дата в прошлом")
            uid = self.store.add_item("reminder", text, due.isoformat(), recurrence=recurrence)
            await self.reply(f"🔔 Напоминание #{uid} сохранено. Первое: {due:%d.%m.%Y %H:%M}")
        elif cmd == "/focus":
            tokens = args.split(maxsplit=1)
            minutes = int(tokens[0]) if tokens else 25
            if not 1 <= minutes <= 180:
                raise ValueError("Недопустимый таймер")
            text = title(tokens[1]) if len(tokens) > 1 else "Фокус-сессия"
            uid = self.store.add_item("reminder", "Фокус завершён: " + text,
                                      (now + timedelta(minutes=minutes)).isoformat())
            await self.reply(f"🍅 Фокус на {minutes} мин запущен · #{uid}. Напомню сделать перерыв.")
        elif cmd == "/lesson":
            await self.lesson(args)
        elif cmd in ("/skip", "/unskip"):
            day, uid = args.split()
            day = parse_day(day, today)
            if uid not in {r["id"] for r in self.store.rules()}:
                await self.reply("Такого правила нет. Список: /lesson list")
                return
            self.store.skip(uid, day.isoformat(), cmd == "/skip")
            await self.reply("✅ Исключение сохранено." if cmd == "/skip" else "✅ Исключение убрано.")
        elif cmd == "/settings":
            fields = [("Группа", self.store.get("group_name")), ("Сводки", self.store.get("digest_times")),
                      ("Погода", self.store.get("weather_times")), ("Завтра", self.store.get("evening_time")),
                      ("За минут до занятий", self.store.get("alert_minutes")),
                      ("Обновление сайта, мин", self.store.get("sync_minutes")),
                      ("Уведомления", self.store.get("notifications") and self.store.get("subscribed"))]
            await self.reply("⚙️ Настройки · Москва / Краснодар\n" + "\n".join(f"{k}: {v}" for k, v in fields)
                             + "\n\n/set digest 07:30,13:00\n/set weather 08:00\n/set evening 21:00\n"
                             "/set alerts 30,10\n/set sync 180\nДля отключения укажите off.\n"
                             "/pause — выключить все уведомления; /resume — включить.",
                             reply_markup={"inline_keyboard": [[{"text": "Пауза", "callback_data": "/pause"},
                                                                  {"text": "Включить", "callback_data": "/resume"}]]})
        elif cmd == "/set":
            await self.setting(args)
        elif cmd == "/group":
            if args not in ("1", "2"):
                raise ValueError("Подгруппа 1 или 2")
            self.store.set("group_id", 2490 + int(args))
            self.store.set("group_name", "ИИ21/" + args)
            self.store.set("pending_change", None)
            await self.reply("✅ Группа: ИИ21/" + args)
        elif cmd in ("/pause", "/resume", "/stop"):
            self.store.set("notifications", cmd == "/resume")
            if cmd == "/stop":
                self.store.set("subscribed", False)
            elif cmd == "/resume":
                self.store.set("subscribed", True)
            await self.reply("🔔 Уведомления включены." if cmd == "/resume" else "🔕 Автоматические сообщения выключены.")
        else:
            await self.reply("Выберите кнопку или команду /help", reply_markup=MENU)

    async def lesson(self, args):
        action, _, rest = args.partition(" ")
        if action == "list":
            lines = []
            for rule in self.store.rules():
                days = ", ".join(WEEKDAYS[d] for d in rule["weekdays"])
                lines.append(f"{rule['id']} · {days} {rule['time']} · {rule['duration_minutes']} мин · "
                             f"{rule['cycle'].upper()}\n{rule['title']}")
            await self.reply("📚 Правила занятий\n\n" + "\n\n".join(lines))
        elif action == "delete":
            await self.reply("✅ Правило удалено." if self.store.delete_rule(rest.strip()) else "Правило не найдено.")
        elif action == "add":
            days, value, duration, cycle, text = rest.split(maxsplit=4)
            weekdays = sorted({DAY_CODES[d.lower()] for d in days.split(",")})
            duration = int(duration)
            if not 5 <= duration <= 1440 or cycle.lower() not in ("all", "a", "b"):
                raise ValueError("Неверная длительность или цикл")
            uid = "custom-" + uuid4().hex[:8]
            self.store.put_rule(dict(id=uid, weekdays=weekdays, time=clock(value), duration_minutes=duration,
                                     cycle=cycle.lower(), title=title(text)))
            await self.reply("✅ Правило добавлено: " + uid)
        else:
            await self.reply("/lesson list\n/lesson add mon,wed 17:00 120 all Моё занятие\n/lesson delete ID")

    async def setting(self, args):
        name, value = args.split(maxsplit=1)
        if name in ("digest", "weather"):
            values = [] if value == "off" else sorted({clock(v.strip()) for v in value.split(",")})
            if len(values) > 6:
                raise ValueError("Не более 6 отправок в день")
            self.store.set(name + "_times", values)
        elif name == "evening":
            self.store.set("evening_time", None if value == "off" else clock(value))
        elif name == "alerts":
            values = [] if value == "off" else sorted({int(v.strip()) for v in value.split(",")}, reverse=True)
            if len(values) > 5 or any(v < 1 or v > 1440 for v in values):
                raise ValueError("От 1 до 1440 минут, до 5 интервалов")
            self.store.set("alert_minutes", values)
        elif name == "sync":
            minutes = int(value)
            if not 15 <= minutes <= 1440:
                raise ValueError("От 15 до 1440 минут")
            self.store.set("sync_minutes", minutes)
            self.scheduler.next_sync = None
        elif name == "anchor":
            anchor = date.fromisoformat(value)
            if anchor.weekday() != 0:
                raise ValueError("Нужен понедельник")
            self.store.set("cycle_anchor", value)
        else:
            raise ValueError("Неизвестная настройка")
        await self.reply("✅ Настройка сохранена.")

    async def run(self):
        identity = await self.telegram.call("getMe")
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("У бота установлен webhook. Для перехода на polling запустите --delete-webhook.")
        log.info("Бот @%s запущен; OWNER_ID %s", identity["username"], "настроен" if self.config.owner_id else "не настроен")
        await self.telegram.call("setMyCommands", commands=[
            {"command": name, "description": desc} for name, desc in
            [("app", "Открыть приложение"), ("today", "План на сегодня"), ("tomorrow", "План на завтра"), ("next", "Следующее занятие"),
             ("tasks", "Задачи и планы"), ("weather", "Погода Краснодара"),
             ("settings", "Настройки"), ("help", "Все команды"), ("id", "Мой Telegram ID")]])
        worker = asyncio.create_task(self.scheduler.run())
        backoff = 1
        try:
            while True:
                try:
                    updates = await self.telegram.call("getUpdates", offset=self.store.get("offset", 0),
                                                        timeout=30, allowed_updates=["message", "callback_query"])
                    backoff = 1
                    for update in updates:
                        # Persist acceptance before effects: a failed reply must not create a task twice.
                        # A crash during handling can lose this one command; the user can safely retry.
                        self.store.set("offset", update["update_id"] + 1)
                        try:
                            await self.handle(update)
                        except ServiceError:
                            raise
                        except Exception as exc:
                            log.error("Не обработана команда: %s", type(exc).__name__)
                            # Advance past a malformed/unsupported update to avoid a poison-message loop.
                except ServiceError as exc:
                    if exc.status in (401, 409):
                        raise ValueError("Проверьте BOT_TOKEN и убедитесь, что запущен только один экземпляр.") from None
                    delay = min(60, max(backoff, exc.retry_after))
                    log.warning("Telegram недоступен; повтор через %s с", delay)
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, 30)
        finally:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
