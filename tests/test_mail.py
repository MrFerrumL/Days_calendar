from dataclasses import replace
from email import message_from_bytes, policy
from email.message import EmailMessage
import imaplib
import json
from types import SimpleNamespace
import unittest

from campus_bot import mail
from campus_bot.bot import Bot
from campus_bot.config import ROOT
from campus_bot.miniapp import App
from campus_bot.schedule import Schedule
from campus_bot.storage import Store
from test_bot import NOW, TZ, FakeTelegram, Fetch, config

SEED = json.loads((ROOT / "data/hackathons.json").read_text(encoding="utf-8"))


def letter(subject, sender="Иван Петров <ivan@example.com>", body="Привет! Встречаемся завтра в 10:00.",
           html=None, headers=None, msgid=None):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, "me@gmail.com", subject
    message["Date"] = "Fri, 23 Oct 2026 11:05:00 +0000"
    message["Message-ID"] = msgid or f"<{abs(hash((subject, sender, body)))}@test>"
    for key, value in (headers or {}).items():
        message[key] = value
    if html:
        message.set_content(html, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes(policy=policy.SMTP)


def parsed(raw):
    return message_from_bytes(raw, policy=policy.default)


class FakeIMAP:
    """Answers like imaplib does against Gmail: literals in tuples, UID in the FETCH metadata."""

    def __init__(self, messages=None, validity=7, gmail=True):
        self.messages, self.validity, self.primary = dict(messages or {}), validity, None
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.fail_login, self.commands, self.readonly = False, [], None

    def __call__(self, host, port, timeout):
        self.commands.append(("CONNECT", host, port))
        return self

    def login(self, user, password):
        if self.fail_login:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.validity).encode()]

    def status(self, box, items):
        return "OK", [b'"INBOX" (UIDNEXT %d)' % (max(self.messages, default=0) + 1)]

    def uid(self, command, *args):
        self.commands.append((command,) + args)
        if command == "SEARCH":
            start = int(args[1].split(":")[0])
            # Like real IMAP, "n:*" returns the last message even when n is past the end.
            uids = [u for u in sorted(self.messages) if u >= start] or sorted(self.messages)[-1:]
            if "X-GM-RAW" in args:
                uids = [u for u in uids if self.primary is None or u in self.primary]
            return "OK", [" ".join(map(str, uids)).encode()]
        if command == "FETCH":
            data = []
            for number, uid in enumerate(map(int, args[0].split(",")), 1):
                raw = self.messages[uid]
                part = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if "HEADER" in args[1] else raw
                data += [(b"%d (UID %d BODY[] {%d}" % (number, uid, len(part)), part), b")"]
            return "OK", data
        raise AssertionError(command)

    def logout(self):
        self.commands.append(("LOGOUT",))


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.seed_projects(SEED, TZ)
        self.rule = mail.rules(self.store)

    def tearDown(self):
        self.store.close()

    def verdict(self, raw, primary=True):
        return mail.classify(parsed(raw), primary, self.rule)

    def test_personal_letter_in_primary_is_sent_but_newsletter_is_not(self):
        self.assertEqual(self.verdict(letter("Встреча"))["reason"], "«Основные»")
        newsletter = letter("Скидки недели", headers={"List-Unsubscribe": "<mailto:x@shop.ru>"})
        self.assertIsNone(self.verdict(newsletter))
        self.assertIsNone(self.verdict(letter("Встреча"), primary=False))

    def test_hackathon_letters_are_linked_to_their_card(self):
        by_subject = self.verdict(letter("Hack&Change: кейсы опубликованы", "Команда <news@other.org>",
                                         headers={"List-Unsubscribe": "<mailto:x@other.org>"}), primary=False)
        self.assertEqual((by_subject["project"], by_subject["reason"]), ("hackchange", "хакатон «Hack&Change 2026»"))
        by_domain = self.verdict(letter("Новости", "Info <info@иикузница.рф>"), primary=False)
        self.assertEqual(by_domain["project"], "iikuznitsa")
        shared = self.verdict(letter("Новый контест", "Codenrock <noreply@codenrock.com>"), primary=False)
        self.assertEqual((shared["project"], shared["reason"]), (None, "важный отправитель"))

    def test_keywords_rescue_promotions_and_mute_wins(self):
        self.assertIn("приглаш", self.verdict(letter("Приглашение на собеседование", "HR <hr@corp.ru>"), primary=False)["reason"])
        mailing = {"List-Unsubscribe": "<mailto:u@media.ru>"}
        self.assertIsNone(self.verdict(letter("Результаты недели", "Media <news@media.ru>", headers=mailing), primary=False))
        self.assertIsNotNone(self.verdict(letter("Новый хакатон по ML", "Media <news@media.ru>", headers=mailing), primary=False))
        self.rule["mute"] = ["corp.ru"]
        self.assertIsNone(self.verdict(letter("Приглашение на собеседование", "HR <hr@mail.corp.ru>"), primary=False))

    def test_robots_spam_and_sensitive(self):
        self.assertIsNone(self.verdict(letter("Undelivered", "MAILER-DAEMON@gmail.com")))
        self.assertIsNone(self.verdict(letter("Встреча", headers={"X-Spam-Flag": "YES"})))
        self.assertTrue(self.verdict(letter("Код подтверждения: 123456"))["sensitive"])

    def test_text_is_cleaned_and_secrets_are_not_forwarded(self):
        html = ("<html><head><style>p{color:red}</style></head><body><p>Решения принимаются до 16.11</p>"
                "<p>Ссылка: https://codenrock.com/contests/hackchange-2026/tasks?utm_source=mail&amp;utm_campaign=x</p></body></html>")
        text = mail.body_text(parsed(letter("x", html=html)))
        self.assertIn("Решения принимаются до 16.11", text)
        self.assertNotIn("color", text)
        self.assertIn("https://codenrock.com/…", text)
        reply = mail.body_text(parsed(letter("Re", body="Ок, буду\n\nOn Fri, 23 Oct 2026 Иван wrote:\n> старое")))
        self.assertEqual(reply, "Ок, буду")
        verdict = self.verdict(letter("Код подтверждения", body="Ваш код 123456"))
        text, markup = mail.letter(parsed(letter("Код подтверждения", body="Ваш код 123456")), verdict, TZ)
        self.assertNotIn("123456", text.split("\n", 1)[1])
        self.assertIn("Открыть в Gmail", json.dumps(markup, ensure_ascii=False))

    def test_entries_are_normalized(self):
        self.assertEqual(mail.normalize("News@ИИкузница.РФ"), "news@xn--80ancabkw0c4a.xn--p1ai")
        self.assertEqual(mail.normalize(" Corp.RU "), "corp.ru")
        for bad in ("", "nodomain", "a@b", "@corp.ru"):
            with self.assertRaises(ValueError):
                mail.normalize(bad)


class MailboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.store.seed_projects(SEED, TZ)
        self.telegram = FakeTelegram()
        cfg = replace(config(), mail_user="pavel@gmail.com", mail_password="abcdabcdabcdabcd")
        self.bot = Bot(cfg, self.store, Schedule(self.store, cfg, Fetch()), SimpleNamespace(), self.telegram)
        self.bot.now = lambda: NOW
        self.imap = FakeIMAP({1: letter("Старое письмо")})
        self.bot.mail.connect = self.imap

    async def asyncTearDown(self):
        self.store.close()

    def texts(self):
        return [text for _, text, _ in self.telegram.messages]

    async def test_first_check_only_remembers_position(self):
        await self.bot.mail.check(NOW)
        self.assertEqual(self.store.get("mail_state"), {"validity": 7, "uid": 1})
        self.assertIn("pa…@gmail.com подключена", self.texts()[-1])
        self.assertEqual(len(self.telegram.messages), 1)

    async def test_only_important_new_letters_reach_chat_once_and_stay_unread(self):
        await self.bot.mail.check(NOW)
        self.imap.messages.update({
            2: letter("Встреча по проекту"),
            3: letter("Распродажа", "Shop <news@shop.ru>", headers={"List-Unsubscribe": "<mailto:u@shop.ru>"}),
            4: letter("Hack&Change: старт 9 ноября", "Changellenge <team@changellenge.com>"),
            5: letter("Выигрыш!", headers={"X-Spam-Flag": "YES"}),
        })
        self.imap.primary = {2, 3, 5}
        self.assertEqual(await self.bot.mail.check(NOW), 2)
        sent = self.texts()[1:]
        self.assertTrue(sent[0].startswith("📬 Иван Петров · ivan@example.com\nВстреча по проекту"))
        self.assertIn("хакатон «Hack&Change 2026»", sent[1])
        markup = json.dumps(self.telegram.messages[-1][2]["reply_markup"], ensure_ascii=False)
        self.assertIn("hk:open:hackchange", markup)
        self.assertIn("mail:mute:changellenge.com", markup)
        self.assertTrue(self.imap.readonly)
        self.assertFalse(any(c[0] == "STORE" for c in self.imap.commands))
        self.assertEqual(await self.bot.mail.check(NOW), 0)
        self.assertEqual(len(self.telegram.messages), 3)

    async def test_failed_telegram_send_is_retried_without_duplicates(self):
        await self.bot.mail.check(NOW)
        self.imap.messages.update({2: letter("Первое"), 3: letter("Второе")})
        sent = []

        async def flaky(chat_id, text, **kwargs):
            if "Второе" in text and not sent:
                sent.append(text)
                raise mail.ServiceError("Telegram", 429, 1)
            self.telegram.messages.append((chat_id, text, kwargs))
        self.telegram.send = flaky
        await self.bot.mail.check(NOW)
        self.assertEqual(self.store.get("mail_state")["uid"], 2)
        await self.bot.mail.check(NOW)
        self.assertEqual(sum("Первое" in t for t in self.texts()), 1)
        self.assertEqual(sum("Второе" in t for t in self.texts()), 1)
        self.assertEqual(self.store.get("mail_state")["uid"], 3)

    async def test_backlog_is_capped(self):
        await self.bot.mail.check(NOW)
        self.imap.messages.update({uid: letter(f"Письмо {uid}") for uid in range(2, 17)})
        await self.bot.mail.check(NOW)
        self.assertEqual(len(self.telegram.messages), 1 + mail.SEND_LIMIT + 1)
        self.assertIn("И ещё 5 важных писем", self.texts()[-1])

    async def test_login_error_is_reported_once_and_new_mailbox_is_not_flooded(self):
        self.imap.fail_login = True
        await self.bot.mail.check(NOW)
        await self.bot.mail.check(NOW)
        self.assertEqual(sum("пароль приложения" in t for t in self.texts()), 1)
        self.assertNotIn("abcdabcd", " ".join(self.texts()))
        self.imap.fail_login = False
        await self.bot.mail.check(NOW)
        self.assertIsNone(self.store.get("mail_error"))
        self.imap.validity = 8
        self.imap.messages.update({2: letter("После смены ящика")})
        await self.bot.mail.check(NOW)
        self.assertFalse(any("После смены ящика" in t for t in self.texts()))

    async def test_scheduler_checks_mail_on_interval_and_respects_off(self):
        self.store.set("subscribed", True)
        await self.bot.scheduler.tick(NOW)
        self.assertIsNotNone(self.store.get("mail_state"))
        self.assertFalse(self.bot.mail.due(NOW))
        await self.bot.handle({"message": {"text": "/mail off", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}})
        self.assertFalse(self.bot.mail.due(NOW.replace(hour=23)))

    async def test_commands_and_mute_button(self):
        async def message(text):
            await self.bot.handle({"message": {"text": text, "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}})
        await message("/mail vip HR@Corp.ru")
        await message("/set mail 5")
        await self.bot.handle({"callback_query": {"id": "1", "data": "mail:mute:shop.ru", "from": {"id": 42},
                                                  "message": {"message_id": 3, "chat": {"id": 42, "type": "private"}}}})
        self.assertEqual(self.store.get("mail_vip"), ["hr@corp.ru"])
        self.assertEqual(self.store.get("mail_mute"), ["shop.ru"])
        self.assertEqual(self.store.get("mail_minutes"), 5)
        await message("/mail")
        status = self.texts()[-1]
        self.assertIn("pa…@gmail.com · включена", status)
        self.assertIn("Не присылать: shop.ru", status)

    async def test_unconfigured_mail_explains_setup(self):
        self.bot.config = config()
        await self.bot.handle({"message": {"text": "/mail check", "from": {"id": 42}, "chat": {"id": 42, "type": "private"}}})
        self.assertIn("пароль приложения", self.texts()[-1])
        self.assertFalse(self.bot.mail.due(NOW))


class MailAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_toggle_and_masked_status(self):
        store = Store(":memory:")
        app = App(replace(config(), mail_user="pavel@gmail.com", mail_password="x"))
        Schedule(store, config(), Fetch())
        app.mutate(store, "settings", {"mail_enabled": False})
        self.assertFalse(store.get("mail_enabled"))
        self.assertEqual(app.mail(store)["address"], "pa…@gmail.com")
        with self.assertRaises(ValueError):
            app.mutate(store, "settings", {"mail_enabled": "yes"})
        store.close()


if __name__ == "__main__":
    unittest.main()
