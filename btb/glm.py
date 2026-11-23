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
Give an HONEST confidence (0.5 to 1.0) that your call is right; your record is shown to the players, so
overconfidence will be exposed. Open-to-close direction is close to a coin flip: most calls deserve 0.5-0.6,
and 0.7+ only with a clear, specific reason.
Keep the reason to one short, punchy sentence (light trash talk at the humans is welcome, nothing rude).
Write in simple everyday English: short words, short sentences, no finance jargon (no 'gap', 'fade', 'priced in', 'momentum', 'catalyst').
Answer with ONE JSON object and nothing else: {"call":"UP|DOWN","confidence":0.55,"reason":"..."}"""


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
Playful only: no profanity, nothing about anyone's identity, looks, money or life. Plain text, no JSON, no hashtags.
Write in simple everyday English: short words, short sentences, no finance jargon (no 'gap', 'fade', 'priced in', 'momentum', 'catalyst')."""


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


MOVER = """You are "{bot}", a cocky AI in a daily stock game. Below are today's stocks with the previous close,
pre-market price and headlines. Bonus question: which ONE will have the LARGEST absolute move (up or down) from
today's OPEN to today's CLOSE? Big news, earnings and volatile names move more; the opening gap itself does not count.
Use only the data given. Write in simple everyday English: short words, short sentences, no finance jargon (no 'gap', 'fade', 'priced in', 'momentum', 'catalyst').
Answer with ONE JSON object and nothing else:
{"ticker":"XXX","reason":"one short punchy sentence"}"""


def call_mover(s: Settings, stocks: list[dict], session=None, timeout: float = 120) -> tuple[str, str] | None:
    lines = []
    for st in stocks:
        lines.append(prompt(st["ticker"], st["name"], st, st.get("headlines") or []))
    body = {"model": s.glm_model, "reasoning_effort": "low", "max_tokens": 1200, "temperature": 0.3,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": MOVER.replace("{bot}", _name(s.bot_name))},
                         {"role": "user", "content": "\n\n".join(lines)}]}
    try:
        r = (session or requests).post(f"{s.glm_base_url.rstrip('/')}/chat/completions", json=body,
                                       headers=_headers(s), timeout=timeout)
        r.raise_for_status()
        content = (r.json()["choices"][0]["message"].get("content") or "").strip()
        data = json.loads(content.removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return None
    ticker = str(data.get("ticker", "")).upper() if isinstance(data, dict) else ""
    if ticker not in {st["ticker"] for st in stocks}:
        return None
    return ticker, str(data.get("reason", "")).strip()[:200]


LESSON = """You write ONE short daily lesson for a group game where friends call whether stocks close above or below
their open. You get today's stocks (pre-market move, open-to-close move, the group's split) as JSON.
Teach a decision-making habit (base rates, priced-in news, crowd vs individual, calibration, process vs outcome,
noise vs signal) using today's data. Max 2 sentences, no advice on what to buy or sell.
Write in simple everyday English: short words, short sentences, no finance jargon (no 'gap', 'fade', 'priced in', 'momentum', 'catalyst'). Plain text only."""

WHY = """For each stock below you get its open-to-close move today and today's headlines. In at most 170 characters
per stock, explain the most likely driver of the move using only these headlines. If none explains it, say it was
probably the whole market moving, or random. No predictions, no advice. Write in simple everyday English: short words, short sentences, no finance jargon (no 'gap', 'fade', 'priced in', 'momentum', 'catalyst').
Answer with ONE JSON object mapping ticker to text."""


def _text(s: Settings, system: str, user: str, session=None, timeout: float = 120, json_mode: bool = False):
    body = {"model": s.glm_model, "reasoning_effort": "low", "max_tokens": 1500, "temperature": 0.4,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = (session or requests).post(f"{s.glm_base_url.rstrip('/')}/chat/completions", json=body,
                                       headers=_headers(s), timeout=timeout)
        r.raise_for_status()
        return (r.json()["choices"][0]["message"].get("content") or "").strip()
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return None


def lesson(s: Settings, summary: dict, session=None) -> str | None:
    text = _text(s, LESSON, json.dumps(summary, default=str), session)
    return text.strip('"')[:320] if text else None


def why(s: Settings, stocks: list[dict], session=None) -> dict[str, str]:
    """{ticker: short explanation of today's move}; empty when the model is unavailable."""
    lines = []
    for st in stocks:
        lines.append(f"{st['ticker']} ({st['name']}): {st['move']:+.1%} open to close")
        lines += [f"- {h}" for h in st.get("headlines", [])[:6]] or ["- (no headlines)"]
    text = _text(s, WHY, "\n".join(lines), session, json_mode=True)
    try:
        data = json.loads((text or "").removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): str(v)[:180] for k, v in data.items() if isinstance(v, str)}
