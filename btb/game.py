"""The game: a daily round of stocks; players and the Bot call each close as above (UP) or below (DOWN) its open.

Timeline on each NYSE trading day (New York time):
  08:45  round posted: stocks, pre-market move, headlines, vote buttons. The Bot's calls are made but hidden.
  09:30  picks lock (votes are judged by the server time they arrive); the Bot's calls are revealed.
  09:35+ opening prices captured from quote snapshots.
  close + 10 min  results and leaderboard posted.
Every stage is idempotent and driven by tick(), so a restart (or a missed stage) catches up."""
from __future__ import annotations

import datetime as dt
import html
import random

from . import market
from .config import NY, SG
from .store import BOT_ID, Store

POOL = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO", "PLTR", "COIN",
        "INTC", "DIS", "NKE", "SBUX", "BA", "JPM", "UBER", "SHOP", "MU", "ORCL", "CRM", "PYPL", "ADBE",
        "COST", "WMT", "KO", "MCD", "XOM", "LLY", "UNH", "V", "GS", "ABNB", "SNOW", "ARM", "SMCI", "RIVN", "HOOD"]

POST_AT = dt.time(8, 45)
LAST_POST = dt.time(9, 20)       # too late to give people time to play: skip the day
AI_DEADLINE = dt.time(9, 15)     # stop asking the model; later stocks are played without the Bot
OPEN_CAPTURE = dt.time(9, 35)
SETTLE_DELAY = dt.timedelta(minutes=10)
GIVE_UP = dt.timedelta(days=4)   # unresolved stocks are voided after this

ARROW = {"UP": "⬆️", "DOWN": "⬇️"}
DISCLAIMER = "<i>Just a game: not investment advice, no money involved.</i>"


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def at(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, tzinfo=NY)


def lock_at(day: dt.date) -> dt.datetime:
    return at(day, market.OPEN)


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def pick_tickers(day: dt.date, n: int, pool=POOL) -> list[str]:
    return random.Random(day.isoformat()).sample(list(pool), n)


def _move(s: dict) -> str:
    if s.get("pre_price") and s.get("prev_close"):
        return f"pre-market {s['pre_price'] / s['prev_close'] - 1:+.1%}"
    return "no pre-market price"


def _sg(t: dt.datetime) -> str:
    return t.astimezone(SG).strftime("%H:%M")


def round_text(day: dt.date, stocks: list[dict], voters: int = 0) -> str:
    lk = lock_at(day)
    lines = [f"🎯 <b>Beat the Bot</b> · {day:%a %d %b}",
             "Will each stock <b>close above ⬆️ or below ⬇️ its opening price</b> today?",
             f"Picks lock at the open: 09:30 New York ({_sg(lk)} Singapore). "
             "The Bot has already made its calls; they are revealed at the lock.", ""]
    for i, s in enumerate(stocks, 1):
        lines.append(f"<b>{i}. {esc(s['ticker'])}</b> {esc(s['name'])} · {_move(s)}")
        for h in s["headlines"][:2]:
            lines.append(f"   • {esc(h['title'][:110])}")
        if not s["headlines"]:
            lines.append("   • (no fresh headlines)")
    lines += ["", f"Players so far: {voters}", DISCLAIMER]
    return "\n".join(lines)


def buttons(day: dt.date, stocks: list[dict]) -> list:
    key = day.strftime("%Y%m%d")
    return [[(f"⬆️ {s['ticker']}", f"v|{key}|{s['ticker']}|U"), (f"⬇️ {s['ticker']}", f"v|{key}|{s['ticker']}|D")]
            for s in stocks]


def lock_text(day: dt.date, stocks: list[dict], voters: int) -> str:
    lines = [f"🔒 <b>Picks locked</b> · {day:%a %d %b} · {voters} player{'s' if voters != 1 else ''}",
             "The Bot's calls:"]
    for s in stocks:
        if s["ai_call"]:
            lines.append(f"{ARROW[s['ai_call']]} <b>{esc(s['ticker'])}</b>: {esc(s['ai_reason'] or '(no comment)')}")
        else:
            lines.append(f"💤 <b>{esc(s['ticker'])}</b>: the Bot sat this one out (model unavailable)")
    close = at(day, market.close_time(day))
    lines.append(f"Results after the close ({_sg(close + SETTLE_DELAY)} Singapore).")
    return "\n".join(lines)


def board_lines(rows: list[dict], limit: int = 10) -> list[str]:
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows[:limit]):
        pct = f"{r['points'] / r['played']:.0%}" if r["played"] else "-"
        out.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(r['name'])}: {r['points']}/{r['played']} ({pct})")
    return out or ["(no settled picks yet)"]


def results_text(store: Store, day: dt.date) -> str:
    key = day.isoformat()
    stocks, votes = store.stocks(key), store.votes(key)
    lines = [f"🏁 <b>Results</b> · {day:%a %d %b}"]
    for s in stocks:
        if s["result"] in ("UP", "DOWN"):
            right = sum(1 for v in votes if v["ticker"] == s["ticker"] and v["call"] == s["result"] and v["user_id"] != BOT_ID)
            humans = sum(1 for v in votes if v["ticker"] == s["ticker"] and v["user_id"] != BOT_ID)
            bot = "" if not s["ai_call"] else (" · Bot ✅" if s["ai_call"] == s["result"] else " · Bot ❌")
            lines.append(f"{ARROW[s['result']]} <b>{esc(s['ticker'])}</b> {s['open']:.2f} → {s['close']:.2f} "
                         f"({s['close'] / s['open'] - 1:+.1%}) · {right}/{humans} humans right{bot}")
        else:
            lines.append(f"➖ <b>{esc(s['ticker'])}</b>: no result ({'closed flat' if s['result'] == 'FLAT' else 'price unavailable'}), not scored")
    today = store.scores(key, key)
    if today:
        bot = next((r for r in today if r["user_id"] == BOT_ID), None)
        humans = [r for r in today if r["user_id"] != BOT_ID]
        lines += ["", "<b>Today</b>", *board_lines(today)]
        if bot and humans:
            beat = [esc(r["name"]) for r in humans if r["points"] > bot["points"]]
            lines.append("🏆 Beat the Bot today: " + ", ".join(beat) if beat else "🤖 Nobody beat the Bot today.")
    lines += ["", "<b>This week</b>", *board_lines(store.scores(week_start(day).isoformat(), key))]
    return "\n".join(lines)


HELP = ("🎯 <b>Beat the Bot</b>\nEvery US trading day a round of stocks is posted before the open (21:45 Singapore "
        "in winter, 20:45 in summer). Tap ⬆️ if you think a stock closes above its opening price, ⬇️ if below. "
        "You can change your mind until the 09:30 New York open. The Bot (our own AI) plays too.\n\n"
        "/today: today's round · /board: leaderboard · /me: your record\n" + DISCLAIMER)


class Game:
    def __init__(self, settings, store: Store, tg, quotes, news, call_ai, ai_ready, log=print, clock=None):
        self.s, self.store, self.tg = settings, store, tg
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.quotes, self.news, self.call_ai, self.ai_ready, self.log = quotes, news, call_ai, ai_ready, log

    # ---------------------------------------------------------------- stages
    def tick(self, now: dt.datetime) -> None:
        ny = now.astimezone(NY)
        day = ny.date()
        for r in self.store.db.execute("SELECT day, status FROM rounds WHERE status IN ('open','locked') ORDER BY day"):
            d = dt.date.fromisoformat(r["day"])
            if r["status"] == "open" and ny >= lock_at(d):
                self.lock(d)
            if ny >= lock_at(d):
                self.capture_open(d, ny)
                self.settle(d, ny)
        if (market.is_trading_day(day) and self.store.round(day.isoformat()) is None
                and POST_AT <= ny.time() < LAST_POST):
            self.create(day, now)

    def create(self, day: dt.date, now: dt.datetime) -> None:
        tickers = pick_tickers(day, self.s.stocks_per_day)
        try:
            q = self.quotes(tickers)
        except Exception as e:  # quotes are context only at this stage
            self.log(f"pre-market quotes failed: {e!r}"[:300])
            q = {}
        ready = self.ai_ready()
        if not ready:
            self.log("model unavailable: the Bot sits today out")
        stocks = []
        for t in tickers:
            info = q.get(t, {})
            s = {"ticker": t, "name": info.get("name") or t, "prev_close": info.get("prev_close"),
                 "pre_price": info.get("pre_price"), "ai_call": None, "ai_reason": ""}
            s["headlines"] = [{"title": h["title"], "published": h["published"]} for h in self.news(t, s["name"])]
            if ready and self.clock() < at(day, AI_DEADLINE):
                got = self.call_ai(t, s["name"], info, s["headlines"])
                if got:
                    s["ai_call"], s["ai_reason"] = got
            stocks.append(s)
        for s in stocks:  # store JSON-safe headlines
            s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                              for h in s["headlines"]]
        key = day.isoformat()
        self.store.add_round(key, stocks, now.isoformat())
        mid = self.tg.send(round_text(day, self.store.stocks(key)), buttons(day, stocks))
        if mid:
            self.store.set_message(key, mid)

    def voters(self, key: str) -> int:
        return len({v["user_id"] for v in self.store.votes(key) if v["user_id"] != BOT_ID})

    def lock(self, day: dt.date) -> None:
        key = day.isoformat()
        stocks = self.store.stocks(key)
        for s in stocks:
            if s["ai_call"]:
                self.store.vote(key, s["ticker"], BOT_ID, s["ai_call"], lock_at(day).isoformat())
        self.store.set_status(key, "locked")
        r = self.store.round(key)
        if r["message_id"]:
            self.tg.edit(r["message_id"], round_text(day, stocks, self.voters(key)) + "\n🔒 <b>Locked.</b>", None)
        self.tg.send(lock_text(day, stocks, self.voters(key)), None)

    def capture_open(self, day: dt.date, ny: dt.datetime) -> None:
        key = day.isoformat()
        missing = [s["ticker"] for s in self.store.stocks(key) if s["open"] is None and s["result"] is None]
        if not missing or ny < at(day, OPEN_CAPTURE):
            return
        try:
            q = self.quotes(missing)
        except Exception as e:
            self.log(f"open capture failed: {e!r}"[:300])
            return
        for t in missing:
            o, _ = market.session_prices(day, q.get(t))
            if o:
                self.store.update_stock(key, t, open=o)

    def settle(self, day: dt.date, ny: dt.datetime) -> None:
        key = day.isoformat()
        if ny < at(day, market.close_time(day)) + SETTLE_DELAY:
            return
        pending = [s for s in self.store.stocks(key) if s["result"] is None]
        if pending:
            try:
                q = self.quotes([s["ticker"] for s in pending])
            except Exception as e:
                self.log(f"settle quotes failed: {e!r}"[:300])
                q = None
            give_up = ny >= at(day, market.close_time(day)) + GIVE_UP
            for s in pending:
                info = (q or {}).get(s["ticker"])
                o, c = market.session_prices(day, info)
                o = s["open"] or o
                if o and c:
                    result = "UP" if c > o else "DOWN" if c < o else "FLAT"
                    self.store.update_stock(key, s["ticker"], open=o, close=c, result=result)
                elif give_up or c:  # c without o: close only known from the next session, the open was never captured
                    self.store.update_stock(key, s["ticker"], result="VOID")
                elif info and info["update_time"] and info["update_time"].date() > market.next_trading_day(day):
                    self.store.update_stock(key, s["ticker"], result="VOID")
        if all(s["result"] is not None for s in self.store.stocks(key)):
            self.store.set_status(key, "settled")
            self.tg.send(results_text(self.store, day), None)

    # ---------------------------------------------------------------- players
    def on_vote(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        try:
            _, d, ticker, c = data.split("|")
            day = dt.datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return "Unknown button."
        key = day.isoformat()
        r = self.store.round(key)
        if r is None or ticker not in {s["ticker"] for s in self.store.stocks(key)} or c not in ("U", "D"):
            return "That round no longer exists."
        if r["status"] != "open" or now >= lock_at(day):
            return "Too late: picks are locked."
        call = "UP" if c == "U" else "DOWN"
        self.store.player(user_id, name)
        self.store.vote(key, ticker, user_id, call, now.isoformat())
        return f"{ticker} {ARROW[call]} saved. You can change it until the open."

    def on_command(self, text: str, user_id: int, name: str, now: dt.datetime) -> tuple[str, list | None] | None:
        cmd = text.split()[0].split("@")[0].lower() if text.strip() else ""
        ny = now.astimezone(NY)
        if cmd in ("/start", "/help"):
            return HELP, None
        if cmd == "/today":
            row = self.store.db.execute("SELECT day FROM rounds ORDER BY day DESC LIMIT 1").fetchone()
            if not row:
                return "No round yet. The first one is posted before the next US open.", None
            day = dt.date.fromisoformat(row["day"])
            r = self.store.round(row["day"])
            stocks = self.store.stocks(row["day"])
            if r["status"] == "open":
                return round_text(day, stocks, self.voters(row["day"])), buttons(day, stocks)
            if r["status"] == "locked":
                return lock_text(day, stocks, self.voters(row["day"])), None
            return results_text(self.store, day), None
        if cmd == "/board":
            today = ny.date().isoformat()
            return ("<b>This week</b>\n" + "\n".join(board_lines(self.store.scores(week_start(ny.date()).isoformat(), today)))
                    + "\n\n<b>All time</b>\n" + "\n".join(board_lines(self.store.scores()))), None
        if cmd == "/me":
            mine = next((r for r in self.store.scores() if r["user_id"] == user_id), None)
            bot = next((r for r in self.store.scores() if r["user_id"] == BOT_ID), None)
            if not mine:
                return f"{esc(name)}: no settled picks yet. Tap the buttons on today's round!", None
            vs = f"\nThe Bot: {bot['points']}/{bot['played']}" if bot else ""
            return f"{esc(name)}: {mine['points']}/{mine['played']} correct ({mine['points'] / mine['played']:.0%}){vs}", None
        return None
