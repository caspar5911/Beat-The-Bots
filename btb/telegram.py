"""Minimal Telegram Bot API client (HTML messages, inline buttons)."""
from __future__ import annotations

import datetime as dt
import html
import re
import time
from zoneinfo import ZoneInfo

import requests


def redact(text: str, token: str) -> str:
    """Error text can carry request URLs, and Bot API URLs contain the token."""
    text = str(text)
    if token:
        text = text.replace(token, "<token>")
    return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]{20,}", "bot<token>", text)


class Forbidden(Exception):
    """The bot can no longer post in a chat (removed from the group, or blocked)."""


def keyboard(rows) -> dict | None:
    if not rows:
        return None
    return {"inline_keyboard": [[{"text": a, "callback_data": b[:64]} for a, b in row] for row in rows]}


def night(now: dt.datetime | None = None) -> bool:
    """Singapore night (23:00-08:00): messages arrive silently."""
    h = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo("Asia/Singapore")).hour
    return h >= 23 or h < 8


def plain(t: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", t))


class Telegram:
    def __init__(self, token: str, chat_id: str, session=None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self.http = session or requests.Session()

    def call(self, method: str, timeout: float = 30, **params) -> dict:
        for attempt in range(2):
            r = self.http.post(f"{self.api}/{method}", json=params, timeout=timeout)
            try:
                data = r.json()
            except ValueError:  # e.g. an HTML 502 page
                data = {}
            if r.status_code == 429 and attempt == 0:
                time.sleep(min(int(data.get("parameters", {}).get("retry_after", 5)), 30))
                continue
            return data
        return {}

    def send(self, text: str, buttons=None, chat_id: str | None = None, silent: bool | None = None) -> int | None:
        """silent=None: quiet during the Singapore night only."""
        params = {"chat_id": chat_id or self.chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True, "disable_notification": night() if silent is None else silent}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        res = self.call("sendMessage", **params)
        if res.get("error_code") == 403:
            raise Forbidden(res.get("description", ""))
        if not res.get("ok"):  # markup rejected: fall back to plain text
            params.pop("parse_mode")
            params["text"] = plain(text)
            res = self.call("sendMessage", **params)
        if not res.get("ok"):
            raise RuntimeError(f"Telegram refused the message: {res.get('description', 'no answer')}"[:300])
        return (res.get("result") or {}).get("message_id")

    def send_photo(self, png: bytes, caption: str = "", chat_id: str | None = None) -> int | None:
        r = self.http.post(f"{self.api}/sendPhoto", timeout=60,
                           data={"chat_id": chat_id or self.chat_id, "caption": caption[:1000]},
                           files={"photo": ("card.png", png, "image/png")})
        try:
            res = r.json()
        except ValueError:
            return None
        if res.get("error_code") == 403:
            raise Forbidden(res.get("description", ""))
        return (res.get("result") or {}).get("message_id")

    def leave(self, chat_id) -> None:
        self.call("leaveChat", chat_id=chat_id)

    def edit(self, message_id: int, text: str, buttons=None, chat_id: str | None = None) -> None:
        params = {"chat_id": chat_id or self.chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        self.call("editMessageText", **params)

    def answer(self, callback_id: str, text: str) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])

    def updates(self, offset: int, timeout: int = 25) -> list[dict]:
        r = self.http.get(f"{self.api}/getUpdates", timeout=timeout + 15,
                          params={"timeout": timeout, "offset": offset,
                                  "allowed_updates": '["message","callback_query","my_chat_member"]'})
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
