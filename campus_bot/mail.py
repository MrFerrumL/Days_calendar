"""Important mail → Telegram. Reads INBOX over IMAP read-only, so letters stay unread in the mailbox."""
import asyncio
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from email.utils import parseaddr, parsedate_to_datetime
import html
import imaplib
import logging
import re
import socket
from urllib.parse import quote, urlsplit

from .network import ServiceError

log = logging.getLogger(__name__)

VIP = ("codenrock.com", "changellenge.com", "cl-friends.com", "softline.ru", "softline.com", "mgts.ru",
       "it-one.ru", "kubsu.ru")
# Strong words pass even in mailing lists; the rest only in letters without newsletter headers.
STRONG = ("хакатон", "hackathon", "приглаш", "собеседован", "интервью", "стажир", "оффер")
KEYWORDS = STRONG + ("дедлайн", "deadline", "заявк", "регистрац", "подтвержд", "результат", "финал", "отбор",
                     "кейс", "задани", "трансляц", "митап", "расписан", "экзамен", "зачёт", "зачет", "пересдач")
SENSITIVE = ("код подтвержд", "код для входа", "код входа", "одноразов", "сброс пароля", "восстановлени",
             "verification code", "security code", "sign-in code", "login code", "password reset",
             "reset your password", "2fa", "двухфактор")
ROBOTS = ("mailer-daemon@", "postmaster@")
SEND_LIMIT = 10
HEADER_LIMIT = 60
BODY_BYTES = 262144


def idna(host):
    try:
        return host.encode("idna").decode()
    except UnicodeError:
        return host


def domain(address):
    return idna(address.rsplit("@", 1)[-1].lower().strip("."))


def normalize(entry):
    """Sender or domain from a command; Cyrillic domains are compared in punycode."""
    entry = entry.strip().lower()
    local, at, host = entry.rpartition("@")
    if not re.fullmatch(r"[^@\s]+\.[^@\s.]+", host) or len(entry) > 100 or (at and not local):
        raise ValueError("Нужен адрес или домен")
    return (local + "@" if at else "") + idna(host)


def site(url):
    """Registrable part of a link host: lk.studchamp.mgts.ru → mgts.ru."""
    host = idna((urlsplit(url).hostname or "").lower())
    return ".".join(host.split(".")[-2:])


def listed(address, entries):
    host = domain(address)
    full = address.rsplit("@", 1)[0].lower() + "@" + host
    return any(full == e or host == e or host.endswith("." + e) for e in entries)


def rules(store):
    """Plain data for the IMAP thread: no SQLite objects cross thread boundaries."""
    projects, owners = [], {}
    for project in store.projects():
        if project.get("status") == "dropped":
            continue
        name = project["title"]
        aliases = {name.lower()} | {w.strip("«»:,.").lower() for w in name.split()[:1] if len(w.strip("«»:,.")) >= 4}
        projects.append((project["id"], name, sorted(aliases)))
        for link in project.get("links", []):
            owners.setdefault(site(link["url"]), set()).add(project["id"])
    return dict(vip=sorted(set(VIP) | set(store.get("mail_vip", [])) | set(owners)),
                mute=store.get("mail_mute", []), projects=projects,
                owners={k: sorted(v)[0] for k, v in owners.items() if len(v) == 1})


def classify(message, primary, rule):
    """Returns why a letter matters, or None for newsletters, robots and muted senders."""
    name, address = sender(message)
    subject = str(message.get("Subject", "") or "")
    lowered = (subject + " " + name).lower()
    if not address or address.lower().startswith(ROBOTS) or listed(address, rule["mute"]):
        return None
    if str(message.get("X-Spam-Flag", "")).upper() == "YES":
        return None
    project = next((pid for pid, _, aliases in rule["projects"] if any(a in lowered for a in aliases)), None)
    project = project or next((pid for site_, pid in rule["owners"].items() if listed(address, [site_])), None)
    sensitive = any(word in lowered for word in SENSITIVE)
    title = next((name_ for pid, name_, _ in rule["projects"] if pid == project), "")
    if project:
        reason = "хакатон «" + title + "»"
    elif listed(address, rule["vip"]):
        reason = "важный отправитель"
    elif any(word in lowered for word in (KEYWORDS if not bulk(message) else STRONG)):
        reason = "ключевое слово «" + next(w for w in KEYWORDS if w in lowered) + "…»"
    elif primary and not bulk(message):
        reason = "«Основные»"
    else:
        return None
    return dict(reason=reason, project=project, project_title=title, sensitive=sensitive)


def bulk(message):
    precedence = str(message.get("Precedence", "")).lower()
    return bool(message.get("List-Unsubscribe") or message.get("List-Id")) or precedence in ("bulk", "list", "junk")


def sender(message):
    try:
        found = message["From"].addresses[0]
        return found.display_name or "", found.addr_spec or ""
    except (AttributeError, IndexError, TypeError, KeyError):
        name, address = parseaddr(str(message.get("From", "")))
        return name, address


def clean(text):
    lines = []
    for line in text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^(On .+ wrote:|.+ (написал|писал)\(?а?\)?:)$", stripped):
            break  # The rest is the quoted conversation.
        lines.append(stripped)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"https?://\S{50,}", lambda m: "https://" + (urlsplit(m.group()).hostname or "") + "/…", text)


def body_text(message, limit=700):
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        content = part.get_content() if part else ""
    except (KeyError, LookupError, ValueError, AttributeError):
        return ""
    if part and part.get_content_subtype() == "html":
        content = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", " ", content))
        content = "\n".join(" ".join(line.split()) for line in content.split("\n"))
    text = clean(content)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def letter(message, verdict, tz, gmail=True):
    name, address = sender(message)
    subject = " ".join(str(message.get("Subject", "") or "").split()) or "(без темы)"
    try:
        received = parsedate_to_datetime(str(message.get("Date"))).astimezone(tz)
        when = f"{received:%d.%m %H:%M}"
    except (TypeError, ValueError, IndexError):
        when = "время не указано"
    head = [f"📬 {name} · {address}" if name and name != address else f"📬 {address}", subject,
            f"🕒 {when} · {verdict['reason']}"]
    if verdict["sensitive"]:
        text = "🔐 Похоже на код входа или сброс пароля — текст не пересылаю, откройте почту."
    else:
        text = body_text(message) or "Текст письма пустой или во вложении."
    buttons = []
    msgid = str(message.get("Message-ID", "") or "").strip().strip("<>")
    if gmail and msgid:
        buttons.append([{"text": "Открыть в Gmail", "url": "https://mail.google.com/mail/u/0/#search/rfc822msgid%3A"
                         + quote(msgid, safe="")}])
    if verdict["project"]:
        buttons.append([{"text": "🏆 " + verdict["project_title"], "callback_data": "hk:open:" + verdict["project"]}])
    host = domain(address)
    if host and len(("mail:mute:" + host).encode()) <= 64:
        buttons.append([{"text": "🔕 Не присылать от " + host, "callback_data": "mail:mute:" + host}])
    return "\n".join(head) + "\n\n" + text, {"inline_keyboard": buttons} if buttons else None


def _uid(meta):
    found = re.search(rb"UID (\d+)", meta)
    return int(found.group(1)) if found else None


def _fetched(data):
    """UID → literal; some servers put UID after the literal, in the trailing chunk."""
    result, pending = {}, None
    for item in data:
        if isinstance(item, tuple):
            uid = _uid(item[0])
            if uid:
                result[uid], pending = item[1], None
            else:
                pending = item[1]
        elif isinstance(item, bytes) and pending is not None:
            uid = _uid(item)
            if uid:
                result[uid] = pending
            pending = None
    return result


def poll(connect, host, user, password, state, rule, tz):
    """One IMAP session: returns the new mailbox position and the letters worth sending."""
    imap = connect(host, 993, timeout=30)
    try:
        imap.login(user, password)
        gmail = "X-GM-EXT-1" in getattr(imap, "capabilities", ()) or host.endswith("gmail.com")
        typ, _ = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error("INBOX недоступен")
        validity = int((imap.response("UIDVALIDITY")[1] or [b"0"])[0] or 0)
        if state.get("validity") != validity or "uid" not in state:
            _, status = imap.status("INBOX", "(UIDNEXT)")
            uidnext = int(re.search(rb"UIDNEXT (\d+)", status[0]).group(1))
            return dict(validity=validity, uid=uidnext - 1, letters=[], skipped=0, baseline=True, gmail=gmail)
        last = state["uid"]
        _, found = imap.uid("SEARCH", "UID", f"{last + 1}:*")
        uids = sorted(u for u in map(int, (found[0] or b"").split()) if u > last)
        if not uids:
            return dict(validity=validity, uid=last, letters=[], skipped=0, baseline=False, gmail=gmail)
        recent, skipped = uids[-HEADER_LIMIT:], max(0, len(uids) - HEADER_LIMIT)
        primary = set(recent)
        if gmail:
            _, found = imap.uid("SEARCH", "UID", f"{recent[0]}:*", "X-GM-RAW", '"category:primary"')
            primary = {int(u) for u in (found[0] or b"").split()}
        _, data = imap.uid("FETCH", ",".join(map(str, recent)), "(BODY.PEEK[HEADER])")
        chosen = []
        for uid, raw in sorted(_fetched(data).items()):
            try:
                verdict = classify(message_from_bytes(raw, policy=policy.default), uid in primary, rule)
            except Exception as exc:  # One malformed letter must not stop the mailbox.
                log.warning("Письмо %s пропущено: %s", uid, type(exc).__name__)
                verdict = None
            if verdict:
                chosen.append((uid, verdict))
        letters = []
        if chosen:
            _, data = imap.uid("FETCH", ",".join(str(u) for u, _ in chosen), f"(BODY.PEEK[]<0.{BODY_BYTES}>)")
            bodies = _fetched(data)
            for uid, verdict in chosen:
                message = message_from_bytes(bodies.get(uid, b""), policy=policy.default)
                try:
                    text, markup = letter(message, verdict, tz, gmail)
                except Exception as exc:
                    log.warning("Письмо %s не разобрано: %s", uid, type(exc).__name__)
                    text, markup = f"📬 Важное письмо ({verdict['reason']}) — не удалось показать текст, откройте почту.", None
                key = str(message.get("Message-ID", "") or "").strip() or f"{validity}:{uid}"
                letters.append(dict(uid=uid, key=key, text=text, markup=markup))
        return dict(validity=validity, uid=uids[-1], letters=letters, skipped=skipped, baseline=False, gmail=gmail)
    finally:
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def masked(address):
    local, _, host = address.partition("@")
    return (local[:2] + "…" if len(local) > 2 else local) + "@" + host


class Mail:
    def __init__(self, bot, connect=imaplib.IMAP4_SSL):
        self.bot, self.connect = bot, connect
        self.next_check = None
        self.lock = asyncio.Lock()

    @property
    def configured(self):
        return bool(self.bot.config.mail_user and self.bot.config.mail_password)

    def due(self, now):
        return (self.configured and self.bot.store.get("mail_enabled", True)
                and (self.next_check is None or now >= self.next_check))

    async def check(self, now, manual=False):
        """Returns how many letters were sent; problems are reported once, never with the password."""
        bot, store, config = self.bot, self.bot.store, self.bot.config
        self.next_check = now + timedelta(minutes=store.get("mail_minutes", 3))
        async with self.lock:
            try:
                result = await asyncio.to_thread(poll, self.connect, config.mail_host, config.mail_user,
                                                 config.mail_password, store.get("mail_state") or {}, rules(store),
                                                 config.timezone)
            except (imaplib.IMAP4.abort, OSError, socket.timeout):
                return await self.problem("Почтовый сервер недоступен, повторю позже.", manual, quiet=True)
            except imaplib.IMAP4.error:
                return await self.problem("Почта отклонила вход или запрос. Проверьте MAIL_USER и пароль "
                                          "приложения в .env, затем перезапустите бота.", manual)
            except (ValueError, AttributeError, IndexError, TypeError):
                return await self.problem("Почтовый сервер ответил в неожиданном формате, повторю позже.", manual, quiet=True)
            store.set("mail_checked", now.isoformat())
            store.set("mail_error", None)
            if result["baseline"]:
                store.set("mail_state", dict(validity=result["validity"], uid=result["uid"]))
                await bot.telegram.send(config.owner_id, f"📬 Почта {masked(config.mail_user)} подключена. "
                                        "Новые важные письма буду присылать сюда; настройки — /mail.")
                return None
            sent, position = 0, result["uid"]
            for item in result["letters"]:
                key = "mail:" + item["key"]
                if store.delivered(key):
                    continue
                if sent == SEND_LIMIT:
                    rest = sum(not store.delivered("mail:" + i["key"]) for i in result["letters"])
                    await bot.telegram.send(config.owner_id, f"📬 И ещё {rest} важных писем — откройте почту.")
                    break
                try:
                    await bot.telegram.send(config.owner_id, item["text"],
                                            **({"reply_markup": item["markup"]} if item["markup"] else {}))
                except ServiceError:
                    position = item["uid"] - 1  # The next check starts from the letter that failed.
                    break
                store.mark(key, now.isoformat())
                sent += 1
            store.set("mail_state", dict(validity=result["validity"], uid=position))
            if result["skipped"] and position == result["uid"]:
                await bot.telegram.send(config.owner_id, f"📬 Пока бот не проверял почту, пришло ещё "
                                        f"{result['skipped']} писем; показаны только последние.")
            return sent

    async def problem(self, text, manual, quiet=False):
        store = self.bot.store
        log.warning("Почта: %s", text)
        repeated = store.get("mail_error") == text
        store.set("mail_error", text)
        if manual or not (repeated or quiet):
            await self.bot.telegram.send(self.bot.config.owner_id, "⚠️ " + text)
        return None

    def status(self, now):
        store, config = self.bot.store, self.bot.config
        if not self.configured:
            return ("📬 Почта не подключена.\n\n1. Включите двухэтапную проверку в аккаунте Google.\n"
                    "2. Создайте пароль приложения: myaccount.google.com/apppasswords\n"
                    "3. Допишите в .env на сервере:\nMAIL_USER=адрес@gmail.com\nMAIL_PASSWORD=пароль приложения\n"
                    "4. Перезапустите бота. Пароль хранится только на сервере.")
        checked = store.get("mail_checked")
        rule = rules(store)
        lines = [f"📬 Почта {masked(config.mail_user)} · " + ("включена" if store.get("mail_enabled", True) else "выключена"),
                 f"Проверка раз в {store.get('mail_minutes', 3)} мин" + (
                     f", последняя — {datetime.fromisoformat(checked):%d.%m %H:%M}" if checked else ""),
                 store.get("mail_error") or "Ошибок нет.",
                 "",
                 "Присылаю: письма из «Основных» без рассылок, письма от важных отправителей и по хакатонам, "
                 "а также с ключевыми словами (хакатон, дедлайн, заявка, приглашение, результат…). "
                 "Спам не читаю, письма остаются непрочитанными.",
                 "",
                 "Важные отправители: " + ", ".join(rule["vip"]),
                 "Не присылать: " + (", ".join(rule["mute"]) or "—"),
                 "",
                 "/mail check — проверить сейчас\n/mail vip example.com — всегда присылать\n"
                 "/mail mute example.com — не присылать\n/mail unvip или unmute — убрать из списка\n"
                 "/mail off · /mail on · /set mail 5 — частота в минутах"]
        return "\n".join(lines)
