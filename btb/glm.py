"""The bot's opponent: one short GLM call per stock. Adapted from AI-Trading-Lab's ais/glm.py."""
from __future__ import annotations

import json
import time

import requests

from .config import Settings

SYSTEM = """You are "{bot}", a cocky but honest AI playing a daily stock game against humans.
For ONE US stock you get the previous close, the pre-market price and recent headlines.
Predict whether TODAY's regular-session CLOSE will be ABOVE (UP) or BELOW (DOWN) today's OPEN price.
Note: the opening gap is already priced in at the open; you are calling the move from open to close.
Be balanced: roughly half of open-to-close moves are DOWN. Gaps fade about as often as they run, so call
DOWN whenever the evidence leans that way; never default to UP. Use only the data given; never invent news.
Keep the reason to one short, punchy sentence (light trash talk at the humans is welcome, nothing rude).
Answer with ONE JSON object and nothing else: {"call":"UP|DOWN","confidence":0.5,"reason":"..."}"""


def healthy(s: Settings, session=None) -> bool:
    if not s.glm_base_url:
        return False
    try:
        r = (session or requests).get(f"{s.glm_base_url.rstrip('/')}/models", timeout=10, headers=_headers(s))
        return r.status_code == 200
    except requests.RequestException:
        return False


def _headers(s: Settings) -> dict:
    h = {"Content-Type": "application/json"}
    if s.glm_api_key:
        h["Authorization"] = f"Bearer {s.glm_api_key}"
    return h


def _name(bot: str) -> str:
    return "".join(ch for ch in bot if ch.isalnum() or ch in " '-").strip() or "the Bot"


def parse_call(content: str) -> tuple[str, str, float] | None:
    content = (content or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    call = str(data.get("call", "")).upper()
    if call not in ("UP", "DOWN"):
        return None
    try:
        conf = min(max(float(data.get("confidence", 0.5)), 0.0), 1.0)
        conf = conf if conf == conf else 0.5
    except (TypeError, ValueError):
        conf = 0.5
    return call, str(data.get("reason", "")).strip()[:200], conf


def prompt(ticker: str, name: str, quote: dict, news: list[dict]) -> str:
    lines = [f"STOCK: {ticker} ({name})",
             f"PREVIOUS CLOSE: {quote.get('prev_close')}", f"PRE-MARKET PRICE: {quote.get('pre_price') or 'n/a'}",
             "HEADLINES (newest first):"]
    for n in news or []:
        when = n["published"].strftime("%Y-%m-%d %H:%M UTC") if n.get("published") else "time unknown"
        lines.append(f"- [{when}] {n['title']}")
    if not news:
        lines.append("- (no headlines found)")
    return "\n".join(lines)


def call_stock(s: Settings, ticker: str, name: str, quote: dict, news: list[dict], session=None,
               timeout: float = 120, retries: int = 1, sleep=time.sleep) -> tuple[str, str, float] | None:
    """(UP|DOWN, reason, confidence), or None when the model is unavailable or answers badly (the AI sits it out)."""
    body = {"model": s.glm_model, "reasoning_effort": "low", "max_tokens": 1200, "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM.replace("{bot}", _name(s.bot_name))},
                         {"role": "user", "content": prompt(ticker, name, quote, news)}]}
    for attempt in range(retries + 1):
        try:
            r = (session or requests).post(f"{s.glm_base_url.rstrip('/')}/chat/completions", json=body,
                                           headers=_headers(s), timeout=timeout)
            r.raise_for_status()
            got = parse_call(r.json()["choices"][0]["message"].get("content") or "")
            if got:
                return got
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
            pass
        if attempt < retries:
            sleep(5)
    return None


BANTER = """You are "{bot}", a cocky AI that just finished a daily stock-calling game against a group of friends.
You get today's results as JSON: each stock's result, and each player's points, correct calls and picks played
(your own row has user_id equal to "you"). Write the results-time banter: at most 2 short sentences,
naming 1 to 3 players. Tease bad PICKS, give grudging respect to anyone who beat you, gloat if you won.
Playful only: no profanity, nothing about anyone's identity, looks, money or life. Plain text, no JSON, no hashtags."""


def banter(s: Settings, summary: dict, session=None, timeout: float = 120) -> str | None:
    body = {"model": s.glm_model, "reasoning_effort": "low", "max_tokens": 1500, "temperature": 0.8,
            "messages": [{"role": "system", "content": BANTER.replace("{bot}", _name(s.bot_name))},
                         {"role": "user", "content": json.dumps(summary, default=str)}]}
    try:
        r = (session or requests).post(f"{s.glm_base_url.rstrip('/')}/chat/completions", json=body,
                                       headers=_headers(s), timeout=timeout)
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"].get("content") or "").strip().strip('"')
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return None
    return text[:300] or None
