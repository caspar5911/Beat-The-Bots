"""The game: a daily round of stocks; players and the AI call each close as above (UP) or below (DOWN) its open.

Timeline on each NYSE trading day (New York time; Singapore is +13h in winter, +12h in summer):
  08:45  round posted: stocks, pre-market move, headlines, ⬆️/⬇️/🎯x2 buttons. The AI's calls are made, hidden.
  09:20  reminder, tagging regular players who still have picks to make.
  09:30  picks lock (votes are judged by the server time they arrive); the AI's calls and double-down revealed.
  09:35+ opening prices captured.
  11:00  halftime: live standings from the current prices.
  close + 10 min  results, streaks, the AI's banter, weekly titles on the last trading day of the week.
Every stage is idempotent and driven by tick(), so a restart (or a missed stage) catches up.

Scoring: 1 point per correct call. One pick a day may be doubled: +2 if right, -1 if wrong."""
from __future__ import annotations

import datetime as dt
import html
import json
import random

from . import market
from .config import NY, SG
from .store import BOT_ID, Store

POOL = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO", "PLTR", "COIN",
        "INTC", "DIS", "NKE", "SBUX", "BA", "JPM", "UBER", "SHOP", "MU", "ORCL", "CRM", "PYPL", "ADBE",
        "COST", "WMT", "KO", "MCD", "XOM", "LLY", "UNH", "V", "GS", "ABNB", "SNOW", "ARM", "SMCI", "RIVN", "HOOD"]

POST_AT = dt.time(8, 45)
LAST_POST = dt.time(9, 20)       # too late to give people time to play: skip the day
REMIND_AT = dt.time(9, 20)
AI_DEADLINE = dt.time(9, 15)     # stop asking the model; later stocks are played without the AI
OPEN_CAPTURE = dt.time(9, 35)
MIDGAME = dt.time(11, 0)
MIDGAME_LATEST = dt.timedelta(minutes=30)  # skip halftime when it would land this close to the close
SETTLE_DELAY = dt.timedelta(minutes=10)
RETRY = dt.timedelta(minutes=10)  # spacing of quote retries for halftime and settle
GIVE_UP = dt.timedelta(days=4)    # unresolved stocks are voided after this
ACTIVE_DAYS = 14                  # players who voted this recently get the reminder
STREAK_SHOW = 3
MIN_TITLE_PICKS = 3

PRACTICE_STOCKS = 3
PRACTICE_SECONDS = 120

ARROW = {"UP": "⬆️", "DOWN": "⬇️"}
DISCLAIMER = "<i>Just a game: not investment advice, no money involved.</i>"


def esc(t) -> str:
    return html.escape(str(t), quote=False)


def at(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, tzinfo=NY)


def lock_at(day: dt.date) -> dt.datetime:
    return at(day, market.OPEN)


def close_at(day: dt.date) -> dt.datetime:
    return at(day, market.close_time(day))


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def last_of_week(day: dt.date) -> bool:
    return week_start(market.next_trading_day(day)) != week_start(day)


def pick_tickers(day: dt.date, n: int, pool=POOL) -> list[str]:
    return random.Random(day.isoformat()).sample(list(pool), n)


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{esc(name)}</a>'


def _move(s: dict) -> str:
    if s.get("pre_price") and s.get("prev_close"):
        return f"pre-market {s['pre_price'] / s['prev_close'] - 1:+.1%}"
    return "no pre-market price"


def _sg(t: dt.datetime) -> str:
    return t.astimezone(SG).strftime("%H:%M")


def points(call: str, result: str | None, double: int) -> int:
    if result not in ("UP", "DOWN"):
        return 0
    return (2 if double else 1) if call == result else (-1 if double else 0)


def pts(n: int) -> str:
    return f"{n} pt" if abs(n) == 1 else f"{n} pts"


def board_lines(rows: list[dict], limit: int = 10, streaks: dict | None = None) -> list[str]:
    medals = ["🥇", "🥈", "🥉"]
    out = []
    for i, r in enumerate(rows[:limit]):
        fire = f" 🔥{streaks[r['user_id']]}" if streaks and streaks.get(r["user_id"], 0) >= STREAK_SHOW else ""
        out.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(r['name'])}: {pts(r['points'])} "
                   f"({r['correct']}/{r['played']}){fire}")
    return out or ["(no settled picks yet)"]


def fallback_banter(bot: str, today: list[dict]) -> str:
    """The AI's results line when the model is unavailable."""
    humans = [r for r in today if r["user_id"] != BOT_ID]
    me = next((r for r in today if r["user_id"] == BOT_ID), None)
    if not humans:
        return f"No humans showed up today. I win by forfeit 😎"
    best, worst = humans[0], humans[-1]
    if me and best["points"] > me["points"]:
        return f"Fine, {best['name']} beat me today. Enjoy it, it won't last 😤"
    if len(humans) > 1 and worst["correct"] <= worst["played"] // 2:
        return f"{worst['name']} went {worst['correct']}/{worst['played']}. I've seen goldfish trade better 🐟"
    return "Another day, another win for the machine 🤖"


def help_text(title: str, bot: str) -> str:
    return (f"🎯 <b>{esc(title)}</b>\nEvery US trading day a round of stocks is posted before the open "
            "(21:45 Singapore in winter, 20:45 in summer). Tap ⬆️ if you think a stock closes above its opening "
            "price, ⬇️ if below. Tap 🎯 x2 on your surest pick to double it: +2 if right, −1 if wrong. "
            f"You can change your mind until the 09:30 New York open. {esc(bot)} (our own AI) plays too.\n"
            "Halftime scores around midnight, results before breakfast, titles every Friday.\n\n"
            "/today: today's round · /board: leaderboard · /me: your record · "
            "/practice: a quick 2-minute round anytime (not scored)\n" + DISCLAIMER)


class Game:
    def __init__(self, settings, store: Store, tg, quotes, news, call_ai, ai_ready, banter=None, log=print,
                 clock=None):
        self.s, self.store, self.tg = settings, store, tg
        self.quotes, self.news, self.call_ai, self.ai_ready = quotes, news, call_ai, ai_ready
        self.banter, self.log = banter, log
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.title = getattr(settings, "game_name", "") or "Huat or Not?"
        self.bot = store.bot_name
        self.short = self.bot.lstrip("".join(ch for ch in self.bot if not (ch.isalnum() or ch == " "))).strip() or self.bot
        self._tried: dict[tuple, dt.datetime] = {}

    # ---------------------------------------------------------------- texts
    def round_text(self, day: dt.date, stocks: list[dict], voters: int = 0) -> str:
        lines = [f"🎯 <b>{esc(self.title)}</b> · {day:%a %d %b}",
                 "Will each stock <b>close above ⬆️ or below ⬇️ its opening price</b> today?",
                 "🎯 <b>x2</b> = double down on your surest pick: +2 if right, −1 if wrong (one per day).",
                 f"Picks lock at the open: 09:30 New York ({_sg(lock_at(day))} Singapore). "
                 f"{esc(self.bot)} has already made its calls; they are revealed at the lock.", ""]
        for i, s in enumerate(stocks, 1):
            lines.append(f"<b>{i}. {esc(s['ticker'])}</b> {esc(s['name'])} · {_move(s)}")
            for h in s["headlines"][:2]:
                lines.append(f"   • {esc(h['title'][:110])}")
            if not s["headlines"]:
                lines.append("   • (no fresh headlines)")
        lines += ["", f"Players so far: {voters}", DISCLAIMER]
        return "\n".join(lines)

    @staticmethod
    def buttons(day: dt.date, stocks: list[dict]) -> list:
        key = day.strftime("%Y%m%d")
        return [[(f"⬆️ {s['ticker']}", f"v|{key}|{s['ticker']}|U"), (f"⬇️ {s['ticker']}", f"v|{key}|{s['ticker']}|D"),
                 ("🎯 x2", f"d|{key}|{s['ticker']}")] for s in stocks]

    def lock_text(self, day: dt.date, stocks: list[dict], voters: int) -> str:
        lines = [f"🔒 <b>Picks locked</b> · {day:%a %d %b} · {voters} player{'s' if voters != 1 else ''}",
                 f"{esc(self.bot)}'s calls:"]
        doubled = None
        for s in stocks:
            if s["ai_call"]:
                lines.append(f"{ARROW[s['ai_call']]} <b>{esc(s['ticker'])}</b>: {esc(s['ai_reason'] or '(no comment)')}")
                if s.get("ai_double"):
                    doubled = s["ticker"]
            else:
                lines.append(f"💤 <b>{esc(s['ticker'])}</b>: {esc(self.bot)} sat this one out (model unavailable)")
        if doubled:
            lines.append(f"🎯 {esc(self.short)} doubles down on <b>{esc(doubled)}</b>.")
        mid = at(day, MIDGAME)
        if mid <= close_at(day) - MIDGAME_LATEST:
            lines.append(f"Halftime scores at {_sg(mid)} Singapore, results at {_sg(close_at(day) + SETTLE_DELAY)}.")
        else:
            lines.append(f"Results at {_sg(close_at(day) + SETTLE_DELAY)} Singapore.")
        return "\n".join(lines)

    def results_text(self, day: dt.date, banter: str = "") -> str:
        key = day.isoformat()
        stocks, votes = self.store.stocks(key), self.store.votes(key)
        lines = [f"🏁 <b>Results</b> · {day:%a %d %b}"]
        for s in stocks:
            if s["result"] in ("UP", "DOWN"):
                mine = [v for v in votes if v["ticker"] == s["ticker"] and v["user_id"] != BOT_ID]
                right = sum(v["call"] == s["result"] for v in mine)
                bot = "" if not s["ai_call"] else (" · AI ✅" if s["ai_call"] == s["result"] else " · AI ❌")
                lines.append(f"{ARROW[s['result']]} <b>{esc(s['ticker'])}</b> {s['open']:.2f} → {s['close']:.2f} "
                             f"({s['close'] / s['open'] - 1:+.1%}) · {right}/{len(mine)} humans right{bot}")
            else:
                why = "closed flat" if s["result"] == "FLAT" else "price unavailable"
                lines.append(f"➖ <b>{esc(s['ticker'])}</b>: no result ({why}), not scored")
        today = self.store.scores(key, key)
        streaks = self.store.streaks()
        if today:
            bot = next((r for r in today if r["user_id"] == BOT_ID), None)
            humans = [r for r in today if r["user_id"] != BOT_ID]
            lines += ["", "<b>Today</b>", *board_lines(today, streaks=streaks)]
            if bot and humans:
                beat = [esc(r["name"]) for r in humans if r["points"] > bot["points"]]
                lines.append(f"🏆 Beat {esc(self.short)} today: " + ", ".join(beat) if beat
                             else f"🤖 Nobody beat {esc(self.short)} today.")
        if banter:
            lines += ["", f"💬 <b>{esc(self.bot)}:</b> {esc(banter)}"]
        ws = week_start(day).isoformat()
        lines += ["", "<b>This week</b>", *board_lines(self.store.scores(ws, key), streaks=streaks)]
        if last_of_week(day):
            lines += ["", "<b>Titles of the week</b>", *self.titles(ws, key)]
        return "\n".join(lines)

    def titles(self, since: str, until: str) -> list[str]:
        rows = self.store.scores(since, until)
        bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
        humans = [r for r in rows if r["user_id"] != BOT_ID and r["played"] >= MIN_TITLE_PICKS]
        if not humans:
            return [f"(nobody made {MIN_TITLE_PICKS}+ picks)"]
        top, out = humans[0], []
        if not bot or top["points"] > bot["points"]:
            out.append(f"👑 Bot Slayer: <b>{esc(top['name'])}</b> ({pts(top['points'])})")
        elif top["points"] == bot["points"]:
            out.append(f"👑 Top Human: <b>{esc(top['name'])}</b> ({pts(top['points'])}), tied with {esc(self.short)} 😤")
        else:
            out.append(f"👑 Top Human: <b>{esc(top['name'])}</b> ({pts(top['points'])}), "
                       f"but {esc(self.short)} won the week with {pts(bot['points'])} 🤖")
        if len(humans) > 1:
            low = humans[-1]
            out.append(f"🤡 {esc(self.short)}'s Snack: <b>{esc(low['name'])}</b> ({pts(low['points'])})")
        return out

    def help_text(self) -> str:
        return help_text(self.title, self.bot)

    # ---------------------------------------------------------------- stages
    def tick(self, now: dt.datetime) -> None:
        ny = now.astimezone(NY)
        day = ny.date()
        rows = self.store.db.execute(
            "SELECT day, status, message_id FROM rounds WHERE status IN ('open','locked') ORDER BY day").fetchall()
        for r in rows:
            d = dt.date.fromisoformat(r["day"])
            if r["status"] == "open" and ny < lock_at(d):
                if r["message_id"] is None:
                    self.post(d)  # the first post failed (Telegram down): keep trying until the lock
                elif ny >= at(d, REMIND_AT):
                    self.remind(d)
            if r["status"] == "open" and ny >= lock_at(d):
                self.lock(d)
            if ny >= lock_at(d):
                self.capture_open(d, ny)
                self.midgame(d, ny)
                self.settle(d, ny)
        if (market.is_trading_day(day) and self.store.round(day.isoformat()) is None
                and POST_AT <= ny.time() < LAST_POST):
            self.create(day, now)
        self.practice_reveal(now)

    def _due(self, what: str, key: str, ny: dt.datetime) -> bool:
        """Space out retries that hit OpenD."""
        last = self._tried.get((what, key))
        if last and ny - last < RETRY:
            return False
        self._tried[(what, key)] = ny
        return True

    def create(self, day: dt.date, now: dt.datetime) -> None:
        tickers = pick_tickers(day, self.s.stocks_per_day)
        try:
            q = self.quotes(tickers)
        except Exception as e:  # quotes are context only at this stage
            self.log(f"pre-market quotes failed: {e!r}"[:300])
            q = {}
        ready = self.ai_ready()
        if not ready:
            self.log("model unavailable: the AI sits today out")
        stocks = []
        for t in tickers:
            info = q.get(t, {})
            s = {"ticker": t, "name": info.get("name") or t, "prev_close": info.get("prev_close"),
                 "pre_price": info.get("pre_price"), "ai_call": None, "ai_reason": "", "ai_conf": None}
            s["headlines"] = [{"title": h["title"], "published": h["published"]} for h in self.news(t, s["name"])]
            if ready and self.clock() < at(day, AI_DEADLINE):
                got = self.call_ai(t, s["name"], info, s["headlines"])
                if got:
                    s["ai_call"], s["ai_reason"], s["ai_conf"] = got
            stocks.append(s)
        for s in stocks:  # store JSON-safe headlines
            s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                              for h in s["headlines"]]
        self.store.add_round(day.isoformat(), stocks, now.isoformat())
        self.post(day)

    def post(self, day: dt.date) -> None:
        key = day.isoformat()
        stocks = self.store.stocks(key)
        mid = self.tg.send(self.round_text(day, stocks, self.voters(key)), self.buttons(day, stocks))
        if mid:
            self.store.set_message(key, mid)

    def voters(self, key: str) -> int:
        return len({v["user_id"] for v in self.store.votes(key) if v["user_id"] != BOT_ID})

    def remind(self, day: dt.date) -> None:
        key = day.isoformat()
        if self.store.get(f"reminded:{key}"):
            return
        n = len(self.store.stocks(key))
        since = (day - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
        rows = self.store.db.execute(
            "SELECT p.user_id, p.name, (SELECT COUNT(*) FROM votes t WHERE t.day=? AND t.user_id=p.user_id) AS today "
            "FROM players p WHERE p.user_id != ? AND EXISTS "
            "(SELECT 1 FROM votes v WHERE v.user_id=p.user_id AND v.day >= ? AND v.day < ?) ORDER BY p.name",
            (key, BOT_ID, since, key)).fetchall()
        missing = [r for r in rows if r["today"] < n]
        text = "⏰ <b>10 minutes to lock!</b> "
        if missing:
            text += ", ".join(mention(r["user_id"], r["name"]) + (f" ({n - r['today']} left)" if r["today"] else "")
                              for r in missing) + f": your picks vs {esc(self.bot)} are waiting."
        else:
            text += f"{self.voters(key)} player(s) in so far. Last chance to beat {esc(self.bot)}."
        self.tg.send(text, None)
        self.store.set(f"reminded:{key}", "1")

    def lock(self, day: dt.date) -> None:
        key = day.isoformat()
        stocks = self.store.stocks(key)
        called = [s for s in stocks if s["ai_call"]]
        best = max(called, key=lambda s: s["ai_conf"] or 0.0)["ticker"] if called else None
        for s in called:
            self.store.vote(key, s["ticker"], BOT_ID, s["ai_call"], lock_at(day).isoformat())
        if best:
            self.store.set_double(key, BOT_ID, best)
            self.store.update_stock(key, best, ai_double=1)
        self.store.set_status(key, "locked")
        stocks = self.store.stocks(key)
        r = self.store.round(key)
        if r["message_id"]:
            self.tg.edit(r["message_id"], self.round_text(day, stocks, self.voters(key)) + "\n🔒 <b>Locked.</b>", None)
        self.tg.send(self.lock_text(day, stocks, self.voters(key)), None)

    def capture_open(self, day: dt.date, ny: dt.datetime) -> None:
        key = day.isoformat()
        missing = [s["ticker"] for s in self.store.stocks(key) if s["open"] is None and s["result"] is None]
        if not missing or ny < at(day, OPEN_CAPTURE) or not self._due("open", key, ny):
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

    def midgame(self, day: dt.date, ny: dt.datetime) -> None:
        key = day.isoformat()
        if self.store.get(f"halftime:{key}") or ny < at(day, MIDGAME):
            return
        if ny >= close_at(day) - MIDGAME_LATEST or self.store.round(key)["status"] != "locked":
            self.store.set(f"halftime:{key}", "skipped")
            return
        if not self._due("mid", key, ny):
            return
        stocks = self.store.stocks(key)
        try:
            q = self.quotes([s["ticker"] for s in stocks])
        except Exception as e:
            self.log(f"halftime quotes failed: {e!r}"[:300])
            return
        now_result, lines = {}, [f"⏱ <b>Halftime</b> · {day:%a %d %b} (live prices, not final)"]
        for s in stocks:
            info = q.get(s["ticker"]) or {}
            o = s["open"] or (info.get("open") if info.get("update_time") and info["update_time"].date() == day else None)
            last = info.get("last") if info.get("update_time") and info["update_time"].date() == day else None
            if o and last:
                res = "UP" if last > o else "DOWN" if last < o else None
                now_result[s["ticker"]] = res
                lines.append(f"{ARROW.get(res, '➖')} <b>{esc(s['ticker'])}</b> {last / o - 1:+.1%} since the open")
            else:
                lines.append(f"➖ <b>{esc(s['ticker'])}</b>: no live price")
        totals: dict[int, list] = {}
        for v in self.store.votes(key):
            t = totals.setdefault(v["user_id"], [v["name"], 0])
            t[1] += points(v["call"], now_result.get(v["ticker"]), v["double"])
        ranked = sorted(totals.items(), key=lambda kv: -kv[1][1])
        if ranked:
            lines += ["", "<b>If it ended now</b>"] + [f"{esc(n)}: {pts(p)}" for _, (n, p) in ranked[:10]]
            bot = totals.get(BOT_ID)
            humans = [p for uid, (_, p) in totals.items() if uid != BOT_ID]
            if bot and humans:
                lines.append(f"🤖 {esc(self.short)} is losing 😬" if max(humans) > bot[1]
                             else f"🤖 {esc(self.short)} is leading 😎" if max(humans) < bot[1]
                             else f"🤖 Neck and neck with {esc(self.short)} 😤")
        self.tg.send("\n".join(lines), None, silent=False)  # around midnight Singapore: the point is to ping
        self.store.set(f"halftime:{key}", "posted")

    def settle(self, day: dt.date, ny: dt.datetime) -> None:
        key = day.isoformat()
        if ny < close_at(day) + SETTLE_DELAY:
            return
        pending = [s for s in self.store.stocks(key) if s["result"] is None]
        if pending:
            if not self._due("settle", key, ny):
                return
            try:
                q = self.quotes([s["ticker"] for s in pending])
            except Exception as e:
                self.log(f"settle quotes failed: {e!r}"[:300])
                q = None
            give_up = ny >= close_at(day) + GIVE_UP
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
            self.tg.send(self.results_text(day, self._banter(day)), None)

    def _banter(self, day: dt.date) -> str:
        key = day.isoformat()
        today = self.store.scores(key, key)
        if not today:
            return ""
        if self.banter:
            stocks = [{"ticker": s["ticker"], "result": s["result"],
                       "move": round(s["close"] / s["open"] - 1, 4) if s["open"] and s["close"] else None}
                      for s in self.store.stocks(key)]
            try:
                text = self.banter({"stocks": stocks, "scores": today, "you": BOT_ID})
                if text:
                    return text
            except Exception as e:
                self.log(f"banter failed: {e!r}"[:300])
        return fallback_banter(self.bot, today)

    # ---------------------------------------------------------------- players
    def _open_round(self, data: str, now: dt.datetime):
        parts = data.split("|")
        try:
            day = dt.datetime.strptime(parts[1], "%Y%m%d").date()
            ticker = parts[2]
        except (IndexError, ValueError):
            return None, "Unknown button."
        key = day.isoformat()
        r = self.store.round(key)
        if r is None or ticker not in {s["ticker"] for s in self.store.stocks(key)}:
            return None, "That round no longer exists."
        if r["status"] != "open" or now >= lock_at(day):
            return None, "Too late: picks are locked."
        return (day, key, ticker, parts), ""

    def on_button(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        if data.startswith("p|"):
            return self.practice_vote(data, user_id, name, now)
        if data.startswith("d|"):
            return self.on_double(data, user_id, name, now)
        return self.on_vote(data, user_id, name, now)

    def on_vote(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        got, err = self._open_round(data, now)
        if not got:
            return err
        day, key, ticker, parts = got
        c = parts[3] if len(parts) > 3 else ""
        if c not in ("U", "D"):
            return "Unknown button."
        call = "UP" if c == "U" else "DOWN"
        self.store.player(user_id, name)
        self.store.vote(key, ticker, user_id, call, now.isoformat())
        return f"{ticker} {ARROW[call]} saved. You can change it until the open."

    def on_double(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        got, err = self._open_round(data, now)
        if not got:
            return err
        day, key, ticker, _ = got
        mine = {v["ticker"]: v for v in self.store.votes(key) if v["user_id"] == user_id}
        if ticker not in mine:
            return f"Pick ⬆️ or ⬇️ on {ticker} first, then double it."
        if mine[ticker]["double"]:
            self.store.set_double(key, user_id, None)
            return f"Double down removed from {ticker}."
        self.store.set_double(key, user_id, ticker)
        return f"🎯 Doubled {ticker} {ARROW[mine[ticker]['call']]}: +2 if right, −1 if wrong."

    def on_command(self, text: str, user_id: int, name: str, now: dt.datetime) -> tuple[str, list | None] | None:
        cmd = text.split()[0].split("@")[0].lower() if text.strip() else ""
        ny = now.astimezone(NY)
        if cmd in ("/start", "/help"):
            return self.help_text(), None
        if cmd == "/practice":
            return self.practice_start(now)
        if cmd == "/today":
            row = self.store.db.execute("SELECT day FROM rounds ORDER BY day DESC LIMIT 1").fetchone()
            if not row:
                return "No round yet. The first one is posted before the next US open.", None
            day = dt.date.fromisoformat(row["day"])
            r = self.store.round(row["day"])
            stocks = self.store.stocks(row["day"])
            if r["status"] == "open":
                return self.round_text(day, stocks, self.voters(row["day"])), self.buttons(day, stocks)
            if r["status"] == "locked":
                return self.lock_text(day, stocks, self.voters(row["day"])), None
            return self.results_text(day), None
        if cmd == "/board":
            today, streaks = ny.date(), self.store.streaks()
            last_week = week_start(today) - dt.timedelta(days=7)
            return ("<b>This week</b>\n" + "\n".join(board_lines(self.store.scores(week_start(today).isoformat(),
                                                                                    today.isoformat()), streaks=streaks))
                    + "\n\n<b>All time</b>\n" + "\n".join(board_lines(self.store.scores(), streaks=streaks))
                    + "\n\n<b>Last week's titles</b>\n"
                    + "\n".join(self.titles(last_week.isoformat(), (last_week + dt.timedelta(days=6)).isoformat()))), None
        if cmd == "/me":
            rows = self.store.scores()
            mine = next((r for r in rows if r["user_id"] == user_id), None)
            bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
            if not mine:
                return f"{esc(name)}: no settled picks yet. Tap the buttons on today's round!", None
            streak = self.store.streaks().get(user_id, 0)
            vs = f"\n{esc(self.bot)}: {pts(bot['points'])} ({bot['correct']}/{bot['played']})" if bot else ""
            return (f"{esc(name)}: {pts(mine['points'])} · {mine['correct']}/{mine['played']} correct "
                    f"({mine['correct'] / mine['played']:.0%}) · 🔥 {streak} in a row{vs}"), None
        return None


    # ---------------------------------------------------------------- practice
    # A 2-minute unscored round on the last COMPLETED session (or today's move so far while the market is open),
    # whose answer is already in the quote snapshot. It exercises buttons, the AI and results in the real group
    # at any hour. State lives in kv as JSON; it never touches rounds, votes or scores.

    def _practice(self) -> dict | None:
        raw = self.store.get("practice")
        return json.loads(raw) if raw else None

    def practice_start(self, now: dt.datetime) -> tuple[str, list | None]:
        p = self._practice()
        if p and now < dt.datetime.fromisoformat(p["ends"]) + dt.timedelta(minutes=5):
            return "A practice round is already running: tap the buttons above.", None
        rng = random.Random(now.isoformat())
        tickers = rng.sample(POOL, PRACTICE_STOCKS)
        try:
            q = self.quotes(tickers)
        except Exception as e:
            self.log(f"practice quotes failed: {e!r}"[:300])
            return "Practice needs live quotes and Moomoo isn't answering right now. Try again in a bit.", None
        stocks = []
        for t in tickers:
            info = q.get(t) or {}
            ut = info.get("update_time")
            if not (info.get("open") and info.get("last") and ut):
                continue
            live = ut.date() == now.astimezone(NY).date() and ut.time() < market.close_time(ut.date())
            stocks.append({"ticker": t, "name": info.get("name") or t, "open": info["open"], "close": info["last"],
                           "day": ut.date().isoformat(), "live": live, "ai_call": None, "ai_reason": ""})
        if not stocks:
            return "No usable prices for practice right now. Try again in a bit.", None
        if self.ai_ready():
            for s in stocks:
                # Same information as the players: no pre-market price (the snapshot's is from a later session).
                got = self.call_ai(s["ticker"], s["name"], {"prev_close": (q.get(s["ticker"]) or {}).get("prev_close")}, [])
                if got:
                    s["ai_call"], s["ai_reason"] = got[0], got[1]
        pid = now.strftime("%H%M%S")
        ends = now + dt.timedelta(seconds=PRACTICE_SECONDS)
        self.store.set("practice", json.dumps({"id": pid, "ends": ends.isoformat(), "stocks": stocks,
                                               "votes": {}, "names": {}}))
        day = dt.date.fromisoformat(stocks[0]["day"])
        when = "so far today" if stocks[0]["live"] else f"on {day:%a %d %b}"
        lines = [f"🧪 <b>Practice round</b> (not scored) · answers in {PRACTICE_SECONDS // 60} minutes",
                 f"Did each stock finish <b>above ⬆️ or below ⬇️ its opening price</b> {when}? No peeking at charts!",
                 f"{esc(self.bot)} has made its calls too.", ""]
        lines += [f"<b>{esc(s['ticker'])}</b> {esc(s['name'])}" for s in stocks]
        buttons = [[(f"⬆️ {s['ticker']}", f"p|{pid}|{s['ticker']}|U"), (f"⬇️ {s['ticker']}", f"p|{pid}|{s['ticker']}|D")]
                   for s in stocks]
        return "\n".join(lines), buttons

    def practice_vote(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        p = self._practice()
        try:
            _, pid, ticker, c = data.split("|")
        except ValueError:
            return "Unknown button."
        if not p or p["id"] != pid or ticker not in {s["ticker"] for s in p["stocks"]} or c not in ("U", "D"):
            return "That practice round is over. Send /practice for a new one."
        if now >= dt.datetime.fromisoformat(p["ends"]):
            return "Time's up: answers coming."
        call = "UP" if c == "U" else "DOWN"
        p["votes"].setdefault(str(user_id), {})[ticker] = call
        p["names"][str(user_id)] = name
        self.store.set("practice", json.dumps(p))
        return f"{ticker} {ARROW[call]} saved (practice)."

    def practice_reveal(self, now: dt.datetime) -> None:
        p = self._practice()
        if not p or p.get("revealed") or now < dt.datetime.fromisoformat(p["ends"]):
            return
        lines = ["🧪 <b>Practice answers</b>"]
        right: dict[str, int] = {}
        bot_right = 0
        for s in p["stocks"]:
            res = "UP" if s["close"] > s["open"] else "DOWN" if s["close"] < s["open"] else None
            ai = "" if not s["ai_call"] else (f" · {esc(self.short)} {ARROW[s['ai_call']]} " +
                                              ("✅" if s["ai_call"] == res else "❌"))
            bot_right += bool(s["ai_call"] and s["ai_call"] == res)
            lines.append(f"{ARROW.get(res, '➖')} <b>{esc(s['ticker'])}</b> {s['open']:.2f} → {s['close']:.2f} "
                         f"({s['close'] / s['open'] - 1:+.1%}){ai}")
            if s["ai_call"] and s["ai_reason"]:
                lines.append(f"   <i>{esc(s['ai_reason'])}</i>")
            for uid, calls in p["votes"].items():
                right[uid] = right.get(uid, 0) + (calls.get(s["ticker"]) == res)
        n = len(p["stocks"])
        if p["votes"]:
            lines.append("")
            lines += [f"{esc(p['names'][uid])}: {k}/{n}" for uid, k in sorted(right.items(), key=lambda kv: -kv[1])]
        else:
            lines += ["", "Nobody played. Send /practice to try again."]
        if any(s["ai_call"] for s in p["stocks"]):
            lines.append(f"{esc(self.bot)}: {bot_right}/{n}")
        lines.append("The real round posts every US trading day at 21:45 Singapore (20:45 in summer).")
        self.tg.send("\n".join(lines), None, silent=False)
        p["revealed"] = True
        self.store.set("practice", json.dumps(p))
