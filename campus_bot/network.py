import asyncio
import json
import os
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ServiceError(Exception):
    """Intentionally omits request URLs: Telegram URLs contain the bot secret."""

    def __init__(self, service, status=0, retry_after=0):
        self.status, self.retry_after = status, retry_after
        super().__init__(f"{service}: HTTP {status}" if status else f"{service}: сеть недоступна")


def request_json(url, payload=None, timeout=15, service="Источник"):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = Request(url, data=body, headers={"User-Agent": os.getenv(
        "HTTP_USER_AGENT", "CampusDayBot/1.0 (https://github.com/MrFerrumL/Days_calendar)"),
                                              "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
            if isinstance(result, dict) and response.headers.get("Expires"):
                result["_http_expires"] = response.headers["Expires"]
            return result
    except HTTPError as exc:
        retry_after = 0
        try:
            retry_after = int(json.load(exc).get("parameters", {}).get("retry_after", 0))
        except (ValueError, TypeError):
            pass
        raise ServiceError(service, exc.code, retry_after) from None
    except (URLError, OSError, socket.timeout, ValueError):
        raise ServiceError(service) from None


async def get_json(url, service="Источник"):
    return await asyncio.to_thread(request_json, url, service=service)


class Telegram:
    def __init__(self, token):
        self._base = "https://api.telegram.org/bot" + token + "/"
        self._send_lock = asyncio.Lock()
        self._next_send = {}

    async def call(self, method, **payload):
        response = await asyncio.to_thread(request_json, self._base + method, payload, 45, "Telegram")
        if not response.get("ok"):
            raise ServiceError("Telegram", response.get("error_code", 0),
                               response.get("parameters", {}).get("retry_after", 0))
        return response["result"]

    async def send(self, chat_id, text, **kwargs):
        # Plain text avoids HTML/Markdown injection from titles and timetable data.
        while text:
            units, end = 0, 0
            for char in text:
                width = 2 if ord(char) > 0xFFFF else 1
                if units + width > 3500:
                    break
                units += width
                end += 1
            chunk = text[:end]
            if len(text) > end:
                split = chunk.rfind("\n")
                if split > 0:
                    chunk = chunk[:split + 1]
            async with self._send_lock:
                loop = asyncio.get_running_loop()
                delay = self._next_send.get(chat_id, 0) - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    await self.call("sendMessage", chat_id=chat_id, text=chunk,
                                    link_preview_options={"is_disabled": True}, **kwargs)
                finally:
                    self._next_send[chat_id] = loop.time() + 1.05
            text = text[len(chunk):]
