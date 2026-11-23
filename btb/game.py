"""The game: a daily round of stocks; players and the AI call each close as above (UP) or below (DOWN) its open.

Timeline on each NYSE trading day (New York time; Singapore is +13h in winter, +12h in summer):
  07:30  round posted to every group: 3 of the day's biggest pre-market movers + 2 random stocks, headlines,
         ⬆️/⬇️/🎯x2 buttons and the 🚀 biggest-mover bonus. The AI's calls are made, hidden.
  09:20  reminder in each group, tagging its regular players who still have picks to make.
  09:30  picks lock (votes are judged by the server time they arrive); the AI's calls and doubles revealed.
  09:35+ opening prices captured.
  11:00  halftime: live standings from the current prices.
  close + 10 min  results, streaks, the AI's banter, weekly titles, league and scorecard on the week's last day.
Every stage is idempotent and driven by tick(), so a restart (or a missed stage) catches up.

Scoring: 1 point per correct call. One pick a day may be doubled: +2 if right, -1 if wrong.
Bonus: +2 for naming the stock with the biggest open-to-close move (either way).
One round a day is shared by all groups; a player's picks count in every group they play in."""
from __future__ import annotations

import datetime as dt
import html
import json
import random

from . import market
from .config import NY, SG
from .store import BOT_ID, VOID, Store
from .telegram import Forbidden

POOL = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO", "PLTR", "COIN",
        "INTC", "DIS", "NKE", "SBUX", "BA", "JPM", "UBER", "SHOP", "MU", "ORCL", "CRM", "PYPL", "ADBE",
        "COST", "WMT", "KO", "MCD", "XOM", "LLY", "UNH", "V", "GS", "ABNB", "SNOW", "ARM", "SMCI", "RIVN", "HOOD"]

POST_AT = dt.time(7, 30)         # 20:30 Singapore in winter: two hours to play
LAST_POST = dt.time(9, 20)       # too late to give people time to play: skip the day
REMIND_AT = dt.time(9, 20)
AI_DEADLINE = dt.time(9, 15)     # stop asking the model; later stocks are played without the AI
OPEN_CAPTURE = dt.time(9, 35)
MIDGAME = dt.time(11, 0)
MIDGAME_LATEST = dt.timedelta(minutes=30)  # skip halftime when it would land this close to the close
SETTLE_DELAY = dt.timedelta(minutes=10)
RETRY = dt.timedelta(minutes=10)  # spacing of quote retries for halftime and settle
POST_RETRY = dt.timedelta(minutes=2)
GIVE_UP = dt.timedelta(days=4)    # unresolved stocks are voided after this
ACTIVE_DAYS = 14                  # players who voted this recently get the reminder
STREAK_SHOW = 3
MIN_TITLE_PICKS = 3
MOVERS = 3                        # of the day's stocks, this many are the biggest pre-market movers
BANTER_MODEL_GROUPS = 5           # groups beyond this get template banter (one model call per group)
PRACTICE_STOCKS = 3
PRACTICE_SECONDS = 120
PRACTICE_COOLDOWN = dt.timedelta(minutes=10)  # per group, from the start of its last practice
PRACTICE_AI_PER_HOUR = 6                       # across groups: the model is shared with other work

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


def pre_move(q: dict | None) -> float | None:
    if q and q.get("pre_price") and q.get("prev_close"):
        return q["pre_price"] / q["prev_close"] - 1
    return None


def pick_stocks(day: dt.date, n: int, quotes: dict, pool=POOL, movers: int = MOVERS) -> list[str]:
    """The day's biggest pre-market movers (they have news behind them) plus random others, in shuffled order."""
    rng = random.Random(day.isoformat())
    moved = sorted((t for t in pool if pre_move(quotes.get(t)) is not None), key=lambda t: -abs(pre_move(quotes[t])))
    top = moved[:min(movers, n)]
    picks = top + rng.sample([t for t in pool if t not in top], n - len(top))
    rng.shuffle(picks)
    return picks


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{esc(name)}</a>'


def _move(s: dict) -> str:
    m = pre_move(s)
    return f"pre-market {m:+.1%}" if m is not None else "no pre-market price"


def _sg(t: dt.datetime) -> str:
    return t.astimezone(SG).strftime("%H:%M")


def pts(n: int) -> str:
    return f"{n} pt" if abs(n) == 1 else f"{n} pts"


def points(call: str, result: str | None, double: int) -> int:
    if result not in ("UP", "DOWN"):
        return 0
    return (2 if double else 1) if call == result else (-1 if double else 0)


def move_of(s: dict) -> float | None:
    return s["close"] / s["open"] - 1 if s.get("open") and s.get("close") else None


def biggest_mover(stocks: list[dict]) -> str:
    """Ticker with the largest absolute open-to-close move; VOID if any stock has no result (it could have been it)."""
    if any(s["result"] not in ("UP", "DOWN", "FLAT") or move_of(s) is None for s in stocks):
        return VOID
    return max(stocks, key=lambda s: abs(move_of(s)))["ticker"]


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
        return "No humans showed up today. I win by forfeit 😎"
    best, worst = humans[0], humans[-1]
    if me and best["points"] > me["points"]:
        return f"Fine, {best['name']} beat me today. Enjoy it, it won't last 😤"
    if len(humans) > 1 and worst["correct"] <= worst["played"] // 2:
        return f"{worst['name']} went {worst['correct']}/{worst['played']}. I've seen goldfish trade better 🐟"
    return "Another day, another win for the machine 🤖"


def help_text(title: str, bot: str) -> str:
    return (f"🎯 <b>{esc(title)}</b>\nEvery US trading day a round of stocks is posted before the open "
            "(20:30 Singapore in winter, 19:30 in summer). Tap ⬆️ if you think a stock closes above its opening "
            "price, ⬇️ if below. Tap 🎯 x2 on your surest pick to double it: +2 if right, −1 if wrong. "
            "🚀 Bonus: name the stock that moves the most (either way) for +2. "
            f"You can change your mind until the 09:30 New York open. {esc(bot)} (our own AI) plays too.\n"
            "Halftime scores around midnight, results before breakfast, titles and scorecards every Friday.\n\n"
            "/today: today's round · /board: leaderboard · /me: your record · /card: your weekly scorecard\n"
            f"/bot: {esc(bot)}'s record · /league: groups vs groups · /practice: a 2-minute round anytime\n"
            + DISCLAIMER)


class Game:
    def __init__(self, settings, store: Store, tg, quotes, news, call_ai, ai_ready, banter=None, call_mover=None,
                 cards=None, log=print, clock=None):
        self.s, self.store, self.tg = settings, store, tg
        self.quotes, self.news, self.call_ai, self.ai_ready = quotes, news, call_ai, ai_ready
        self.banter, self.call_mover, self.log = banter, call_mover, log
        if cards is None:
            from . import cards as cards_module
            cards = cards_module
        self.cards = cards
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.title = getattr(settings, "game_name", "") or "Huat or Not?"
        self.bot = store.bot_name
        self.short = self.bot.lstrip("".join(ch for ch in self.bot if not (ch.isalnum() or ch == " "))).strip() or self.bot
        self.max_groups = getattr(settings, "max_groups", 20)
        self._tried: dict[tuple, dt.datetime] = {}

    # ---------------------------------------------------------------- sending
    def send(self, chat_id: int, text: str, buttons=None, silent=None) -> int | None:
        try:
            return self.tg.send(text, buttons, chat_id=chat_id, silent=silent)
        except Forbidden:
            self.log(f"removed from group {chat_id}")
            self.store.deactivate(chat_id)
        except Exception as e:
            self.log(f"send to {chat_id} failed: {e!r}"[:300])
        return None

    def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        try:
            self.tg.edit(message_id, text, buttons, chat_id=chat_id)
        except Exception as e:
            self.log(f"edit in {chat_id} failed: {e!r}"[:300])

    def chat_ids(self) -> list[int]:
        return [c["chat_id"] for c in self.store.chats()]

    # ---------------------------------------------------------------- texts
    def round_text(self, day: dt.date, stocks: list[dict], voters: int = 0) -> str:
        lines = [f"🎯 <b>{esc(self.title)}</b> · {day:%a %d %b}",
                 "Will each stock <b>close above ⬆️ or below ⬇️ its opening price</b> today?",
                 "🎯 <b>x2</b> = double down on your surest pick: +2 if right, −1 if wrong (one per day).",
                 "🚀 <b>Bonus</b>: which one moves the MOST (up or down)? +2 if you name it.",
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
        rows = [[(f"⬆️ {s['ticker']}", f"v|{key}|{s['ticker']}|U"), (f"⬇️ {s['ticker']}", f"v|{key}|{s['ticker']}|D"),
                 ("🎯 x2", f"d|{key}|{s['ticker']}")] for s in stocks]
        rows.append([(f"🚀 {s['ticker']}", f"m|{key}|{s['ticker']}") for s in stocks])
        return rows

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
        r = self.store.round(day.isoformat())
        if r and r["ai_mover"]:
            lines.append(f"🚀 {esc(self.short)}'s biggest mover: <b>{esc(r['ai_mover'])}</b>"
                         + (f": {esc(r['ai_mover_reason'])}" if r["ai_mover_reason"] else ""))
        mid = at(day, MIDGAME)
        if mid <= close_at(day) - MIDGAME_LATEST:
            lines.append(f"Halftime scores at {_sg(mid)} Singapore, results at {_sg(close_at(day) + SETTLE_DELAY)}.")
        else:
            lines.append(f"Results at {_sg(close_at(day) + SETTLE_DELAY)} Singapore.")
        return "\n".join(lines)

    def results_text(self, day: dt.date, chat_id: int | None = None, banter: str = "") -> str:
        key = day.isoformat()
        stocks, votes = self.store.stocks(key), self.store.votes(key)
        members = self.store.members(chat_id) if chat_id is not None else None
        ours = (lambda uid: uid != BOT_ID and (members is None or uid in members))
        lines = [f"🏁 <b>Results</b> · {day:%a %d %b}"]
        for s in stocks:
            if s["result"] in ("UP", "DOWN"):
                mine = [v for v in votes if v["ticker"] == s["ticker"] and ours(v["user_id"])]
                right = sum(v["call"] == s["result"] for v in mine)
                bot = "" if not s["ai_call"] else (" · AI ✅" if s["ai_call"] == s["result"] else " · AI ❌")
                lines.append(f"{ARROW[s['result']]} <b>{esc(s['ticker'])}</b> {s['open']:.2f} → {s['close']:.2f} "
                             f"({move_of(s):+.1%}) · {right}/{len(mine)} humans right{bot}")
            else:
                why = "closed flat" if s["result"] == "FLAT" else "price unavailable"
                lines.append(f"➖ <b>{esc(s['ticker'])}</b>: no result ({why}), not scored")
        r = self.store.round(key)
        if r and r["mover"] and r["mover"] != VOID:
            big = next(s for s in stocks if s["ticker"] == r["mover"])
            picks = [b for b in self.store.bonus(key) if ours(b["user_id"])]
            got = [esc(b["name"]) for b in picks if b["pick"] == r["mover"]]
            ai = "" if not r["ai_mover"] else (" · AI ✅" if r["ai_mover"] == r["mover"] else " · AI ❌")
            lines.append(f"🚀 Biggest mover: <b>{esc(r['mover'])}</b> ({move_of(big):+.1%})"
                         + (f" · +2 for {', '.join(got)}" if got else f" · {len(picks)} tried, nobody got it") + ai)
        elif r and r["mover"] == VOID:
            lines.append("🚀 Biggest mover: not scored today (a price was missing)")
        today = self.store.scores(key, key, chat_id)
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
        lines += ["", "<b>This week</b>", *board_lines(self.store.scores(ws, key, chat_id), streaks=streaks)]
        if last_of_week(day):
            lines += ["", "<b>Titles of the week</b>", *self.titles(ws, key, chat_id)]
            lines += self.league_lines(ws, key, chat_id)
        return "\n".join(lines)

    def titles(self, since: str, until: str, chat_id: int | None = None) -> list[str]:
        rows = self.store.scores(since, until, chat_id)
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

    def league_lines(self, since: str, until: str, chat_id: int | None) -> list[str]:
        table = self.store.league(since, until)
        if len(table) < 2:
            return []
        out = ["", "<b>🏟 League this week</b> (average points per player)"]
        for i, g in enumerate(table[:5], 1):
            out.append(f"{i}. {esc(g['title'])}: {g['avg']:.1f} ({g['players']} players)")
        mine = next((i for i, g in enumerate(table, 1) if g["chat_id"] == chat_id), None)
        if mine and mine > 5:
            out.append(f"… your group: #{mine} of {len(table)}")
        return out

    def help_text(self) -> str:
        return help_text(self.title, self.bot)

    # ---------------------------------------------------------------- stages
    def tick(self, now: dt.datetime) -> None:
        ny = now.astimezone(NY)
        day = ny.date()
        rows = self.store.db.execute(
            "SELECT day, status FROM rounds WHERE status IN ('open','locked') ORDER BY day").fetchall()
        for r in rows:
            d = dt.date.fromisoformat(r["day"])
            if r["status"] == "open" and ny < lock_at(d):
                for chat in self.chat_ids():
                    if self.store.post_id(r["day"], chat) is None:
                        if self._due("post", f"{r['day']}:{chat}", ny, POST_RETRY):
                            self.post(d, chat)  # new group, or Telegram failed earlier
                    elif ny >= at(d, REMIND_AT):
                        self.remind(d, chat)
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

    def _due(self, what: str, key: str, ny: dt.datetime, gap: dt.timedelta = RETRY) -> bool:
        """Space out retries."""
        last = self._tried.get((what, key))
        if last and ny - last < gap:
            return False
        self._tried[(what, key)] = ny
        return True

    def create(self, day: dt.date, now: dt.datetime) -> None:
        try:
            q = self.quotes(list(POOL))
        except Exception as e:  # quotes are context only at this stage
            self.log(f"pre-market quotes failed: {e!r}"[:300])
            q = {}
        tickers = pick_stocks(day, self.s.stocks_per_day, q) if q else pick_tickers(day, self.s.stocks_per_day)
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
        ai_mover = None
        if ready and self.call_mover and self.clock() < at(day, AI_DEADLINE):
            try:
                ai_mover = self.call_mover(stocks)
            except Exception as e:
                self.log(f"mover call failed: {e!r}"[:300])
            if ai_mover and ai_mover[0] not in tickers:
                ai_mover = None
        for s in stocks:  # store JSON-safe headlines
            s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                              for h in s["headlines"]]
        self.store.add_round(day.isoformat(), stocks, now.isoformat(), ai_mover)
        for chat in self.chat_ids():
            self._tried[("post", f"{day.isoformat()}:{chat}")] = now.astimezone(NY)
            self.post(day, chat)

    def post(self, day: dt.date, chat_id: int) -> None:
        key = day.isoformat()
        stocks = self.store.stocks(key)
        mid = self.send(chat_id, self.round_text(day, stocks, self.voters(key, chat_id)), self.buttons(day, stocks))
        if mid:
            self.store.set_post(key, chat_id, mid)

    def voters(self, key: str, chat_id: int | None = None) -> int:
        ids = {v["user_id"] for v in self.store.votes(key)} | {b["user_id"] for b in self.store.bonus(key)}
        ids.discard(BOT_ID)
        if chat_id is not None:
            ids &= self.store.members(chat_id)
        return len(ids)

    def remind(self, day: dt.date, chat_id: int) -> None:
        key = day.isoformat()
        if self.store.get(f"reminded:{key}:{chat_id}"):
            return
        n = len(self.store.stocks(key))
        since = (day - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
        rows = self.store.db.execute(
            "SELECT p.user_id, p.name, (SELECT COUNT(*) FROM votes t WHERE t.day=? AND t.user_id=p.user_id) AS today "
            "FROM players p JOIN members m ON m.user_id=p.user_id AND m.chat_id=? WHERE p.user_id != ? AND EXISTS "
            "(SELECT 1 FROM votes v WHERE v.user_id=p.user_id AND v.day >= ? AND v.day < ?) ORDER BY p.name",
            (key, chat_id, BOT_ID, since, key)).fetchall()
        missing = [r for r in rows if r["today"] < n]
        text = "⏰ <b>10 minutes to lock!</b> "
        if missing:
            text += ", ".join(mention(r["user_id"], r["name"]) + (f" ({n - r['today']} left)" if r["today"] else "")
                              for r in missing) + f": your picks vs {esc(self.bot)} are waiting."
        else:
            text += f"{self.voters(key, chat_id)} player(s) in so far. Last chance to beat {esc(self.bot)}."
        self.store.set(f"reminded:{key}:{chat_id}", "1")
        self.send(chat_id, text)

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
        r = self.store.round(key)
        if r["ai_mover"]:
            self.store.set_bonus(key, BOT_ID, r["ai_mover"], lock_at(day).isoformat())
        self.store.set_status(key, "locked")
        stocks = self.store.stocks(key)
        for chat in self.chat_ids():
            mid = self.store.post_id(key, chat)
            if mid:
                self.edit(chat, mid, self.round_text(day, stocks, self.voters(key, chat)) + "\n🔒 <b>Locked.</b>", None)
                self.send(chat, self.lock_text(day, stocks, self.voters(key, chat)))

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
        now_result, moves = {}, {}
        head = [f"⏱ <b>Halftime</b> · {day:%a %d %b} (live prices, not final)"]
        for s in stocks:
            info = q.get(s["ticker"]) or {}
            today = info.get("update_time") and info["update_time"].date() == day
            o = s["open"] or (info.get("open") if today else None)
            last = info.get("last") if today else None
            if o and last:
                res = "UP" if last > o else "DOWN" if last < o else None
                now_result[s["ticker"]], moves[s["ticker"]] = res, last / o - 1
                head.append(f"{ARROW.get(res, '➖')} <b>{esc(s['ticker'])}</b> {last / o - 1:+.1%} since the open")
            else:
                head.append(f"➖ <b>{esc(s['ticker'])}</b>: no live price")
        lead = max(moves, key=lambda t: abs(moves[t])) if len(moves) == len(stocks) and moves else None
        if lead:
            head.append(f"🚀 Biggest mover so far: <b>{esc(lead)}</b> ({moves[lead]:+.1%})")
        totals: dict[int, list] = {}
        for v in self.store.votes(key):
            t = totals.setdefault(v["user_id"], [v["name"], 0])
            t[1] += points(v["call"], now_result.get(v["ticker"]), v["double"])
        for b in self.store.bonus(key):
            t = totals.setdefault(b["user_id"], [b["name"], 0])
            t[1] += 2 if lead and b["pick"] == lead else 0
        for chat in self.chat_ids():
            if not self.store.post_id(key, chat):
                continue
            members = self.store.members(chat)
            mine = {uid: v for uid, v in totals.items() if uid == BOT_ID or uid in members}
            lines = list(head)
            ranked = sorted(mine.items(), key=lambda kv: -kv[1][1])
            if ranked:
                lines += ["", "<b>If it ended now</b>"] + [f"{esc(n)}: {pts(p)}" for _, (n, p) in ranked[:10]]
                bot = mine.get(BOT_ID)
                humans = [p for uid, (_, p) in mine.items() if uid != BOT_ID]
                if bot and humans:
                    lines.append(f"🤖 {esc(self.short)} is losing 😬" if max(humans) > bot[1]
                                 else f"🤖 {esc(self.short)} is leading 😎" if max(humans) < bot[1]
                                 else f"🤖 Neck and neck with {esc(self.short)} 😤")
            self.send(chat, "\n".join(lines), silent=False)  # around midnight Singapore: the point is to ping
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
                    self.store.update_stock(key, s["ticker"], result=VOID)
                elif info and info["update_time"] and info["update_time"].date() > market.next_trading_day(day):
                    self.store.update_stock(key, s["ticker"], result=VOID)
        stocks = self.store.stocks(key)
        if not all(s["result"] is not None for s in stocks):
            return
        self.store.set_mover(key, biggest_mover(stocks))
        self.store.set_status(key, "settled")
        ws = week_start(day).isoformat()
        for i, chat in enumerate(self.chat_ids()):
            if not self.store.post_id(key, chat):
                continue
            self.send(chat, self.results_text(day, chat, self._banter(day, chat, model=i < BANTER_MODEL_GROUPS)))
            if last_of_week(day):
                self.send_week_card(chat, ws, key)

    def _banter(self, day: dt.date, chat_id: int | None = None, model: bool = True) -> str:
        key = day.isoformat()
        today = self.store.scores(key, key, chat_id)
        if not today:
            return ""
        if self.banter and model:
            stocks = [{"ticker": s["ticker"], "result": s["result"], "move": round(move_of(s), 4) if move_of(s) else None}
                      for s in self.store.stocks(key)]
            try:
                text = self.banter({"stocks": stocks, "scores": today, "you": BOT_ID})
                if text:
                    return text
            except Exception as e:
                self.log(f"banter failed: {e!r}"[:300])
        return fallback_banter(self.bot, today)

    def send_week_card(self, chat_id: int, since: str, until: str) -> None:
        rows = self.store.scores(since, until, chat_id)
        if not any(r["user_id"] != BOT_ID for r in rows):
            return
        try:
            png = self.cards.group_card(self.title, self.short, since, until, rows)
            self.tg.send_photo(png, f"{self.title}: week of {dt.date.fromisoformat(since):%d %b}", chat_id=chat_id)
        except Exception as e:
            self.log(f"week card failed: {e!r}"[:300])

    # ---------------------------------------------------------------- groups
    def join(self, chat_id: int, title: str, now: dt.datetime) -> bool:
        """The bot was added to a group (or used in one it doesn't know yet)."""
        c = self.store.chat(chat_id)
        if c and c["active"]:
            if title and c["title"] != title:
                self.store.add_chat(chat_id, title, c["joined_at"])
            return True
        if len(self.store.chats()) >= self.max_groups:
            self.send(chat_id, f"Sorry, {esc(self.title)} is full right now ({self.max_groups} groups).")
            try:
                self.tg.leave(chat_id)
            except Exception:
                pass
            return False
        self.store.add_chat(chat_id, title, now.isoformat())
        self.send(chat_id, self.help_text())
        return True

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

    def on_button(self, data: str, user_id: int, name: str, now: dt.datetime, chat_id: int | None = None) -> str:
        if chat_id is not None:
            self.store.player(user_id, name)
            self.store.member(chat_id, user_id)
        if data.startswith("p|"):
            return self.practice_vote(data, user_id, name, now, chat_id)
        if data.startswith("d|"):
            return self.on_double(data, user_id, name, now)
        if data.startswith("m|"):
            return self.on_mover(data, user_id, name, now)
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

    def on_mover(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        got, err = self._open_round(data, now)
        if not got:
            return err
        day, key, ticker, _ = got
        self.store.player(user_id, name)
        self.store.set_bonus(key, user_id, ticker, now.isoformat())
        return f"🚀 Biggest mover: {ticker}. +2 if it moves the most, up or down."

    def on_command(self, text: str, user_id: int, name: str, now: dt.datetime, chat_id: int | None = None):
        """Returns (text, buttons), {"photo": png, "caption": text}, or None for commands it doesn't know."""
        parts = text.split()
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1].lower() if len(parts) > 1 else ""
        ny = now.astimezone(NY)
        if chat_id is not None:
            self.store.player(user_id, name)
            self.store.member(chat_id, user_id)
        if cmd in ("/start", "/help"):
            return self.help_text(), None
        if cmd == "/practice":
            return self.practice_start(now, chat_id)
        if cmd == "/today":
            row = self.store.db.execute("SELECT day FROM rounds ORDER BY day DESC LIMIT 1").fetchone()
            if not row:
                return "No round yet. The first one is posted before the next US open.", None
            day = dt.date.fromisoformat(row["day"])
            r = self.store.round(row["day"])
            stocks = self.store.stocks(row["day"])
            if r["status"] == "open":
                return self.round_text(day, stocks, self.voters(row["day"], chat_id)), self.buttons(day, stocks)
            if r["status"] == "locked":
                return self.lock_text(day, stocks, self.voters(row["day"], chat_id)), None
            return self.results_text(day, chat_id), None
        if cmd == "/board":
            today, streaks = ny.date(), self.store.streaks()
            last_week = week_start(today) - dt.timedelta(days=7)
            return ("<b>This week</b>\n" + "\n".join(board_lines(self.store.scores(
                        week_start(today).isoformat(), today.isoformat(), chat_id), streaks=streaks))
                    + "\n\n<b>All time</b>\n" + "\n".join(board_lines(self.store.scores(chat_id=chat_id), streaks=streaks))
                    + "\n\n<b>Last week's titles</b>\n"
                    + "\n".join(self.titles(last_week.isoformat(), (last_week + dt.timedelta(days=6)).isoformat(),
                                            chat_id))), None
        if cmd == "/me":
            rows = self.store.scores()
            mine = next((r for r in rows if r["user_id"] == user_id), None)
            bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
            if not mine or not mine["played"]:
                return f"{esc(name)}: no settled picks yet. Tap the buttons on today's round!", None
            streak = self.store.streaks().get(user_id, 0)
            won, tried = self.store.mover_record(user_id)
            vs = f"\n{esc(self.bot)}: {pts(bot['points'])} ({bot['correct']}/{bot['played']})" if bot else ""
            return (f"{esc(name)}: {pts(mine['points'])} · {mine['correct']}/{mine['played']} correct "
                    f"({mine['correct'] / mine['played']:.0%}) · 🔥 {streak} in a row · 🚀 {won}/{tried} movers{vs}"), None
        if cmd == "/bot":
            return self.bot_text(), None
        if cmd == "/card":
            return self.personal_card(user_id, name, ny.date())
        if cmd == "/league":
            return self.league_command(arg, chat_id, ny.date()), None
        return None

    def bot_text(self) -> str:
        rows = self.store.scores()
        me = next((r for r in rows if r["user_id"] == BOT_ID), None)
        if not me or not me["played"]:
            return f"{esc(self.bot)} hasn't finished a round yet. Come back after the first results."
        humans = [r for r in rows if r["user_id"] != BOT_ID and r["played"]]
        beaten = sum(r["points"] > me["points"] for r in humans)
        won, tried = self.store.mover_record(BOT_ID)
        calls = self.store.calls(BOT_ID)
        wrong = sorted((c for c in calls if c["call"] != c["result"]), key=lambda c: -abs(move_of(c)))[:3]
        right = sorted((c for c in calls if c["call"] == c["result"]), key=lambda c: -abs(move_of(c)))[:1]
        lines = [f"🤖 <b>{esc(self.bot)}'s record</b>",
                 f"{pts(me['points'])} · {me['correct']}/{me['played']} calls right ({me['correct'] / me['played']:.0%}) "
                 f"· 🔥 {self.store.streaks().get(BOT_ID, 0)} in a row · 🚀 {won}/{tried} movers",
                 f"Humans ahead of it all-time: {beaten} of {len(humans)}"]
        if wrong:
            lines += ["", "<b>Hall of shame</b>"]
            for c in wrong:
                lines.append(f"❌ {esc(c['ticker'])} {ARROW[c['call']]} on {dt.date.fromisoformat(c['day']):%d %b}, "
                             f"went {move_of(c):+.1%}" + (f": <i>{esc(c['ai_reason'])}</i>" if c["ai_reason"] else ""))
        if right:
            c = right[0]
            lines += ["", f"<b>Best call</b>\n✅ {esc(c['ticker'])} {ARROW[c['call']]} on "
                          f"{dt.date.fromisoformat(c['day']):%d %b}, went {move_of(c):+.1%}"]
        return "\n".join(lines)

    def personal_card(self, user_id: int, name: str, today: dt.date):
        since, until = week_start(today).isoformat(), today.isoformat()
        rows = self.store.scores(since, until)
        me = next((r for r in rows if r["user_id"] == user_id), None)
        if not me:
            return f"{esc(name)}: no settled picks this week yet, so no scorecard. Play today's round!", None
        bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
        try:
            png = self.cards.personal_card(self.title, self.short, name, since, until, me, bot,
                                           self.store.streaks().get(user_id, 0))
        except Exception as e:
            self.log(f"card failed: {e!r}"[:300])
            return "Couldn't draw your card right now, sorry.", None
        verdict = ("beat" if not bot or me["points"] > bot["points"] else
                   "tied with" if me["points"] == bot["points"] else "lost to")
        return {"photo": png, "caption": f"{name} {verdict} {self.short} this week: {pts(me['points'])}"}

    def league_command(self, arg: str, chat_id: int | None, today: dt.date) -> str:
        if chat_id is None:
            return "The league is for groups: use /league in your game group."
        if arg == "join":
            self.store.set_league(chat_id, True)
            return "🏟 This group is in the league: its name and average score are shown to the other groups."
        if arg == "leave":
            self.store.set_league(chat_id, False)
            return "This group left the league and is no longer shown to other groups."
        c = self.store.chat(chat_id)
        table = self.store.league(week_start(today).isoformat(), today.isoformat())
        lines = ["🏟 <b>League this week</b> (groups ranked by average points per player, 2+ players)"]
        lines += [f"{i}. {esc(g['title'])}: {g['avg']:.1f} ({g['players']} players)" for i, g in enumerate(table, 1)]
        if not table:
            lines.append("(no groups ranked yet)")
        lines.append("" if c and c["league"] else "")
        lines.append("This group is in the league. /league leave to hide it." if c and c["league"]
                     else "This group is not in the league. /league join to compete (shows the group name to others).")
        return "\n".join(x for x in lines if x != "")

    # ---------------------------------------------------------------- practice
    # A 2-minute unscored round per group on the last COMPLETED session (or today's move so far while the market is
    # open), whose answer is already in the quote snapshot. It exercises buttons, the AI and results at any hour.
    # State lives in kv as JSON; it never touches rounds, votes or scores.

    def _practice(self, chat_id) -> dict | None:
        raw = self.store.get(f"practice:{chat_id}")
        return json.loads(raw) if raw else None

    def practice_start(self, now: dt.datetime, chat_id=None) -> tuple[str, list | None]:
        p = self._practice(chat_id)
        if p and not p.get("revealed") and now < dt.datetime.fromisoformat(p["ends"]) + dt.timedelta(minutes=5):
            return "A practice round is already running: tap the buttons above.", None
        if p and now < dt.datetime.fromisoformat(p["ends"]) - dt.timedelta(seconds=PRACTICE_SECONDS) + PRACTICE_COOLDOWN:
            wait = dt.datetime.fromisoformat(p["ends"]) - dt.timedelta(seconds=PRACTICE_SECONDS) + PRACTICE_COOLDOWN - now
            return f"Practice again in {max(1, round(wait.total_seconds() / 60))} min.", None
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
        recent = [t for t in json.loads(self.store.get("practice_ai", "[]"))
                  if now - dt.datetime.fromisoformat(t) < dt.timedelta(hours=1)]
        if len(recent) < PRACTICE_AI_PER_HOUR and self.ai_ready():
            self.store.set("practice_ai", json.dumps(recent + [now.isoformat()]))
            for s in stocks:
                # Same information as the players: no pre-market price (the snapshot's is from a later session).
                got = self.call_ai(s["ticker"], s["name"], {"prev_close": (q.get(s["ticker"]) or {}).get("prev_close")}, [])
                if got:
                    s["ai_call"], s["ai_reason"] = got[0], got[1]
        pid = now.strftime("%H%M%S")
        ends = now + dt.timedelta(seconds=PRACTICE_SECONDS)
        self.store.set(f"practice:{chat_id}", json.dumps({"id": pid, "chat": chat_id, "ends": ends.isoformat(),
                                                          "stocks": stocks, "votes": {}, "names": {}}))
        day = dt.date.fromisoformat(stocks[0]["day"])
        when = "so far today" if stocks[0]["live"] else f"on {day:%a %d %b}"
        lines = [f"🧪 <b>Practice round</b> (not scored) · answers in {PRACTICE_SECONDS // 60} minutes",
                 f"Did each stock finish <b>above ⬆️ or below ⬇️ its opening price</b> {when}? No peeking at charts!",
                 f"{esc(self.bot)} has made its calls too.", ""]
        lines += [f"<b>{esc(s['ticker'])}</b> {esc(s['name'])}" for s in stocks]
        buttons = [[(f"⬆️ {s['ticker']}", f"p|{pid}|{s['ticker']}|U"), (f"⬇️ {s['ticker']}", f"p|{pid}|{s['ticker']}|D")]
                   for s in stocks]
        return "\n".join(lines), buttons

    def practice_vote(self, data: str, user_id: int, name: str, now: dt.datetime, chat_id=None) -> str:
        p = self._practice(chat_id)
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
        self.store.set(f"practice:{chat_id}", json.dumps(p))
        return f"{ticker} {ARROW[call]} saved (practice)."

    def practice_reveal(self, now: dt.datetime) -> None:
        for row in self.store.db.execute("SELECT k FROM kv WHERE k LIKE 'practice:%'").fetchall():
            chat = row["k"].split(":", 1)[1]
            p = self._practice(chat)
            if not p or p.get("revealed") or now < dt.datetime.fromisoformat(p["ends"]):
                continue
            p["revealed"] = True
            self.store.set(row["k"], json.dumps(p))
            self.send(p.get("chat"), self._practice_answers(p), silent=False)

    def _practice_answers(self, p: dict) -> str:
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
        lines.append("The real round posts every US trading day at 20:30 Singapore (19:30 in summer).")
        return "\n".join(lines)
