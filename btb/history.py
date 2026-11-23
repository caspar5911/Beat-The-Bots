"""Daily prices for the 'More' details (free Yahoo Finance chart data; never Moomoo, whose history calls count
against the trading bot's quota). Cached for a few hours per ticker."""
from __future__ import annotations

import datetime as dt
import time

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) BeatTheBot/0.1"
_CACHE: dict[str, tuple[float, list[dict]]] = {}
TTL = 3 * 3600


def daily(ticker: str, session=None) -> list[dict]:
    """[{date, open, close}] for about the last two months, oldest first; empty if the source is unavailable."""
    hit = _CACHE.get(ticker)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=2mo&interval=1d"
    try:
        r = (session or requests).get(url, headers={"User-Agent": UA}, timeout=15)
        res = r.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        rows = [{"date": dt.datetime.fromtimestamp(ts, dt.timezone.utc).date(), "open": o, "close": c}
                for ts, o, c in zip(res["timestamp"], q["open"], q["close"]) if o and c]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return []
    _CACHE[ticker] = (time.time(), rows)
    return rows
