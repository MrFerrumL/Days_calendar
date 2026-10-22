import argparse
import asyncio
from contextlib import contextmanager
from datetime import date, datetime
import logging
import os
from pathlib import Path
import sys

from .bot import Bot
from .config import Config
from .network import ServiceError, Telegram
from .schedule import Schedule
from .storage import Store
from .weather import Weather


@contextmanager
def process_lock(path: Path):
    """OS releases this lock after either graceful exit or a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("Бот уже запущен с этой папкой данных") from None
    try:
        yield
    finally:
        handle.close()


async def main(args):
    config = Config.load(require_token=not (args.preview or args.check or args.reset_offset))
    config.data_dir.mkdir(parents=True, exist_ok=True)
    with process_lock(config.data_dir / "bot.lock"):
        store = Store(config.data_dir / "bot.sqlite3")
        try:
            schedule = Schedule(store, config)
            weather = Weather(store, config.timezone)
            bot = Bot(config, store, schedule, weather, Telegram(config.token))
            if args.reset_offset:
                store.set("offset", 0)
                print("Telegram offset сброшен; планы и настройки сохранены.")
            elif args.preview:
                print(await bot.day_text(date.fromisoformat(args.preview)))
            elif args.check:
                today = datetime.now(config.timezone).date()
                events, note, _ = await schedule.day(today, force=True)
                print("Группа:", store.get("group_name"))
                print("Регулярных правил:", len(store.rules()))
                print("Событий на сегодня:", len(events))
                print(note or "Сайт расписания: OK")
                forecast = await weather.today()
                print(forecast)
                if note or "недоступна" in forecast:
                    return 1
            elif args.delete_webhook:
                await bot.telegram.call("deleteWebhook", drop_pending_updates=False)
                print("Webhook удалён. Запустите python -m campus_bot")
            else:
                from .miniapp import start_server
                server = start_server(config)
                try:
                    await bot.run()
                finally:
                    server.shutdown()
                    server.server_close()
        finally:
            store.close()
    return 0


def entry():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Личный Telegram-бот: вуз, занятия, планы и погода")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", metavar="YYYY-MM-DD", help="Показать день в терминале, токен не нужен")
    mode.add_argument("--check", action="store_true", help="Проверить живые источники без токена")
    mode.add_argument("--delete-webhook", action="store_true", help="Явно отключить старый webhook")
    mode.add_argument("--reset-offset", action="store_true", help="Сбросить Telegram offset после смены токена")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return asyncio.run(main(args))
    except KeyboardInterrupt:
        print("Бот остановлен.")
        return 0
    except (ValueError, ServiceError) as exc:
        print("Ошибка:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entry())
