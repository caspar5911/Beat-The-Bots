"""NYSE calendar and Moomoo quote snapshots (quotes only: no history/K-line quota, no trade context)."""
from __future__ import annotations

import datetime as dt

from .config import NY, Settings

# NYSE full-day closures. Dates after 2027 are unknown here: those days still play, and any stock whose
# snapshot is not from that day is voided at settle.
HOLIDAYS = {dt.date(*d) for d in [
    (2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3), (2026, 5, 25), (2026, 6, 19), (2026, 7, 3),
    (2026, 9, 7), (2026, 11, 26), (2026, 12, 25),
    (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26), (2027, 5, 31), (2027, 6, 18), (2027, 7, 5),
    (2027, 9, 6), (2027, 11, 25), (2027, 12, 24)]}
EARLY_CLOSE = {dt.date(2026, 11, 27), dt.date(2026, 12, 24), dt.date(2027, 11, 26)}  # 13:00 NY

OPEN = dt.time(9, 30)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def close_time(d: dt.date) -> dt.time:
    return dt.time(13, 0) if d in EARLY_CLOSE else dt.time(16, 0)


def next_trading_day(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def _num(x) -> float | None:
    try:
        v = float(x)
        return v if v == v and v > 0 else None
    except (TypeError, ValueError):
        return None


def _time(x) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(x)).replace(tzinfo=NY)
    except ValueError:
        return None


class Quotes:
    """quotes(tickers) -> {ticker: {name, open, last, prev_close, pre_price, update_time}} via OpenD snapshots."""

    def __init__(self, s: Settings, ctx=None):
        self.s, self._ctx = s, ctx

    def __call__(self, tickers: list[str]) -> dict:
        ctx = self._ctx
        if ctx is None:
            from moomoo import OpenQuoteContext
            ctx = OpenQuoteContext(host=self.s.opend_host, port=self.s.opend_port, is_async_connect=True)
            ctx.set_sync_query_connect_timeout(5)
        try:
            ret, df = ctx.get_market_snapshot([f"US.{t}" for t in tickers])
            if ret != 0:
                raise RuntimeError(f"snapshot failed: {str(df)[:200]}")
            return parse_snapshot(df.to_dict("records"))
        finally:
            if self._ctx is None:
                ctx.close()


def parse_snapshot(rows: list[dict]) -> dict:
    out = {}
    for r in rows:
        t = str(r.get("code", "")).replace("US.", "")
        out[t] = {"name": str(r.get("name") or t), "open": _num(r.get("open_price")),
                  "last": _num(r.get("last_price")), "prev_close": _num(r.get("prev_close_price")),
                  "pre_price": _num(r.get("pre_price")), "update_time": _time(r.get("update_time"))}
    return out


def session_prices(day: dt.date, q: dict | None) -> tuple[float | None, float | None]:
    """(open, close) of `day` from a snapshot, or None for what the snapshot cannot prove.

    During and right after the session, update_time is on `day`: open_price is that day's open and, once
    update_time has reached the close, last_price is the regular-session close (after-hours is a separate
    field). Once the next session trades, prev_close is `day`'s close but `day`'s open is gone."""
    if not q or not q.get("update_time"):
        return None, None
    ut = q["update_time"]
    if ut.date() == day:
        closed = ut.time() >= close_time(day)
        return q.get("open"), (q.get("last") if closed else None)
    if ut.date() == next_trading_day(day):
        return None, q.get("prev_close")
    return None, None
