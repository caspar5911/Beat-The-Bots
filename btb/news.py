"""Free headlines: Yahoo Finance RSS + Google News RSS, deduplicated, newest first.
Adapted from AI-Trading-Lab's ais/news.py."""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) BeatTheBot/0.1"


def _feeds(ticker: str, name: str = "") -> list[str]:
    word = _keyword(name) if name else ""
    q = urllib.parse.quote(f'"{word}" OR {ticker} stock' if word else f"{ticker} stock")
    return [f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US",
            f"https://news.google.com/rss/search?q={q}+when:2d&hl=en-US&gl=US&ceid=US:en"]


def parse_rss(xml_text: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc) if pub else None
        except (TypeError, ValueError):
            ts = None
        if title:
            out.append({"title": title, "published": ts, "source": (it.findtext("source") or "").strip()})
    return out


def _norm(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]+$", "", t)  # Google News appends " - Publisher"
    return re.sub(r"[^a-z0-9 ]", "", t.lower())[:80]


def dedupe_recent(items: list[dict], now: dt.datetime, hours: int = 36, limit: int = 8) -> list[dict]:
    cutoff = now - dt.timedelta(hours=hours)
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["published"] or now, reverse=True):
        if it["published"] and it["published"] < cutoff:
            continue
        k = _norm(it["title"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= limit:
            break
    return out


GENERIC = {"the", "inc", "corp", "group", "holdings", "company", "and", "co"}


def _keyword(name: str) -> str:
    words = [w for w in re.split(r"[\s\-.,]+", name) if w and w.lower() not in GENERIC]
    if not words:
        return ""
    return words[0] if len(words[0]) >= 4 or len(words) == 1 else " ".join(words[:2])


def about(title: str, ticker: str, name: str = "") -> bool:
    """The headline names the stock: its ticker (exact case) or the main word(s) of its company name."""
    if re.search(rf"(?<![A-Za-z]){re.escape(ticker)}(?![A-Za-z])", title):
        return True
    kw = _keyword(name)
    return bool(kw) and re.search(rf"\b{re.escape(kw)}\b", title, re.I) is not None


def headlines(ticker: str, name: str = "", now: dt.datetime | None = None, session=None) -> list[dict]:
    now = now or dt.datetime.now(dt.timezone.utc)
    s = session or requests
    items = []
    for url in _feeds(ticker, name):
        try:
            r = s.get(url, headers={"User-Agent": UA}, timeout=15)
            if r.status_code == 200:
                items += parse_rss(r.text)
        except requests.RequestException:
            continue
    return dedupe_recent([i for i in items if about(i["title"], ticker, name)], now)
