"""Shareable scorecard images (PNG): a player's week, and a group's weekly leaderboard.

Drawn with Pillow and the DejaVu fonts (no colour emoji in images: names are reduced to text)."""
from __future__ import annotations

import datetime as dt
import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .store import BOT_ID

FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu"),
             Path("/Library/Fonts"), Path("C:/Windows/Fonts")]

BG, PANEL, INK, MUTED = (17, 24, 39), (31, 41, 55), (243, 244, 246), (156, 163, 175)
GOLD, GREEN, RED, ACCENT = (250, 204, 21), (52, 211, 153), (248, 113, 113), (96, 165, 250)


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in FONT_DIRS:
        if (d / name).exists():
            return ImageFont.truetype(str(d / name), size)
    return ImageFont.load_default(size)


def clean(name: str) -> str:
    """Letters, digits and simple punctuation only (fonts here have no colour emoji)."""
    text = re.sub(r"[^\w\s.'&?!-]", "", name, flags=re.UNICODE).strip()
    return text[:24] or "Player"


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _week(since: str, until: str) -> str:
    a, b = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    return f"Week of {a:%d %b %Y}" + (f" (to {b:%a %d %b})" if b != a else "")


def _center(d: ImageDraw.ImageDraw, y: int, text: str, font, fill, width: int) -> None:
    w = d.textlength(text, font=font)
    d.text(((width - w) / 2, y), text, font=font, fill=fill)


def personal_card(title: str, bot: str, name: str, since: str, until: str, me: dict, ai: dict | None,
                  streak: int) -> bytes:
    W, H = 1080, 1080
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _center(d, 70, clean(title).upper(), _font(44, True), ACCENT, W)
    _center(d, 130, _week(since, until), _font(30), MUTED, W)
    _center(d, 230, clean(name), _font(84, True), INK, W)
    won = not ai or me["points"] > ai["points"]
    tied = ai and me["points"] == ai["points"]
    verdict = (f"BEAT {clean(bot).upper()}" if won else f"TIED WITH {clean(bot).upper()}" if tied
               else f"LOST TO {clean(bot).upper()}")
    _center(d, 350, verdict, _font(64, True), GREEN if won else GOLD if tied else RED, W)
    d.rounded_rectangle((90, 470, W - 90, 790), radius=32, fill=PANEL)
    cols = [("POINTS", f"{me['points']}"), ("CORRECT", f"{me['correct']}/{me['played']}"), ("STREAK", f"{streak}")]
    for i, (label, value) in enumerate(cols):
        cx = 90 + (W - 180) * (2 * i + 1) / 6
        v = _font(96, True)
        d.text((cx - d.textlength(value, font=v) / 2, 530), value, font=v, fill=INK)
        lf = _font(28, True)
        d.text((cx - d.textlength(label, font=lf) / 2, 680), label, font=lf, fill=MUTED)
    if ai:
        _center(d, 850, f"{clean(bot)}: {ai['points']} pts ({ai['correct']}/{ai['played']})", _font(36), MUTED, W)
    _center(d, 960, "Can you beat the AI?  A daily stock game on Telegram", _font(30), ACCENT, W)
    return _png(img)


def group_card(title: str, bot: str, since: str, until: str, rows: list[dict], limit: int = 8) -> bytes:
    W = 1080
    shown = rows[:limit]
    H = 330 + 110 * max(len(shown), 1) + 90
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _center(d, 60, clean(title).upper(), _font(48, True), ACCENT, W)
    _center(d, 125, _week(since, until), _font(30), MUTED, W)
    ai = next((r for r in rows if r["user_id"] == BOT_ID), None)
    humans = [r for r in rows if r["user_id"] != BOT_ID]
    slayers = [r for r in humans if ai and r["points"] > ai["points"]]
    head = (f"{len(slayers)} beat {clean(bot)} this week" if slayers else f"{clean(bot)} beat everyone this week"
            if ai else "Humans only this week")
    _center(d, 190, head, _font(40, True), GREEN if slayers else RED, W)
    y = 290
    for i, r in enumerate(shown, 1):
        is_ai = r["user_id"] == BOT_ID
        d.rounded_rectangle((80, y, W - 80, y + 90), radius=22, fill=PANEL)
        colour = GOLD if i == 1 else INK
        d.text((115, y + 22), f"{i}", font=_font(40, True), fill=colour)
        d.text((190, y + 22), clean(r["name"]) + ("  (AI)" if is_ai else ""), font=_font(40, True),
               fill=ACCENT if is_ai else INK)
        score = f"{r['points']} pts  {r['correct']}/{r['played']}"
        f = _font(36)
        d.text((W - 115 - d.textlength(score, font=f), y + 26), score, font=f, fill=MUTED)
        y += 110
    _center(d, H - 70, "A daily stock game against an AI, on Telegram", _font(28), MUTED, W)
    return _png(img)
