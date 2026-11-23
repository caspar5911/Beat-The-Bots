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
import re

from . import market
from .config import NY, SG
from .store import BOT_ID, CONF, VOID, Store
from .telegram import Forbidden

# Well-known US brands grouped by industry. A day's stocks come from different industries, because stocks in the same industry
# tend to move together (one piece of chip news moves every chip stock), which would make 5 picks really 1.
SECTORS = {
    "Chips": ["NVDA", "AMD", "INTC"],
    "Software & gadgets": ["AAPL", "MSFT", "ADBE", "ORCL"],
    "Internet & media": ["GOOGL", "META", "NFLX", "DIS", "SPOT"],
    "Travel": ["UBER", "ABNB", "BKNG"],
    "Shopping": ["AMZN", "COST", "WMT", "NKE"],
    "Food & drink": ["KO", "PEP", "MCD", "SBUX"],
    "Banks & payments": ["JPM", "V", "MA", "PYPL"],
    "Cars": ["TSLA", "F", "GM"],
    "Health": ["JNJ", "PFE"],
    "Energy": ["XOM", "CVX"],
    "Planes & machines": ["BA", "GE"],
}
SECTOR = {t: name for name, tickers in SECTORS.items() for t in tickers}
POOL = [t for tickers in SECTORS.values() for t in tickers]   # well-known stocks: practice games

# The daily game plays industry funds (ETFs): each holds many companies, so one company's news matters less.
# Funds that move almost the same share a group, and a day never has two from one group.
ETF_GROUPS = {
    "Tech": ["XLK", "SMH"], "Internet & media": ["XLC"], "Shopping & cars": ["XLY"], "Everyday goods": ["XLP"],
    "Banks": ["XLF"], "Health": ["XLV"], "Oil & gas": ["XLE"], "Factories & planes": ["XLI"],
    "Property": ["XLRE"], "Power & water": ["XLU"], "Materials": ["XLB"],
}
ETF_GROUP = {t: g for g, ts in ETF_GROUPS.items() for t in ts}
ETF_POOL = [t for ts in ETF_GROUPS.values() for t in ts]
ETF_LABEL = {
    "XLK": "US tech companies (Apple, Microsoft, Nvidia)", "SMH": "Chip makers (Nvidia, TSMC, Broadcom)",
    "XLC": "Internet & media (Google, Meta, Netflix)", "XLY": "Shopping & cars (Amazon, Tesla, McDonald's)",
    "XLP": "Everyday goods (Walmart, Costco, Coca-Cola)", "XLF": "Banks & payments (JPMorgan, Visa, Mastercard)",
    "XLV": "Health (Eli Lilly, UnitedHealth, J&J)", "XLE": "Oil & gas (Exxon, Chevron)",
    "XLI": "Factories & planes (GE, Caterpillar, Boeing)", "XLRE": "Property (offices, malls, data centres)",
    "XLU": "Power & water companies", "XLB": "Materials (chemicals, mining)",
}
ETF_NEWS = {  # (news search, words a headline must contain)
    "XLK": ('"tech stocks" OR "technology stocks" OR "tech sector"', ["tech", "technology"]),
    "SMH": ('"chip stocks" OR semiconductor OR "chipmakers"', ["chip", "semiconductor", "Nvidia", "TSMC"]),
    "XLC": ("Alphabet OR Meta OR Netflix stock", ["Google", "Alphabet", "Meta", "Netflix"]),
    "XLY": ('"consumer stocks" OR Amazon OR Tesla stock', ["consumer", "Amazon", "Tesla", "retail"]),
    "XLP": ('"consumer staples" OR Walmart OR Costco OR "Coca-Cola" stock', ["staples", "Walmart", "Costco", "Coca-Cola"]),
    "XLF": ('"bank stocks" OR "financial stocks" OR JPMorgan', ["bank", "financial", "JPMorgan"]),
    "XLV": ('"healthcare stocks" OR "health stocks" OR "pharma stocks"', ["health", "pharma", "drug"]),
    "XLE": ('"oil prices" OR "energy stocks"', ["oil", "energy", "crude", "OPEC"]),
    "XLI": ('"industrial stocks" OR Boeing OR Caterpillar', ["industrial", "Boeing", "Caterpillar"]),
    "XLRE": ('"real estate stocks" OR REIT', ["real estate", "REIT"]),
    "XLU": ('"utility stocks" OR "utilities stocks"', ["utility", "utilities"]),
    "XLB": ('"materials stocks" OR "copper prices" OR "chemical stocks"', ["copper", "chemical", "steel", "materials stocks"]),
}
ETF_ICON = {"Tech": "💻", "Internet & media": "📺", "Shopping & cars": "🛒", "Everyday goods": "🧴", "Banks": "🏦",
            "Health": "💊", "Oil & gas": "🛢️", "Factories & planes": "🏭", "Property": "🏢", "Power & water": "💡",
            "Materials": "⛏️"}
SUGGESTED_MAX = 2                 # most-suggested stocks that join a game
ADDED_MAX = 3                     # stocks players may add to today's game before picks close
SET_MAX = 8                       # lines /set may choose
INDUSTRY_WORDS = {                # words people type for an industry -> its fund
    "chips": "SMH", "chip": "SMH", "semiconductors": "SMH", "tech": "XLK", "technology": "XLK",
    "internet": "XLC", "media": "XLC", "shopping": "XLY", "consumer": "XLY", "cars": "XLY", "everyday": "XLP",
    "staples": "XLP", "food": "XLP", "banks": "XLF", "bank": "XLF", "finance": "XLF", "payments": "XLF",
    "health": "XLV", "healthcare": "XLV", "pharma": "XLV", "oil": "XLE", "energy": "XLE", "gas": "XLE",
    "factories": "XLI", "industrial": "XLI", "planes": "XLI", "property": "XLRE", "realestate": "XLRE",
    "reits": "XLRE", "power": "XLU", "utilities": "XLU", "water": "XLU", "materials": "XLB", "mining": "XLB",
}
STOCK_FUND = {"Chips": "SMH", "Software & gadgets": "XLK", "Internet & media": "XLC", "Travel": "XLY",
              "Shopping": "XLY", "Food & drink": "XLP", "Banks & payments": "XLF", "Cars": "XLY", "Health": "XLV",
              "Energy": "XLE", "Planes & machines": "XLI"}
ADDED = "➕ added by "

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

SURE_AT = 0.7                     # the AI plays "sure" only at this confidence or more
REASONS = {"news": "📰 News", "trend": "📈 Trend", "friends": "👥 Friends", "gut": "🎲 Feeling"}
CONF_LABEL = {"sure": "Very sure", "lean": "Think so"}
ICON = {("UP", "sure"): "⏫", ("UP", "lean"): "⬆️", ("DOWN", "lean"): "⬇️", ("DOWN", "sure"): "⏬"}
OWN_PER_DAY = 3                   # own picks per player per (New York) day
OWN_CUTOFF = dt.timedelta(minutes=30)  # picks this close to the close start at the next open instead
TICKER = re.compile(r"^[A-Z][A-Z.]{0,5}$")
QUIZ_POST = (5, dt.time(10, 0))     # Saturday 10:00 Singapore
QUIZ_RESULTS = (6, dt.time(20, 0))  # Sunday 20:00 Singapore

COMMANDS = [("today", "Today's game"), ("set", "Choose today's lines, e.g. /set chips"),
            ("add", "Add a stock to today's game"),
            ("suggest", "Suggest a stock for the next game"),
            ("pick", "Pick any stock yourself, e.g. /pick NVDA"),
            ("board", "Scores"), ("me", "My score"), ("report", "How good are my choices?"),
            ("bot", "How good is the AI?"), ("practice", "Quick 2-minute game"), ("help", "How to play")]
EXTRAS = {"halfway": "a halfway update around midnight", "quiz": "a 5-question money quiz every weekend"}

ARROW = {"UP": "⬆️", "DOWN": "⬇️"}
DISCLAIMER = "<i>Just a game. Not money advice. No real money.</i>"


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


def pick_tickers(day: dt.date, n: int, pool=ETF_POOL, sector=ETF_GROUP) -> list[str]:
    """Random picks from different industries (the daily game's funds by default)."""
    return pick_stocks(day, n, {}, pool, sector=sector)


def pre_move(q: dict | None, today: dt.date | None = None) -> float | None:
    """Move before the open, against the latest close (stored game lines keep that close as prev_close)."""
    base = market.ref_close(q, today)
    if q and q.get("pre_price") and base:
        return q["pre_price"] / base - 1
    return None


def pick_stocks(day: dt.date, n: int, quotes: dict, pool=ETF_POOL, movers: int = MOVERS, sector=ETF_GROUP) -> list[str]:
    """Up to `movers` of the day's biggest pre-market movers (they have news behind them) plus random others,
    never two from the same industry, in shuffled order."""
    rng = random.Random(day.isoformat())
    picks, used = [], set()
    moved = sorted((t for t in pool if pre_move(quotes.get(t), day) is not None), key=lambda t: -abs(pre_move(quotes[t], day)))
    for t in moved:
        if len(picks) >= min(movers, n):
            break
        if sector.get(t, t) not in used:
            picks.append(t)
            used.add(sector.get(t, t))
    groups: dict[str, list[str]] = {}
    for t in pool:
        groups.setdefault(sector.get(t, t), []).append(t)
    free = [g for g in groups if g not in used]
    rng.shuffle(free)
    for g in free[:n - len(picks)]:
        picks.append(rng.choice(groups[g]))
    if len(picks) < n:  # fewer industries than wanted: allow repeats
        picks += rng.sample([t for t in pool if t not in picks], min(n - len(picks), len(pool) - len(picks)))
    rng.shuffle(picks)
    return picks


def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={int(user_id)}">{esc(name)}</a>'


def _move(s: dict) -> str:
    m = pre_move(s)
    return f"{m:+.1%} before the market opened" if m is not None else "no price before the market opened"


def _sg(t: dt.datetime) -> str:
    return t.astimezone(SG).strftime("%H:%M")


def pts(n: int) -> str:
    return f"{n} pt" if abs(n) == 1 else f"{n} pts"


def points(call: str, result: str | None, double: int = 0, conf: str | None = None) -> int:
    if result not in ("UP", "DOWN"):
        return 0
    if conf in CONF:
        return CONF[conf][0] if call == result else CONF[conf][1]
    return (2 if double else 1) if call == result else (-1 if double else 0)


def icon(call: str, conf: str | None) -> str:
    return ICON.get((call, conf or "lean"), ARROW.get(call, "?"))


def pct(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.0%})" if n else "0/0"


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
                   f"({r['correct']} of {r['played']} right){fire}")
    return out or ["(no scores yet)"]


def fallback_banter(bot: str, today: list[dict]) -> str:
    """The AI's results line when the model is unavailable."""
    humans = [r for r in today if r["user_id"] != BOT_ID]
    me = next((r for r in today if r["user_id"] == BOT_ID), None)
    if not humans:
        return "No humans played today. I win! 😎"
    best, worst = humans[0], humans[-1]
    if me and best["points"] > me["points"]:
        return f"Fine, {best['name']} beat me today. Enjoy it, it won't happen again 😤"
    if len(humans) > 1 and worst["correct"] <= worst["played"] // 2:
        return f"{worst['name']} got {worst['correct']} of {worst['played']} right. A goldfish could do better 🐟"
    return "Another day, another win for the robot 🤖"


LESSONS = [
    "A good choice can still lose, and a bad one can still win. Ask yourself: did I know something, or did I guess?",
    "Most daily moves are random. If you can't say why you'd be right, skipping costs you nothing.",
    "Saying 'very sure' feels good, but it costs a lot when you're wrong. Only use it when you'd bet on it every time.",
    "News everyone has already seen is usually already in the price by the time the market opens.",
    "Write down why you picked before you see the result. Later, your memory will quietly change the reason.",
]


def fallback_lesson(stocks: list[dict], split: dict) -> str:
    """A data-based lesson when the model is unavailable."""
    scored = [s for s in stocks if move_of(s) is not None and s["result"] in ("UP", "DOWN")]
    for s in sorted(scored, key=lambda s: -abs(pre_move(s) or 0)):
        pm = pre_move(s)
        if pm is not None and abs(pm) >= 0.03:
            same = (pm > 0) == (s["result"] == "UP")
            return (f"{s['ticker']} moved {pm:+.1%} before the market opened, then "
                    f"{'kept going the same way' if same else 'turned around'} ({move_of(s):+.1%}) during the day. "
                    "A big early move doesn't tell you much about the rest of the day.")
    for s in scored:
        c = split.get(s["ticker"])
        if c and max(c["UP"], c["DOWN"]) >= 3 and (c["UP"] > c["DOWN"]) != (s["result"] == "UP"):
            return f"Most of the group got {s['ticker']} wrong. Following the crowd feels safe, but crowds are often wrong together."
    day = int(stocks[0]["day"].replace("-", "")) if stocks and stocks[0].get("day") else 0
    return LESSONS[day % len(LESSONS)]


def clean_headline(title: str) -> str:
    """A raw headline without the ' - Publisher' tail; skipped if it's mostly not English letters."""
    t = re.sub(r"\s+-\s+[^-]+$", "", title or "").strip()
    letters = [c for c in t if c.isalpha()]
    if not t or (letters and sum(c.isascii() for c in letters) / len(letters) < 0.8):
        return "No big news today."
    return t


def help_text(title: str, bot: str) -> str:
    return (f"🎯 <b>{esc(title)}</b>\n"
            "Every US trading day you get 5 industries at 20:30 Singapore time (19:30 in summer), like tech, banks or "
            "oil. Each is a fund that holds many companies. Guess if each one ends the day higher or lower than when "
            "the market opens:\n"
            "⏫ / ⏬ <b>very sure</b>: +5 if right, −11 if wrong\n"
            "⬆️ / ⬇️ <b>think so</b>: +3 if right, −4 if wrong\n"
            "Not sure? Don't tap (0 points).\n"
            f"{esc(bot)} (our AI) plays too. Results come the next morning.\n\n"
            "🎛 /set chips banks NVDA: choose exactly what's in today's game (main group only)\n"
            "➕ /add NVDA: add a stock to today's game (anyone can pick it, or skip it)\n"
            "💡 /suggest NVDA: suggest a stock for the next game (the 2 most-suggested join it)\n"
            "📌 /pick NVDA: pick any US stock yourself (3 a day, own score)\n"
            "📊 /report: how good your choices are · /board: scores · /bot: how good the AI is\n"
            "Extras, off unless your group turns them on: /halfway on (midnight update), /quiz on (weekend quiz)\n"
            + DISCLAIMER)


class Game:
    def __init__(self, settings, store: Store, tg, quotes, news, call_ai, ai_ready, banter=None, call_mover=None,
                 cards=None, log=print, clock=None, explain=None, teach=None, summarize=None, history=None):
        self.s, self.store, self.tg = settings, store, tg
        self.quotes, self.news, self.call_ai, self.ai_ready = quotes, news, call_ai, ai_ready
        self.banter, self.call_mover, self.log = banter, call_mover, log
        self.explain, self.teach, self.summarize, self.history = explain, teach, summarize, history
        self.bot_username = ""
        self._live: dict[str, tuple[dt.datetime, dict]] = {}
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
        """Short on purpose: the buttons name each line, tapping a name shows details."""
        lines = [f"🎯 <b>{esc(self.title)}</b> · {day:%a %d %b}",
                 "Higher or lower at the close than at the open? " + self.what_lines(stocks),
                 "⬆️⬇️ +3 / −4 · ⏫⏬ very sure +5 / −11 · not sure: skip"]
        lines += [f"{esc(s['note'])}: <b>{esc(s['ticker'])}</b>" for s in stocks if s.get("note")]
        lines += [f"🔒 Picks close {_sg(lock_at(day))} · 👥 {voters} playing · 🤖 {esc(self.short)} is playing too",
                  "<i>Just a game, not advice. Tap a name for details.</i>"]
        return "\n".join(lines)

    @staticmethod
    def what_lines(stocks: list[dict]) -> str:
        funds = sum(s["ticker"] in ETF_LABEL for s in stocks)
        if funds == len(stocks):
            return "Each line is a whole industry."
        if funds:
            return "Lines are whole industries, except the stocks players added."
        return ""

    def resolve(self, words: list[str]) -> tuple[list[str], list[str]]:
        """Words people type -> tickers: industry words ('chips'), 'NVDA industry', or tickers. (tickers, unknown)."""
        out, bad, i = [], [], 0
        while i < len(words):
            w = words[i].strip(",").lstrip("$")
            nxt = words[i + 1].lower() if i + 1 < len(words) else ""
            if w.lower() in INDUSTRY_WORDS:
                out.append(INDUSTRY_WORDS[w.lower()])
            elif nxt in ("industry", "sector") and w.upper() in SECTOR:
                out.append(STOCK_FUND[SECTOR[w.upper()]])
                i += 1
            elif TICKER.match(w.upper()):
                out.append(w.upper())
            elif w:
                bad.append(w)
            i += 1
        return list(dict.fromkeys(out)), bad

    def set_command(self, words: list[str], user_id: int, name: str, now: dt.datetime, chat_id: int | None) -> str:
        """Choose exactly which lines are in today's game (main group only: every group shares the game)."""
        if self.s.chat_id and str(chat_id) != str(self.s.chat_id):
            return "Only the main group can change today's game."
        ny = now.astimezone(NY)
        today = ny.date()
        day = today if market.is_trading_day(today) and ny < close_at(today) else market.next_trading_day(today)
        key = day.isoformat()
        if not words:
            return ("🎛 Choose what's in today's game: /set and up to 8 industries or stocks, e.g. /set chips, "
                    "/set NVDA, /set NVDA industry or /set chips banks TSLA. Industries: chips, tech, internet, "
                    "shopping, everyday, banks, health, oil, factories, property, power, materials.")
        tickers, bad = self.resolve(words)
        if bad:
            return f"I don't know {esc(', '.join(bad))}. Use industry words like chips or banks, or tickers like NVDA."
        if not tickers or len(tickers) > SET_MAX:
            return f"Choose between 1 and {SET_MAX} industries or stocks."
        try:
            q = self.quotes(tickers)
        except Exception:
            q = {}
        missing = [t for t in tickers if not (q.get(t) or {}).get("last")]
        if missing:
            return f"I can't find {esc(', '.join(missing))}. Check the tickers and try again."
        r = self.store.round(key)
        names = ", ".join(self.label({"ticker": t, "name": (q[t].get("name") or t)}).split(" · ")[0] for t in tickers)
        if r is None:
            self.store.set(f"preset:{key}", json.dumps(tickers))
            return f"🎛 {esc(name)} set the game for {day:%a %d %b}: {esc(names)}. It's posted at {_sg(at(day, POST_AT))}."
        if r["status"] != "open" or now >= lock_at(day):
            return "Today's picks are closed, so today's game can't change any more."
        current = [s["ticker"] for s in self.store.stocks(key)]
        dropped = [t for t in current if t not in tickers]
        lost = sum(1 for v in self.store.votes(key) if v["ticker"] in dropped and v["user_id"] != BOT_ID)
        for t in dropped:
            self.store.remove_stock(key, t)
        for t in tickers:
            if t not in current:
                self.store.add_stock(key, self.make_line(t, q[t], f"🎛 chosen by {name}"))
        if self.ai_ready():
            self.fill_news_lines(key)
        stocks = self.store.stocks(key)
        for chat in self.chat_ids():
            mid = self.store.post_id(key, chat)
            if mid:
                self.edit(chat, mid, self.round_text(day, stocks, self.voters(key, chat)), self.buttons(day, stocks))
        return (f"🎛 {esc(name)} changed today's game to: {esc(names)}. Pick on the game message above."
                + (f" ({lost} pick{'s were' if lost != 1 else ' was'} dropped from removed lines.)" if lost else ""))

    def make_line(self, ticker: str, info: dict, note: str = "") -> dict:
        """A game line for a ticker: name, latest close, early price, headlines and the AI's hidden pick."""
        s = {"ticker": ticker, "name": ETF_LABEL.get(ticker) or info.get("name") or ticker,
             "prev_close": market.ref_close(info), "pre_price": info.get("pre_price"), "ai_call": None,
             "ai_reason": "", "ai_conf": None, "note": note}
        try:
            heads = self.news(ticker, s["name"])
        except Exception:
            heads = []
        if self.ai_ready():
            got = self.call_ai(ticker, s["name"], info, heads)
            if got:
                s["ai_call"], s["ai_reason"], s["ai_conf"] = got
        s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                          for h in heads]
        return s

    def add_command(self, arg: str, user_id: int, name: str, now: dt.datetime, chat_id: int | None):
        """Add a stock to today's open game: it joins the game message; picking it is optional."""
        ticker = arg.upper().lstrip("$")
        ny = now.astimezone(NY)
        key = ny.date().isoformat()
        r = self.store.round(key)
        if not ticker:
            return (f"➕ Add a stock to today's game: type /add and the ticker, like /add NVDA (up to {ADDED_MAX} a day). "
                    "Anyone can pick it, or skip it."), None
        if r is None or r["status"] != "open" or ny >= lock_at(ny.date()):
            if r is None and market.is_trading_day(ny.date()) and ny.time() < POST_AT:
                return self.suggest_command(arg, user_id, name, now) + "\n(Today's game isn't out yet, so this counts as a suggestion for it.)", None
            return ("Today's picks are closed, so I can't add to today's game. "
                    + self.suggest_command(arg, user_id, name, now)), None
        if not TICKER.match(ticker):
            return f"'{esc(arg)}' doesn't look like a US stock ticker. Try something like /add AAPL.", None
        stocks = self.store.stocks(key)
        if ticker in {s["ticker"] for s in stocks}:
            return f"{esc(ticker)} is already in today's game. Tap its buttons in the game message.", None
        if sum((s.get("note") or "").startswith(ADDED) for s in stocks) >= ADDED_MAX:
            return f"Today's game already has {ADDED_MAX} added stocks. Try /suggest {esc(ticker)} for the next game.", None
        try:
            info = self.quotes([ticker]).get(ticker)
        except Exception:
            info = None
        if not info or not info.get("last"):
            return f"I can't find {esc(ticker)}. Check the ticker (like AAPL or NVDA) and try again.", None
        s = {"ticker": ticker, "name": ETF_LABEL.get(ticker) or info.get("name") or ticker,
             "prev_close": market.ref_close(info, ny.date()), "pre_price": info.get("pre_price"), "ai_call": None, "ai_reason": "",
             "ai_conf": None, "note": f"{ADDED}{name}"}
        try:
            heads = self.news(ticker, s["name"])
        except Exception:
            heads = []
        if self.ai_ready():
            got = self.call_ai(ticker, s["name"], info, heads)
            if got:
                s["ai_call"], s["ai_reason"], s["ai_conf"] = got
        s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                          for h in heads]
        self.store.player(user_id, name)
        self.store.add_stock(key, s)
        if self.ai_ready():
            self.fill_news_lines(key)
        day, stocks = ny.date(), self.store.stocks(key)
        for chat in self.chat_ids():
            mid = self.store.post_id(key, chat)
            if mid:
                self.edit(chat, mid, self.round_text(day, stocks, self.voters(key, chat)), self.buttons(day, stocks))
        k = day.strftime("%Y%m%d")
        row = [[(self.label(s), f"i|{k}|{ticker}"), ("📊 More", f"x|{k}|{ticker}")], self.choice_row(k, ticker)]
        return (f"➕ {esc(name)} added <b>{esc(ticker)}</b> ({esc(s['name'])}) to today's game · {_move(s)}.\n"
                f"Got an opinion? Tap below (or in the game message). Not sure? Skip it, no points lost. "
                f"Picks close at {_sg(lock_at(day))} Singapore time."), row

    @staticmethod
    def label(s: dict) -> str:
        """Button title for a game line: industry name for funds, company name for stocks."""
        t = s["ticker"]
        if t in ETF_LABEL:
            short = {"XLK": "Tech", "SMH": "Chip makers", "XLU": "Power & water"}
            icon_ = "🔌" if t == "SMH" else ETF_ICON.get(ETF_GROUP.get(t, ""), "📊")
            name = f"{icon_} {short.get(t) or ETF_LABEL[t].split(' (')[0]} · {t}"
        else:
            name = f"📈 {(s.get('name') or t)[:22]} · {t}"
        m = pre_move(s)
        return name + (f" {m:+.1%}" if m is not None else "")

    @staticmethod
    def choice_row(key: str, t: str) -> list:
        return [("⏫ Sure", f"v|{key}|{t}|U|s"), ("⬆️ Up", f"v|{key}|{t}|U|l"),
                ("⬇️ Down", f"v|{key}|{t}|D|l"), ("⏬ Sure", f"v|{key}|{t}|D|s")]

    @classmethod
    def buttons(cls, day: dt.date, stocks: list[dict]) -> list:
        key = day.strftime("%Y%m%d")
        rows = []
        for s in stocks:
            rows.append([(cls.label(s), f"i|{key}|{s['ticker']}"), ("📊 More", f"x|{key}|{s['ticker']}")])
            rows.append(cls.choice_row(key, s["ticker"]))
        rows.append([("👀 My picks", f"k|{key}")])
        return rows

    def live(self, tickers: list[str], now: dt.datetime | None = None, ttl: int = 60) -> dict:
        """Quotes cached for a minute, so many taps on a name don't flood the price feed."""
        now = now or dt.datetime.now(dt.timezone.utc)
        need = [t for t in tickers if t not in self._live or (now - self._live[t][0]).total_seconds() > ttl]
        if need:
            try:
                got = self.quotes(need)
            except Exception as e:
                self.log(f"info quotes failed: {e!r}"[:300])
                got = {}
            for t in need:
                if t in got:
                    self._live[t] = (now, got[t])
        return {t: self._live[t][1] for t in tickers if t in self._live}

    def on_info(self, data: str, now: dt.datetime | None = None) -> str:
        """Private pop-up (max 200 characters) about one line of the game: what it is, its move before the open
        against the whole market, yesterday and where it sits in its year, and a neutral one-line news summary."""
        try:
            _, d, ticker = data.split("|")
            day = dt.datetime.strptime(d, "%Y%m%d").date()
        except ValueError:
            return "Unknown button."
        s = next((s for s in self.store.stocks(day.isoformat()) if s["ticker"] == ticker), None)
        if not s:
            return "That game is over."
        now = now or dt.datetime.now(dt.timezone.utc)
        q = self.live([ticker, "SPY"], now)
        info, spy = q.get(ticker) or {}, q.get("SPY") or {}
        lines = [self.label(s).rsplit(" · ", 1)[0] + f" ({ticker})"]
        if ticker in ETF_LABEL and " (" in ETF_LABEL[ticker]:
            lines.append("Inside: " + ETF_LABEL[ticker].split(" (", 1)[1].rstrip(")"))
        elif ticker in SECTOR:
            lines.append(f"Industry: {SECTOR[ticker]}")
        ny = now.astimezone(NY)
        traded_today = bool(info.get("update_time")) and info["update_time"].date() == ny.date()
        if traded_today and info.get("last") and info.get("prev_close"):
            m, mk = info["last"] / info["prev_close"] - 1, (spy["last"] / spy["prev_close"] - 1
                                                            if spy.get("last") and spy.get("prev_close") else None)
            lines.append(f"Today so far: {m:+.1%}" + (f" (whole market {mk:+.1%})" if mk is not None else ""))
        else:
            m = pre_move(info, ny.date()) if info else pre_move(s)
            mk = pre_move(spy, ny.date())
            if m is not None:
                lines.append(f"Before the open: {m:+.1%}" + (f" (whole market {mk:+.1%})" if mk is not None else ""))
            if info.get("last") and info.get("prev_close"):
                y = info["last"] / info["prev_close"] - 1
                lines.append(f"Yesterday: {y:+.1%}" + self.year_position(info))
        news = s.get("news_line") or clean_headline(s["headlines"][0]["title"] if s["headlines"] else "")
        text = "\n".join(lines)
        room = 200 - len(text) - len("\nNews: ")
        return text + (f"\nNews: {news[:room - 1] + '…' if len(news) > room else news}" if room > 20 else "")

    def details(self, key: str, ticker: str, now: dt.datetime | None = None) -> tuple[str, bytes | None] | None:
        """The 'More' details for one line of a game: (HTML text, chart PNG or None). Sent in a private chat."""
        s = next((s for s in self.store.stocks(key) if s["ticker"] == ticker), None)
        if not s:
            return None
        now = now or dt.datetime.now(dt.timezone.utc)
        ny = now.astimezone(NY)
        lines = [f"📊 <b>{esc(self.label(s).rsplit(' · ', 1)[0])} · {esc(ticker)}</b>"]
        if ticker in ETF_LABEL and " (" in ETF_LABEL[ticker]:
            lines.append("Inside: " + esc(ETF_LABEL[ticker].split(" (", 1)[1].rstrip(")")) + " and many more")
        q = self.live([ticker, "SPY"], now)
        info, spy = q.get(ticker) or {}, q.get("SPY") or {}
        m, mk = pre_move(info, ny.date()) if info else pre_move(s), pre_move(spy, ny.date())
        if m is not None:
            lines.append(f"Before the open: {m:+.1%}" + (f" (whole market {mk:+.1%})" if mk is not None else ""))
        rows = []
        if self.history:
            try:
                rows = [r for r in self.history(ticker) if r["date"] < ny.date()]
            except Exception as e:
                self.log(f"history failed: {e!r}"[:300])
        last20 = rows[-20:]
        if len(last20) >= 10:
            day_moves = [r["close"] / r["open"] - 1 for r in last20]
            usual = sum(abs(x) for x in day_moves) / len(day_moves)
            ups = sum(x > 0 for x in day_moves)
            lines.append(f"A normal day (open to close): about ±{usual:.1%}")
            lines.append(f"Last {len(last20)} days: ended higher than it opened <b>{ups}</b> times, lower "
                         f"{len(last20) - ups} times")
            lines.append("Last 5 days: " + ", ".join(f"{x:+.1%}" for x in day_moves[-5:]))
            month = rows[-22:]
            lines.append(f"Past month: {month[-1]['close'] / month[0]['close'] - 1:+.1%}" + self.year_position(info))
        news = s.get("news_line") or ""
        if news:
            lines.append(f"\nNews: {esc(news)}")
        heads = [clean_headline(h["title"]) for h in s["headlines"][:3]]
        heads = [h for h in heads if h != "No big news today."]
        lines += [f"• {esc(h[:110])}" for h in heads[:2]]
        lines.append("\n<i>Tip: start from how often it usually ends higher, then ask if today's news really "
                     "changes that. Just a game, not advice.</i>")
        chart = None
        if len(rows) >= 5:
            try:
                chart = self.cards.price_chart(f"{ticker} · past month", rows[-22:])
            except Exception as e:
                self.log(f"chart failed: {e!r}"[:300])
        return "\n".join(lines), chart

    def send_details(self, chat_id: int, key: str, ticker: str) -> bool:
        """Raises Forbidden when the player has never opened a private chat with the bot."""
        got = self.details(key, ticker)
        if not got:
            self.tg.send("That game is over.", None, chat_id=chat_id)
            return True
        text, chart = got
        if chart and len(text) <= 1024:
            self.tg.send_photo(chart, text, chat_id=chat_id, html=True)
        else:
            if chart:
                self.tg.send_photo(chart, "", chat_id=chat_id)
            self.tg.send(text, None, chat_id=chat_id)
        return True

    def more_link(self, data: str) -> str | None:
        """A t.me link that opens a private chat with the bot and asks for these details on Start."""
        try:
            _, d, ticker = data.split("|")
        except ValueError:
            return None
        return f"https://t.me/{self.bot_username}?start=more_{d}_{ticker}" if self.bot_username else None

    def on_private_start(self, payload: str, chat_id: int) -> bool:
        """/start more_YYYYMMDD_TICKER in a private chat (from the More button's link)."""
        if not payload.startswith("more_"):
            return False
        try:
            _, d, ticker = payload.split("_", 2)
            key = dt.datetime.strptime(d, "%Y%m%d").date().isoformat()
        except ValueError:
            return False
        self.send_details(chat_id, key, ticker.upper())
        return True

    @staticmethod
    def year_position(info: dict) -> str:
        hi, lo, last = info.get("high52"), info.get("low52"), info.get("last")
        if not (hi and lo and last) or hi <= lo:
            return ""
        if last >= hi * 0.97:
            return " · near its highest this year"
        if last <= lo * 1.03:
            return " · near its lowest this year"
        return f" · {1 - last / hi:.0%} below its 1-year high"

    def fill_news_lines(self, key: str) -> None:
        """A neutral one-line news summary per line of an open game (Ah Huat writes it; the pick stays hidden)."""
        if not self.summarize:
            return
        for s in self.store.stocks(key):
            if s.get("news_line"):
                continue
            try:
                line = self.summarize(s["name"], [h["title"] for h in s["headlines"]])
            except Exception as e:
                self.log(f"news line failed: {e!r}"[:300])
                line = None
            if line:
                self.store.update_stock(key, s["ticker"], news_line=line)

    def split(self, key: str, chat_id: int | None) -> dict[str, dict[str, int]]:
        """{ticker: {"UP": n, "DOWN": n}} among the group's human players."""
        members = self.store.members(chat_id) if chat_id is not None else None
        out: dict[str, dict[str, int]] = {}
        for v in self.store.votes(key):
            if v["user_id"] != BOT_ID and (members is None or v["user_id"] in members):
                out.setdefault(v["ticker"], {"UP": 0, "DOWN": 0})[v["call"]] += 1
        return out

    def crowd(self, key: str, chat_id: int | None) -> dict[str, str]:
        """The group's majority call per stock (ties: no call)."""
        return {t: ("UP" if c["UP"] > c["DOWN"] else "DOWN") for t, c in self.split(key, chat_id).items()
                if c["UP"] != c["DOWN"]}

    def lock_text(self, day: dt.date, stocks: list[dict], voters: int, chat_id: int | None = None) -> str:
        key = day.isoformat()
        split = self.split(key, chat_id)
        lines = [f"🔒 <b>Picks closed</b> · {day:%a %d %b} · {voters} player{'s' if voters != 1 else ''}",
                 f"What the group picked, and {esc(self.bot)}'s picks:"]
        for s in stocks:
            c = split.get(s["ticker"], {"UP": 0, "DOWN": 0})
            crowd = f"⬆️ {c['UP']} · ⬇️ {c['DOWN']}"
            if s["ai_call"]:
                conf = "sure" if (s["ai_conf"] or 0) >= SURE_AT else "lean"
                lines.append(f"<b>{esc(s['ticker'])}</b> group {crowd} · {esc(self.short)} {icon(s['ai_call'], conf)} "
                             f"{CONF_LABEL[conf].lower()} ({(s['ai_conf'] or 0.5):.0%} sure): "
                             f"{esc(s['ai_reason'] or '(no comment)')}")
            else:
                lines.append(f"<b>{esc(s['ticker'])}</b> group {crowd} · {esc(self.short)} 💤 didn't play (AI not working)")
        mid = at(day, MIDGAME)
        if mid <= close_at(day) - MIDGAME_LATEST and chat_id is not None and self.extra_on("halfway", chat_id):
            lines.append(f"Halfway update at {_sg(mid)}, results at {_sg(close_at(day) + SETTLE_DELAY)} (Singapore time).")
        else:
            lines.append(f"Results at {_sg(close_at(day) + SETTLE_DELAY)} Singapore time.")
        return "\n".join(lines)

    def results_text(self, day: dt.date, chat_id: int | None = None, banter: str = "") -> str:
        key = day.isoformat()
        stocks, votes = self.store.stocks(key), self.store.votes(key)
        members = self.store.members(chat_id) if chat_id is not None else None
        ours = (lambda uid: uid != BOT_ID and (members is None or uid in members))
        crowd = self.crowd(key, chat_id)
        lines = [f"🏁 <b>Results</b> · {day:%a %d %b}"]
        contrarians = []
        for s in stocks:
            if s["result"] in ("UP", "DOWN"):
                mine = [v for v in votes if v["ticker"] == s["ticker"] and ours(v["user_id"])]
                right = [v for v in mine if v["call"] == s["result"]]
                ai = "" if not s["ai_call"] else (f" · {esc(self.short)} " + ("✅" if s["ai_call"] == s["result"] else "❌"))
                lines.append(f"{ARROW[s['result']]} <b>{esc(s['ticker'])}</b> {s['open']:.2f} → {s['close']:.2f} "
                             f"({move_of(s):+.1%}) · "
                             + (f"{len(right)} of {len(mine)} got it right" if mine else "nobody picked") + ai)
                if mine and 0 < len(right) <= len(mine) / 3:
                    contrarians.append(f"{', '.join(esc(v['name']) for v in right)} on {esc(s['ticker'])}")
            else:
                why = "price didn't change" if s["result"] == "FLAT" else "price not available"
                lines.append(f"➖ <b>{esc(s['ticker'])}</b>: no result ({why}), no points")
        r = self.store.round(key)
        mkt = self.market_move(key)
        if mkt is not None:
            moved = [s for s in stocks if s["result"] in ("UP", "DOWN")]
            same = sum((s["result"] == "UP") == (mkt > 0) for s in moved)
            lines.append(f"🌍 The whole US market (SPY) went {mkt:+.1%} today."
                         + (" Most of today's picks went the same way." if moved and same > len(moved) / 2 else ""))
        scored = [s for s in stocks if s["result"] in ("UP", "DOWN") and s["ai_call"]]
        group_right = sum(crowd.get(s["ticker"]) == s["result"] for s in scored)
        ai_right = sum(s["ai_call"] == s["result"] for s in scored)
        if crowd and scored:
            verdict = ("the group wins! 🎉" if group_right > ai_right else f"{esc(self.short)} wins 🤖"
                       if ai_right > group_right else "a draw 🤝")
            lines.append(f"👥 The group together got {group_right} of {len(scored)} right, {esc(self.short)} got "
                         f"{ai_right} of {len(scored)}: {verdict}")
        if contrarians:
            lines.append("🦅 Right when most people were wrong: " + "; ".join(contrarians))
        today = self.store.scores(key, key, chat_id)
        streaks = self.store.streaks()
        if today:
            bot = next((r for r in today if r["user_id"] == BOT_ID), None)
            humans = [r for r in today if r["user_id"] != BOT_ID]
            lines += ["", "<b>Today's scores</b>", *board_lines(today, streaks=streaks)]
            if bot and humans:
                beat = [esc(r["name"]) for r in humans if r["points"] > bot["points"]]
                lines.append(f"🏆 Beat {esc(self.short)} today: " + ", ".join(beat) if beat
                             else f"🤖 Nobody beat {esc(self.short)} today.")
        if r and r["lesson"]:
            lines += ["", f"📚 <b>Tip:</b> {esc(r['lesson'])}"]
        if banter:
            lines += ["", f"💬 <b>{esc(self.bot)}:</b> {esc(banter)}"]
        ws = week_start(day).isoformat()
        lines += ["", "<b>This week</b>", *board_lines(self.store.scores(ws, key, chat_id), streaks=streaks)]
        if last_of_week(day):
            g, a, n = self.group_vs_ai(ws, key, chat_id)
            if n:
                lines.append(f"👥 This week the group together got {g} of {n} right, {esc(self.short)} got {a} of {n}")
            lines += ["", "<b>This week's awards</b>", *self.titles(ws, key, chat_id)]
            lines += self.league_lines(ws, key, chat_id)
        lines.append("Tap ❓ below to see why each stock went up or down.")
        return "\n".join(lines)

    def group_vs_ai(self, since: str, until: str, chat_id: int | None) -> tuple[int, int, int]:
        """Same count as the daily line: every scored stock the AI played, on days the group played; a tied group
        vote counts as not right."""
        g = a = n = 0
        for row in self.store.db.execute("SELECT day FROM rounds WHERE status='settled' AND day BETWEEN ? AND ?",
                                         (since, until)).fetchall():
            crowd = self.crowd(row["day"], chat_id)
            if not crowd:
                continue
            for s in self.store.stocks(row["day"]):
                if s["result"] in ("UP", "DOWN") and s["ai_call"]:
                    n += 1
                    g += crowd.get(s["ticker"]) == s["result"]
                    a += s["ai_call"] == s["result"]
        return g, a, n

    @staticmethod
    def why_buttons(day: dt.date, stocks: list[dict]) -> list:
        key = day.strftime("%Y%m%d")
        return [[(f"❓ {s['ticker']}", f"w|{key}|{s['ticker']}") for s in stocks]]

    def titles(self, since: str, until: str, chat_id: int | None = None) -> list[str]:
        rows = self.store.scores(since, until, chat_id)
        bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
        humans = [r for r in rows if r["user_id"] != BOT_ID and r["played"] >= MIN_TITLE_PICKS]
        if not humans:
            return [f"(nobody made {MIN_TITLE_PICKS} or more picks)"]
        top, out = humans[0], []
        if not bot or top["points"] > bot["points"]:
            out.append(f"👑 Best player, and beat {esc(self.short)}: <b>{esc(top['name'])}</b> ({pts(top['points'])})")
        elif top["points"] == bot["points"]:
            out.append(f"👑 Best player: <b>{esc(top['name'])}</b> ({pts(top['points'])}), tied with {esc(self.short)} 😤")
        else:
            out.append(f"👑 Best player: <b>{esc(top['name'])}</b> ({pts(top['points'])}), "
                       f"but {esc(self.short)} did better with {pts(bot['points'])} 🤖")
        if len(humans) > 1:
            low = humans[-1]
            out.append(f"🤡 Last place: <b>{esc(low['name'])}</b> ({pts(low['points'])})")
        return out

    def league_lines(self, since: str, until: str, chat_id: int | None) -> list[str]:
        table = self.store.league(since, until)
        if len(table) < 2:
            return []
        out = ["", "<b>🏟 Groups this week</b> (average score per player)"]
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
        self.quiz_stage(now)
        self.own_stage(ny)

    def _due(self, what: str, key: str, ny: dt.datetime, gap: dt.timedelta = RETRY) -> bool:
        """Space out retries."""
        last = self._tried.get((what, key))
        if last and ny - last < gap:
            return False
        self._tried[(what, key)] = ny
        return True

    def create(self, day: dt.date, now: dt.datetime) -> None:
        preset = json.loads(self.store.get(f"preset:{day.isoformat()}", "[]"))
        ideas = [] if preset else self.store.suggested(day.isoformat())
        try:
            q = self.quotes(list(dict.fromkeys(ETF_POOL + [i["ticker"] for i in ideas])))
        except Exception as e:  # quotes are context only at this stage
            self.log(f"pre-market quotes failed: {e!r}"[:300])
            q = {}
        chosen = [i for i in ideas if (q.get(i["ticker"]) or {}).get("last")][:SUGGESTED_MAX]
        notes = {i["ticker"]: f"💡 suggested by {i['names']}" for i in chosen}
        n = self.s.stocks_per_day - len(chosen)
        funds = [t for t in (pick_stocks(day, n + len(chosen), q) if q else pick_tickers(day, n + len(chosen)))
                 if t not in notes][:n]
        tickers = [i["ticker"] for i in chosen] + funds
        if preset:
            try:
                pq = self.quotes(preset)
            except Exception:
                pq = {}
            q.update(pq)
            tickers = [t for t in preset if (pq.get(t) or {}).get("last")] or tickers
        ready = self.ai_ready()
        if not ready:
            self.log("model unavailable: the AI sits today out")
        stocks = []
        for t in tickers:
            info = q.get(t, {})
            s = {"ticker": t, "name": ETF_LABEL.get(t) or info.get("name") or t, "prev_close": market.ref_close(info, day),
                 "pre_price": info.get("pre_price"), "ai_call": None, "ai_reason": "", "ai_conf": None,
                 "note": notes.get(t, "")}
            s["headlines"] = [{"title": h["title"], "published": h["published"]} for h in self.news(t, s["name"])]
            if ready and self.clock() < at(day, AI_DEADLINE):
                got = self.call_ai(t, s["name"], info, s["headlines"])
                if got:
                    s["ai_call"], s["ai_reason"], s["ai_conf"] = got
            stocks.append(s)
        ai_mover = None
        for s in stocks:  # store JSON-safe headlines
            s["headlines"] = [{"title": h["title"], "published": h["published"].isoformat() if h.get("published") else None}
                              for h in s["headlines"]]
        self.store.add_round(day.isoformat(), stocks, now.isoformat(), ai_mover)
        if ready:
            self.fill_news_lines(day.isoformat())
        for chat in self.chat_ids():
            self._tried[("post", f"{day.isoformat()}:{chat}")] = now.astimezone(NY)
            self.post(day, chat)

    def refresh_open_rounds(self) -> None:
        """On start: picks in a still-open round become Lean picks and its posts get the current text and buttons
        (so a rules change applies to tonight's round, not tomorrow's)."""
        for r in self.store.db.execute("SELECT day FROM rounds WHERE status='open'").fetchall():
            self.store.db.execute("UPDATE votes SET conf='lean', double=0 WHERE day=? AND conf IS NULL", (r["day"],))
            self.store.db.commit()
            day = dt.date.fromisoformat(r["day"])
            if dt.datetime.now(dt.timezone.utc) < lock_at(day):  # before the open: latest close and early price
                try:
                    q = self.quotes([s["ticker"] for s in self.store.stocks(r["day"])])
                    for t, info in q.items():
                        if market.ref_close(info, day):
                            self.store.update_stock(r["day"], t, prev_close=market.ref_close(info, day),
                                                    pre_price=info.get("pre_price"))
                except Exception as e:
                    self.log(f"open round price refresh failed: {e!r}"[:300])
            if self.ai_ready():
                self.fill_news_lines(r["day"])
            stocks = self.store.stocks(r["day"])
            for chat in self.chat_ids():
                mid = self.store.post_id(r["day"], chat)
                if mid:
                    self.edit(chat, mid, self.round_text(day, stocks, self.voters(r["day"], chat)), self.buttons(day, stocks))

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
        core = [s["ticker"] for s in self.store.stocks(key) if not (s.get("note") or "").startswith(ADDED)]
        n = len(core)
        since = (day - dt.timedelta(days=ACTIVE_DAYS)).isoformat()
        rows = self.store.db.execute(
            "SELECT p.user_id, p.name, (SELECT COUNT(*) FROM votes t WHERE t.day=? AND t.user_id=p.user_id "
            f"AND t.ticker IN ({','.join('?' * len(core)) or "''"})) AS today "
            "FROM players p JOIN members m ON m.user_id=p.user_id AND m.chat_id=? WHERE p.user_id != ? AND EXISTS "
            "(SELECT 1 FROM votes v WHERE v.user_id=p.user_id AND v.day >= ? AND v.day < ?) ORDER BY p.name",
            (key, *core, chat_id, BOT_ID, since, key)).fetchall()
        missing = [r for r in rows if r["today"] < n]
        text = "⏰ <b>10 minutes left to pick!</b> "
        if missing:
            text += ", ".join(mention(r["user_id"], r["name"]) + (f" ({n - r['today']} left)" if r["today"] else "")
                              for r in missing) + ": make your picks."
        else:
            text += f"{self.voters(key, chat_id)} player(s) so far. Last chance to beat {esc(self.bot)}."
        self.store.set(f"reminded:{key}:{chat_id}", "1")
        self.send(chat_id, text)

    def lock(self, day: dt.date) -> None:
        key = day.isoformat()
        stocks = self.store.stocks(key)
        for s in stocks:
            if s["ai_call"]:
                conf = "sure" if (s["ai_conf"] or 0) >= SURE_AT else "lean"
                self.store.vote(key, s["ticker"], BOT_ID, s["ai_call"], lock_at(day).isoformat(), conf)
        r = self.store.round(key)
        if r["ai_mover"]:
            self.store.set_bonus(key, BOT_ID, r["ai_mover"], lock_at(day).isoformat())
        self.store.set_status(key, "locked")
        stocks = self.store.stocks(key)
        for chat in self.chat_ids():
            mid = self.store.post_id(key, chat)
            if mid:
                self.edit(chat, mid, self.round_text(day, stocks, self.voters(key, chat)) + "\n🔒 <b>Locked.</b>", None)
                self.send(chat, self.lock_text(day, stocks, self.voters(key, chat), chat))

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
        if (ny >= close_at(day) - MIDGAME_LATEST or self.store.round(key)["status"] != "locked"
                or not any(self.extra_on("halfway", c) for c in self.chat_ids())):
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
        head = [f"⏱ <b>Halfway update</b> · {day:%a %d %b} (prices right now, not final)"]
        for s in stocks:
            info = q.get(s["ticker"]) or {}
            today = info.get("update_time") and info["update_time"].date() == day
            o = s["open"] or (info.get("open") if today else None)
            last = info.get("last") if today else None
            if o and last:
                res = "UP" if last > o else "DOWN" if last < o else None
                now_result[s["ticker"]], moves[s["ticker"]] = res, last / o - 1
                head.append(f"{ARROW.get(res, '➖')} <b>{esc(s['ticker'])}</b> {last / o - 1:+.1%} since the market opened")
            else:
                head.append(f"➖ <b>{esc(s['ticker'])}</b>: no price yet")
        lead = None
        totals: dict[int, list] = {}
        for v in self.store.votes(key):
            t = totals.setdefault(v["user_id"], [v["name"], 0])
            t[1] += points(v["call"], now_result.get(v["ticker"]), v["double"], v.get("conf"))
        for b in self.store.bonus(key):
            t = totals.setdefault(b["user_id"], [b["name"], 0])
            t[1] += 2 if lead and b["pick"] == lead else 0
        for chat in self.chat_ids():
            if not self.store.post_id(key, chat) or not self.extra_on("halfway", chat):
                continue
            members = self.store.members(chat)
            mine = {uid: v for uid, v in totals.items() if uid == BOT_ID or uid in members}
            lines = list(head)
            crowd = self.crowd(key, chat)
            if crowd and now_result:
                g = sum(crowd.get(t) == res for t, res in now_result.items() if res)
                a = sum(s["ai_call"] == now_result.get(s["ticker"]) for s in stocks if s["ai_call"] and now_result.get(s["ticker"]))
                lines.append(f"👥 So far the group together has {g} right, {esc(self.short)} has {a}")
            ranked = sorted(mine.items(), key=lambda kv: -kv[1][1])
            if ranked:
                lines += ["", "<b>Scores if the day ended now</b>"] + [f"{esc(n)}: {pts(p)}" for _, (n, p) in ranked[:10]]
                bot = mine.get(BOT_ID)
                humans = [p for uid, (_, p) in mine.items() if uid != BOT_ID]
                if bot and humans:
                    lines.append(f"🤖 {esc(self.short)} is losing 😬" if max(humans) > bot[1]
                                 else f"🤖 {esc(self.short)} is winning 😎" if max(humans) < bot[1]
                                 else f"🤖 It's a tie with {esc(self.short)} 😤")
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
        self.record_market(day)
        self.explain_day(day, stocks)
        self.store.set_status(key, "settled")
        ws = week_start(day).isoformat()
        for i, chat in enumerate(self.chat_ids()):
            if not self.store.post_id(key, chat):
                continue
            self.send(chat, self.results_text(day, chat, self._banter(day, chat, model=i < BANTER_MODEL_GROUPS)),
                      self.why_buttons(day, stocks))
            if last_of_week(day):
                self.send_week_card(chat, ws, key)
                report = self.group_report(chat, ws, key)
                if report:
                    self.send(chat, report)

    def explain_day(self, day: dt.date, stocks: list[dict]) -> None:
        """Once per day: why each stock moved (fresh headlines) and the lesson. Both have fallbacks."""
        key = day.isoformat()
        scored = [s for s in stocks if move_of(s) is not None]
        why = {}
        if self.explain and scored:
            try:
                rows = [{"ticker": s["ticker"], "name": s["name"], "move": move_of(s),
                         "headlines": [h["title"] for h in self.news(s["ticker"], s["name"])]} for s in scored]
                why = self.explain(rows) or {}
            except Exception as e:
                self.log(f"why failed: {e!r}"[:300])
        for s in stocks:
            text = why.get(s["ticker"]) or (
                f"{s['ticker']} went {move_of(s):+.1%} today and there was no clear news about it. Most daily moves "
                "like this are random or follow the whole market." if move_of(s) is not None
                else "No price, so no result today.")
            self.store.update_stock(key, s["ticker"], why=text)
        lesson = None
        if self.teach and scored:
            split = self.split(key, None)
            try:
                lesson = self.teach({"stocks": [{"ticker": s["ticker"], "pre_market_move": pre_move(s),
                                                 "open_to_close": round(move_of(s), 4),
                                                 "group_up": split.get(s["ticker"], {}).get("UP", 0),
                                                 "group_down": split.get(s["ticker"], {}).get("DOWN", 0)}
                                                for s in scored]})
            except Exception as e:
                self.log(f"lesson failed: {e!r}"[:300])
        self.store.set_round(key, lesson=lesson or fallback_lesson(stocks, self.split(key, None)))

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
            self.send(chat_id, f"Sorry, {esc(self.title)} is full right now ({self.max_groups} groups max).")
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
            return None, "That game is over."
        if r["status"] != "open" or now >= lock_at(day):
            return None, "Too late: picks are closed."
        return (day, key, ticker, parts), ""

    def on_button(self, data: str, user_id: int, name: str, now: dt.datetime, chat_id: int | None = None) -> str:
        if chat_id is not None:
            self.store.player(user_id, name)
            self.store.member(chat_id, user_id)
        if data.startswith("w|"):
            return self.on_why(data)
        if data.startswith("k|"):
            return self.my_picks(data, user_id)
        if data.startswith("i|"):
            return self.on_info(data, now)
        if data.startswith("p|"):
            return self.practice_vote(data, user_id, name, now, chat_id)
        if data.startswith("o|"):
            return self.on_own(data, user_id, name, now, chat_id)
        if data.startswith("m|"):
            return self.on_mover(data, user_id, name, now)
        if data.startswith("r|"):
            return self.on_reason(data, user_id, name, now)
        return self.on_vote(data, user_id, name, now)

    @staticmethod
    def is_alert(data: str) -> bool:
        """Answers shown as a pop-up the player must close (longer text)."""
        return data.startswith(("w|", "k|", "i|"))

    def on_why(self, data: str) -> str:
        try:
            _, d, ticker = data.split("|")
            key = dt.datetime.strptime(d, "%Y%m%d").date().isoformat()
        except ValueError:
            return "Unknown button."
        s = next((s for s in self.store.stocks(key) if s["ticker"] == ticker), None)
        return (s and s.get("why")) or "No explanation for this one yet."

    def on_reason(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        parts = data.split("|")
        if len(parts) != 3 or parts[2] not in REASONS:
            return "Unknown button."
        try:
            day = dt.datetime.strptime(parts[1], "%Y%m%d").date()
        except ValueError:
            return "Unknown button."
        r = self.store.round(day.isoformat())
        if r is None:
            return "That game is over."
        if r["status"] != "open" or now >= lock_at(day):
            return "Too late: picks are closed."
        self.store.player(user_id, name)
        self.store.set_reason(day.isoformat(), user_id, parts[2])
        return f"Got it: today you picked mainly by {REASONS[parts[2]]}. On Friday you'll see which reason works best for you."

    def on_vote(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        got, err = self._open_round(data, now)
        if not got:
            return err
        day, key, ticker, parts = got
        c = parts[3] if len(parts) > 3 else ""
        conf = {"s": "sure", "l": "lean"}.get(parts[4] if len(parts) > 4 else "l")
        if c not in ("U", "D") or not conf:
            return "Unknown button."
        call = "UP" if c == "U" else "DOWN"
        self.store.player(user_id, name)
        mine = next((v for v in self.store.votes(key) if v["user_id"] == user_id and v["ticker"] == ticker), None)
        if mine and mine["call"] == call and mine.get("conf") == conf:
            self.store.unvote(key, ticker, user_id)
            return f"{ticker}: pick cancelled. Your picks: {self.picks_line(key, user_id)}"
        self.store.vote(key, ticker, user_id, call, now.isoformat(), conf)
        win, lose = CONF[conf]
        return (f"✅ {ticker} {icon(call, conf)} {CONF_LABEL[conf].lower()} {'up' if call == 'UP' else 'down'} "
                f"(+{win} / {lose}). Your picks: {self.picks_line(key, user_id)}")

    def picks_line(self, key: str, user_id: int) -> str:
        mine = {v["ticker"]: v for v in self.store.votes(key) if v["user_id"] == user_id}
        return " · ".join(f"{s['ticker']} " + (icon(mine[s['ticker']]["call"], mine[s['ticker']].get("conf"))
                                               if s["ticker"] in mine else "–") for s in self.store.stocks(key))

    def my_picks(self, data: str, user_id: int) -> str:
        """Private pop-up with the player's picks for that round (and results once known)."""
        try:
            key = dt.datetime.strptime(data.split("|")[1], "%Y%m%d").date().isoformat()
        except (IndexError, ValueError):
            return "Unknown button."
        mine = {v["ticker"]: v for v in self.store.votes(key) if v["user_id"] == user_id}
        stocks = self.store.stocks(key)
        if not stocks:
            return "That game is over."
        if not mine:
            return "You haven't picked anything yet. Tap ⬆️ ⬇️ ⏫ ⏬ next to a stock."
        lines = ["Your picks:"]
        for s in stocks:
            v = mine.get(s["ticker"])
            if not v:
                lines.append(f"{s['ticker']}: skipped")
                continue
            label = f"{s['ticker']} {icon(v['call'], v.get('conf'))} {CONF_LABEL.get(v.get('conf'), '').lower()}"
            if s["result"] in ("UP", "DOWN"):
                label += f" {'✅' if v['call'] == s['result'] else '❌'} {pts(points(v['call'], s['result'], 0, v.get('conf')))}"
            lines.append(label)
        r = self.store.round(key)
        if r and r["status"] == "open":
            lines.append(f"Change until {_sg(lock_at(dt.date.fromisoformat(key)))} (Singapore).")
        return "\n".join(lines)[:200]

    def on_mover(self, data: str, user_id: int, name: str, now: dt.datetime) -> str:
        got, err = self._open_round(data, now)
        if not got:
            return err
        day, key, ticker, _ = got
        self.store.player(user_id, name)
        self.store.set_bonus(key, user_id, ticker, now.isoformat())
        return f"🚀 {ticker}: +2 if it moves the most today, up or down."

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
        if cmd in ("/halfway", "/quiz"):
            return self.extra_command(cmd[1:], arg, chat_id), None
        if cmd == "/pick":
            return self.own_command(parts[1] if len(parts) > 1 else "", now)
        if cmd == "/set":
            return self.set_command(parts[1:], user_id, name, now, chat_id), None
        if cmd == "/add":
            return self.add_command(parts[1] if len(parts) > 1 else "", user_id, name, now, chat_id)
        if cmd == "/suggest":
            return self.suggest_command(parts[1] if len(parts) > 1 else "", user_id, name, now), None
        if cmd == "/today":
            row = self.store.db.execute("SELECT day FROM rounds ORDER BY day DESC LIMIT 1").fetchone()
            if not row:
                return "No game yet. The next one comes before the US market opens.", None
            day = dt.date.fromisoformat(row["day"])
            r = self.store.round(row["day"])
            stocks = self.store.stocks(row["day"])
            if r["status"] == "open":
                return self.round_text(day, stocks, self.voters(row["day"], chat_id)), self.buttons(day, stocks)
            if r["status"] == "locked":
                return self.lock_text(day, stocks, self.voters(row["day"], chat_id), chat_id), None
            return self.results_text(day, chat_id), self.why_buttons(day, stocks)
        if cmd == "/board":
            today, streaks = ny.date(), self.store.streaks()
            last_week = week_start(today) - dt.timedelta(days=7)
            return ("<b>This week</b>\n" + "\n".join(board_lines(self.store.scores(
                        week_start(today).isoformat(), today.isoformat(), chat_id), streaks=streaks))
                    + "\n\n<b>All time</b>\n" + "\n".join(board_lines(self.store.scores(chat_id=chat_id), streaks=streaks))
                    + self.own_board(week_start(today).isoformat(), today.isoformat(), chat_id)
                    + "\n\n<b>Last week's awards</b>\n"
                    + "\n".join(self.titles(last_week.isoformat(), (last_week + dt.timedelta(days=6)).isoformat(),
                                            chat_id))), None
        if cmd == "/me":
            rows = self.store.scores()
            mine = next((r for r in rows if r["user_id"] == user_id), None)
            bot = next((r for r in rows if r["user_id"] == BOT_ID), None)
            if not mine or not mine["played"]:
                return f"{esc(name)}: no scores yet. Play today's game!" + self.own_line(user_id), None
            streak = self.store.streaks().get(user_id, 0)
            won, tried = self.store.mover_record(user_id)
            vs = f"\n{esc(self.bot)}: {pts(bot['points'])}, right {bot['correct']} of {bot['played']}" if bot else ""
            return (f"{esc(name)}: {pts(mine['points'])} · right {mine['correct']} of {mine['played']} "
                    f"({mine['correct'] / mine['played']:.0%}) · 🔥 {streak} right in a row"
                    + (f" · 🚀 {won} of {tried} biggest movers" if tried else "") + vs + self.own_line(user_id)), None
        if cmd == "/bot":
            return self.bot_text(), None
        if cmd == "/report":
            lines = self.report_lines(user_id)
            if not lines:
                return (f"{esc(name)}: no results yet. Play a few games with ⏫ ⬆️ ⬇️ ⏬ "
                        "and your report will show up here."), None
            return f"📊 <b>{esc(name)}: how good are your choices? (all time)</b>\n" + "\n".join(lines), None
        if cmd == "/card":
            return self.personal_card(user_id, name, ny.date())
        if cmd == "/league":
            return self.league_command(arg, chat_id, ny.date()), None
        return None

    def calibration(self, user_id: int, since: str = "", until: str = "9999") -> dict[str, tuple[int, int]]:
        out = {"sure": [0, 0], "lean": [0, 0]}
        for d in self.store.decisions(user_id, since, until):
            out[d["conf"]][0] += d["call"] == d["result"]
            out[d["conf"]][1] += 1
        return {k: tuple(v) for k, v in out.items()}

    def bot_text(self) -> str:
        rows = self.store.scores()
        me = next((r for r in rows if r["user_id"] == BOT_ID), None)
        if not me or not me["played"]:
            return f"{esc(self.short)} has no results yet. Check again after the first results."
        humans = [r for r in rows if r["user_id"] != BOT_ID and r["played"]]
        beaten = sum(r["points"] > me["points"] for r in humans)
        won, tried = self.store.mover_record(BOT_ID)
        calls = self.store.calls(BOT_ID)
        cal = self.calibration(BOT_ID)
        wrong = sorted((c for c in calls if c["call"] != c["result"]), key=lambda c: -abs(move_of(c)))[:3]
        right = sorted((c for c in calls if c["call"] == c["result"]), key=lambda c: -abs(move_of(c)))[:1]
        lines = [f"🤖 <b>How good is {esc(self.short)}?</b>",
                 f"{pts(me['points'])} · right {me['correct']} of {me['played']} ({me['correct'] / me['played']:.0%}) "
                 f"· 🔥 {self.store.streaks().get(BOT_ID, 0)} right in a row"
                 + (f" · 🚀 {won} of {tried} biggest movers" if tried else "")]
        if cal["sure"][1]:
            lines.append(f"When it was <b>very sure</b>: right {pct(*cal['sure'])} (it needs 69% to gain points)")
        if cal["lean"][1]:
            lines.append(f"When it said <b>think so</b>: right {pct(*cal['lean'])} (it needs 57%)")
        lines += [f"Players doing better than it: {beaten} of {len(humans)}",
                  "<i>Tip: sounding sure doesn't make someone right. Always check their record, AI or person.</i>"]
        if wrong:
            lines += ["", "<b>Worst picks</b>"]
            for c in wrong:
                lines.append(f"❌ {esc(c['ticker'])} {ARROW[c['call']]} on {dt.date.fromisoformat(c['day']):%d %b}, "
                             f"went {move_of(c):+.1%}" + (f": <i>{esc(c['ai_reason'])}</i>" if c["ai_reason"] else ""))
        if right:
            c = right[0]
            lines += ["", f"<b>Best pick</b>\n✅ {esc(c['ticker'])} {ARROW[c['call']]} on "
                          f"{dt.date.fromisoformat(c['day']):%d %b}, went {move_of(c):+.1%}"]
        return "\n".join(lines)

    def report_lines(self, user_id: int, since: str = "", until: str = "9999") -> list[str]:
        """A player's decision habits over [since, until]: confidence vs results, reasons, skips."""
        cal = self.calibration(user_id, since, until)
        ds = self.store.decisions(user_id, since, until)
        if not ds:
            return []
        out = []
        got = sum(points(d["call"], d["result"], 0, d["conf"]) for d in ds)
        guess = sum((CONF[d["conf"]][0] + CONF[d["conf"]][1]) / 2 for d in ds)
        verdict = ("better than guessing, but a few weeks can still be luck: keep checking" if got > guess else
                   "no better than guessing so far")
        out.append(f"Points per pick: {got / len(ds):+.1f}. Random guessing with the same buttons would average "
                   f"{guess / len(ds):+.1f}: {verdict}.")
        k, n = cal["sure"]
        if n:
            verdict = ("✅ good, being very sure paid off" if k / n >= 11 / 16 else
                       "⚠️ too confident. 'Very sure' needs to be right 69% of the time: use 'think so' or skip more")
            out.append(f"When very sure: right {pct(k, n)} · {verdict}")
        k, n = cal["lean"]
        if n:
            out.append(f"When 'think so': right {pct(k, n)} · " + ("✅ good, above the 57% needed" if k / n >= 4 / 7 else
                                                                    "⚠️ below the 57% needed: skip when you're not sure"))
        by: dict[str, list[int]] = {}
        for d in ds:
            if d["reason"]:
                b = by.setdefault(d["reason"], [0, 0])
                b[0] += d["call"] == d["result"]
                b[1] += 1
        if by:
            parts = [f"{REASONS[r].split()[0]} {pct(*v)}" for r, v in sorted(by.items(), key=lambda kv: -kv[1][0] / kv[1][1])]
            out.append("Right, by reason: " + " · ".join(parts))
        skips = len(self.store.skipped(user_id, since, until))
        if skips:
            out.append(f"Skipped {skips} stock{'s' if skips != 1 else ''} (0 points: good when you're not sure)")
        return out

    def group_report(self, chat_id: int, since: str, until: str) -> str:
        members = sorted(self.store.members(chat_id))
        blocks = []
        for uid in members:
            if len(self.store.decisions(uid, since, until)) < 3:
                continue
            name = self.store.db.execute("SELECT name FROM players WHERE user_id=?", (uid,)).fetchone()
            lines = self.report_lines(uid, since, until)
            if lines and name:
                blocks.append(f"<b>{esc(name['name'])}</b>\n" + "\n".join(lines))
        if not blocks:
            return ""
        wk = self.market_week(since, until)
        head = "📊 <b>How good were our choices this week?</b>"
        if wk is not None:
            head += (f"\n🌍 Just holding the whole US market (SPY) this week: {wk:+.1%}. "
                     "If your picks can't beat guessing, a simple index fund is the better plan.")
        return head + "\n\n" + "\n\n".join(blocks) + "\n\nType /report any time to see your own."

    def personal_card(self, user_id: int, name: str, today: dt.date):
        since, until = week_start(today).isoformat(), today.isoformat()
        rows = self.store.scores(since, until)
        me = next((r for r in rows if r["user_id"] == user_id), None)
        if not me:
            return f"{esc(name)}: no scores this week yet. Play today's game!", None
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
            return "The league is for groups. Use /league in your game group."
        if arg == "join":
            self.store.set_league(chat_id, True)
            return "🏟 This group joined the league. Other groups can see its name and average score."
        if arg == "leave":
            self.store.set_league(chat_id, False)
            return "This group left the league. Other groups can't see it any more."
        c = self.store.chat(chat_id)
        table = self.store.league(week_start(today).isoformat(), today.isoformat())
        lines = ["🏟 <b>Groups this week</b> (average score per player, groups with 2 or more players)"]
        lines += [f"{i}. {esc(g['title'])}: {g['avg']:.1f} ({g['players']} players)" for i, g in enumerate(table, 1)]
        if not table:
            lines.append("(no groups yet)")
        lines.append("" if c and c["league"] else "")
        lines.append("This group is in the league. Type /league leave to leave." if c and c["league"]
                     else "This group isn't in the league. Type /league join to join (other groups will see the group's name).")
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
            return "A practice game is already on. Tap the buttons above.", None
        if p and now < dt.datetime.fromisoformat(p["ends"]) - dt.timedelta(seconds=PRACTICE_SECONDS) + PRACTICE_COOLDOWN:
            wait = dt.datetime.fromisoformat(p["ends"]) - dt.timedelta(seconds=PRACTICE_SECONDS) + PRACTICE_COOLDOWN - now
            return f"Practice again in {max(1, round(wait.total_seconds() / 60))} min.", None
        rng = random.Random(now.isoformat())
        tickers = rng.sample(POOL, PRACTICE_STOCKS)
        try:
            q = self.quotes(tickers)
        except Exception as e:
            self.log(f"practice quotes failed: {e!r}"[:300])
            return "Can't get prices right now. Try again soon.", None
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
            return "Can't get prices right now. Try again soon.", None
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
        lines = [f"🧪 <b>Practice game</b> (no points) · answers in {PRACTICE_SECONDS // 60} minutes",
                 f"Did each stock end <b>higher ⬆️ or lower ⬇️</b> than when the market opened {when}? No checking charts!",
                 f"{esc(self.bot)} has picked too.", ""]
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
            return "That practice game is over. Type /practice for a new one."
        if now >= dt.datetime.fromisoformat(p["ends"]):
            return "Time's up! Answers coming."
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
            lines += ["", "Nobody played. Type /practice to try again."]
        if any(s["ai_call"] for s in p["stocks"]):
            lines.append(f"{esc(self.bot)}: {bot_right}/{n}")
        lines.append("The real game comes every US trading day at 20:30 Singapore time (19:30 in summer).")
        return "\n".join(lines)

    # ---------------------------------------------------------------- weekend quiz
    def quiz_stage(self, now: dt.datetime) -> None:
        from . import quiz
        sg = now.astimezone(SG)
        saturday = sg.date() - dt.timedelta(days=(sg.weekday() - 5) % 7)
        week = quiz.week_key(saturday)
        post_at = dt.datetime.combine(saturday, QUIZ_POST[1], tzinfo=SG)
        results_at = dt.datetime.combine(saturday + dt.timedelta(days=1), QUIZ_RESULTS[1], tzinfo=SG)
        if not (post_at <= sg < results_at + dt.timedelta(hours=12)):
            return
        for chat in self.chat_ids():
            if not self.extra_on("quiz", chat):
                continue
            if sg < results_at and not self.store.get(f"quiz:{week}:{chat}"):
                self.post_quiz(chat, week, saturday)
            elif sg >= results_at and self.store.get(f"quiz:{week}:{chat}") == "posted":
                self.store.set(f"quiz:{week}:{chat}", "scored")
                self.send(chat, self.quiz_results(chat, week))

    def post_quiz(self, chat_id: int, week: str, saturday: dt.date) -> None:
        from . import quiz
        self.store.set(f"quiz:{week}:{chat_id}", "posted")
        qs = [quiz.BANK[i] for i in quiz.pick(week)]
        since = (saturday - dt.timedelta(days=5)).isoformat()
        moves = [(s["day"], s["ticker"], move_of(s)) for r in self.store.db.execute(
                     "SELECT day FROM rounds WHERE status='settled' AND day BETWEEN ? AND ?",
                     (since, saturday.isoformat())).fetchall()
                 for s in self.store.stocks(r["day"]) if move_of(s) is not None]
        game_q = quiz.game_question(moves)
        if game_q:
            qs.append(game_q)
        self.send(chat_id, f"🧩 <b>Weekend quiz</b>: {len(qs)} questions about money and making choices. Tap an answer "
                           "to see if you're right and why. Scores on Sunday at 8pm.")
        for i, (question, options, right, explain) in enumerate(qs):
            try:
                poll_id = self.tg.send_poll(question, options, right, explain, chat_id=chat_id)
            except Exception as e:
                self.log(f"quiz poll failed: {e!r}"[:300])
                continue
            if poll_id:
                self.store.add_quiz(poll_id, chat_id, week, i, right)

    def on_quiz_answer(self, poll_id: str, user_id: int, name: str, choice: int) -> None:
        self.store.player(user_id, name)
        if self.store.quiz_answer(poll_id, user_id, choice):
            row = self.store.db.execute("SELECT chat_id FROM quiz WHERE poll_id=?", (poll_id,)).fetchone()
            self.store.member(row["chat_id"], user_id)

    def quiz_results(self, chat_id: int, week: str) -> str:
        rows = self.store.quiz_scores(chat_id, week)
        if not rows:
            return "🧩 Nobody tried this weekend's quiz. Next one on Saturday at 10am!"
        medals = ["🥇", "🥈", "🥉"]
        lines = ["🧩 <b>Weekend quiz scores</b>"]
        lines += [f"{medals[i] if i < 3 else f'{i + 1}.'} {esc(r['name'])}: {r['ok']}/{r['answered']}"
                  for i, r in enumerate(rows[:10])]
        lines.append("The next game is tomorrow evening. Good luck!")
        return "\n".join(lines)

    # ---------------------------------------------------------------- own picks
    # Any US stock, any time: from the current price if the market is open (and not about to close), otherwise from
    # the next open; judged at that session's close with the same points as the daily game, on a separate score.

    def own_start(self, ny: dt.datetime) -> tuple[str, dt.date]:
        """('now', today) during the session (until 30 minutes before the close), else ('open', next session)."""
        d = ny.date()
        if market.is_trading_day(d):
            if lock_at(d) <= ny < close_at(d) - OWN_CUTOFF:
                return "now", d
            if ny < lock_at(d):
                return "open", d
        return "open", market.next_trading_day(d)

    def own_command(self, arg: str, now: dt.datetime):
        ticker = arg.upper().lstrip("$")
        if not ticker:
            return ("📌 Pick any US stock yourself: type /pick and the ticker, like <b>/pick NVDA</b> or "
                    f"<b>/pick TSLA</b>. Up to {OWN_PER_DAY} a day, with their own score."), None
        if not TICKER.match(ticker):
            return f"'{esc(arg)}' doesn't look like a US stock ticker. Try something like /pick AAPL.", None
        try:
            info = self.quotes([ticker]).get(ticker)
        except Exception:
            info = None
        if not info or not info.get("last"):
            return f"I can't find {esc(ticker)}. Check the ticker (like AAPL or NVDA) and try again.", None
        ny = now.astimezone(NY)
        kind, day = self.own_start(ny)
        today = info["update_time"] and info["update_time"].date() == ny.date() and info.get("prev_close")
        move = f" ({info['last'] / info['prev_close'] - 1:+.1%} today)" if today else ""
        when = ("<b>now</b>, from the current price" if kind == "now" else
                f"at the next market open: <b>{day:%a %d %b}, {_sg(lock_at(day))} Singapore time</b>")
        lines = [f"📌 <b>Own pick: {esc(ticker)}</b> {esc(info['name'])} · ${info['last']:.2f}{move}",
                 f"Starts {when}. Ends when the market closes that day ({_sg(close_at(day))} Singapore time).",
                 "Will it end <b>higher or lower</b> than where it starts?",
                 "⏫ / ⏬ very sure: +5 / −11 · ⬆️ / ⬇️ think so: +3 / −4",
                 f"Anyone can tap to make their own pick on {esc(ticker)} ({OWN_PER_DAY} own picks a day each)."]
        buttons = [[(f"⏫ {ticker}", f"o|{ticker}|U|s"), ("⬆️", f"o|{ticker}|U|l"), ("⬇️", f"o|{ticker}|D|l"),
                    (f"⏬ {ticker}", f"o|{ticker}|D|s")]]
        return "\n".join(lines), buttons

    def on_own(self, data: str, user_id: int, name: str, now: dt.datetime, chat_id: int | None) -> str:
        try:
            _, ticker, c, cf = data.split("|")
            call, conf = {"U": "UP", "D": "DOWN"}[c], {"s": "sure", "l": "lean"}[cf]
        except (ValueError, KeyError):
            return "Unknown button."
        if chat_id is None:
            return "Own picks are made in your game group."
        ny = now.astimezone(NY)
        mine = self.store.own("o.user_id=? AND o.ticker=? AND o.result IS NULL", (user_id, ticker))
        if mine:
            p = mine[-1]
            if p["start_price"] is None and p["call"] == call and p["conf"] == conf:
                self.store.delete_own(p["id"])
                return f"📌 {ticker}: own pick cancelled."
            return f"📌 You already have a {ticker} pick running. Wait for its result before picking it again."
        made_today = self.store.own("o.user_id=? AND o.made_day=?", (user_id, ny.date().isoformat()))
        if len(made_today) >= OWN_PER_DAY:
            return f"📌 That's your {OWN_PER_DAY} own picks for today. Try again tomorrow!"
        kind, day = self.own_start(ny)
        start, label = None, ""
        if kind == "now":
            try:
                info = self.quotes([ticker]).get(ticker) or {}
            except Exception:
                info = {}
            if not info.get("last") or not info.get("update_time") or info["update_time"].date() != day:
                return "📌 Can't get the price right now. Try again in a minute."
            start, label = info["last"], f"from ${info['last']:.2f} now until today's close"
        else:
            label = f"from the open on {day:%a %d %b} ({_sg(lock_at(day))} Singapore) until that day's close"
        self.store.player(user_id, name)
        self.store.add_own(chat_id=chat_id, user_id=user_id, ticker=ticker, call=call, conf=conf,
                           made_at=now.isoformat(), made_day=ny.date().isoformat(), day=day.isoformat(),
                           start_price=start, start_kind=kind)
        win, lose = CONF[conf]
        return (f"📌 {ticker} {icon(call, conf)}: {CONF_LABEL[conf].lower()} it goes {'up' if call == 'UP' else 'down'}, "
                f"{label}. +{win} if right, {lose} if wrong.")

    def own_stage(self, ny: dt.datetime) -> None:
        pending = self.store.own("o.result IS NULL")
        need_open = [p for p in pending if p["start_price"] is None and ny >= at(dt.date.fromisoformat(p["day"]), OPEN_CAPTURE)]
        need_close = [p for p in pending if ny >= close_at(dt.date.fromisoformat(p["day"])) + SETTLE_DELAY]
        tickers = sorted({p["ticker"] for p in need_open + need_close})
        if tickers and self._due("own", "all", ny, dt.timedelta(minutes=5)):
            try:
                q = self.quotes(tickers)
            except Exception as e:
                self.log(f"own-pick quotes failed: {e!r}"[:300])
                q = None
            if q is not None:
                for p in need_open:
                    o, _ = market.session_prices(dt.date.fromisoformat(p["day"]), q.get(p["ticker"]))
                    if o:
                        p["start_price"] = o
                        self.store.update_own(p["id"], start_price=o)
                for p in need_close:
                    day = dt.date.fromisoformat(p["day"])
                    info = q.get(p["ticker"])
                    o, c = market.session_prices(day, info)
                    start = p["start_price"] or o
                    if start and c:
                        result = "UP" if c > start else "DOWN" if c < start else "FLAT"
                        self.store.update_own(p["id"], start_price=start, close=c, result=result)
                    elif c or ny >= close_at(day) + GIVE_UP or (
                            info and info.get("update_time") and info["update_time"].date() > market.next_trading_day(day)):
                        self.store.update_own(p["id"], result=VOID)
        done = self.store.own("o.result IS NOT NULL AND o.posted=0")
        for chat in sorted({p["chat_id"] for p in done}):
            mine = [p for p in done if p["chat_id"] == chat]
            for p in mine:
                self.store.update_own(p["id"], posted=1)
            if chat in self.chat_ids():
                self.send(chat, self.own_results_text(mine))

    def own_results_text(self, picks: list[dict]) -> str:
        lines = ["📌 <b>Own picks results</b>"]
        for p in picks:
            head = f"{esc(p['player'])}: {esc(p['ticker'])} {icon(p['call'], p['conf'])}"
            if p["result"] in ("UP", "DOWN"):
                ok = p["call"] == p["result"]
                lines.append(f"{'✅' if ok else '❌'} {head} {p['start_price']:.2f} → {p['close']:.2f} "
                             f"({p['close'] / p['start_price'] - 1:+.1%}) · {pts(points(p['call'], p['result'], 0, p['conf']))}")
            else:
                lines.append(f"➖ {head}: no result ({'price didn\'t change' if p['result'] == 'FLAT' else 'price not available'})")
        return "\n".join(lines)

    def own_line(self, user_id: int) -> str:
        r = next((r for r in self.store.own_scores() if r["user_id"] == user_id), None)
        running = len(self.store.own("o.user_id=? AND o.result IS NULL", (user_id,)))
        if not r and not running:
            return ""
        out = "\n📌 Own picks: "
        if r:
            out += f"{pts(r['points'])}, right {r['correct']} of {r['played']}"
        if running:
            out += (" · " if r else "") + f"{running} waiting for a result"
        return out

    def own_board(self, since: str, until: str, chat_id: int | None) -> str:
        rows = self.store.own_scores(since, until, chat_id)
        if not rows:
            return ""
        return "\n\n<b>📌 Own picks this week</b>\n" + "\n".join(board_lines(rows))

    # ---------------------------------------------------------------- extras and market
    def extra_on(self, name: str, chat_id: int) -> bool:
        return self.store.get(f"extra:{name}:{chat_id}") == "on"

    def extra_command(self, name: str, arg: str, chat_id: int | None) -> str:
        if chat_id is None:
            return "Use this in your game group."
        if arg in ("on", "off"):
            self.store.set(f"extra:{name}:{chat_id}", arg)
            return f"{'✅ On' if arg == 'on' else 'Off'}: {EXTRAS[name]}."
        state = "on" if self.extra_on(name, chat_id) else "off"
        return f"{EXTRAS[name].capitalize()} is {state} for this group. Type /{name} on or /{name} off."

    def record_market(self, day: dt.date) -> None:
        """The whole market's (SPY) open-to-close move and close for the day, for the results and the Friday report."""
        key = day.isoformat()
        if self.store.get(f"market:{key}"):
            return
        try:
            info = self.quotes(["SPY"]).get("SPY")
        except Exception as e:
            self.log(f"market quote failed: {e!r}"[:300])
            return
        o, c = market.session_prices(day, info)
        prev = info.get("prev_close") if info and info.get("update_time") and info["update_time"].date() == day else None
        if o and c:
            self.store.set(f"market:{key}", json.dumps({"move": c / o - 1, "close": c, "prev": prev}))

    def market_move(self, key: str) -> float | None:
        raw = self.store.get(f"market:{key}")
        return json.loads(raw)["move"] if raw else None

    def market_week(self, since: str, until: str) -> float | None:
        """SPY from the close before the week's first recorded day to the last recorded close."""
        days = [r["k"].split(":", 1)[1] for r in self.store.db.execute(
            "SELECT k FROM kv WHERE k LIKE 'market:%' AND substr(k, 8) BETWEEN ? AND ? ORDER BY k", (since, until))]
        if not days:
            return None
        first, last = json.loads(self.store.get(f"market:{days[0]}")), json.loads(self.store.get(f"market:{days[-1]}"))
        return last["close"] / first["prev"] - 1 if first.get("prev") else None

    # ---------------------------------------------------------------- suggestions
    def next_game_day(self, ny: dt.datetime) -> dt.date:
        d = ny.date()
        if market.is_trading_day(d) and ny.time() < POST_AT:
            return d
        return market.next_trading_day(d)

    def suggest_command(self, arg: str, user_id: int, name: str, now: dt.datetime) -> str:
        day = self.next_game_day(now.astimezone(NY))
        preset = json.loads(self.store.get(f"preset:{day.isoformat()}", "[]"))
        ideas = [] if preset else self.store.suggested(day.isoformat())
        board = " · ".join(f"{i['ticker']} ({i['votes']})" for i in ideas[:5])
        ticker = arg.upper().lstrip("$")
        if not ticker:
            return (f"💡 Suggest a stock for the next game ({day:%a %d %b}): type /suggest and the ticker, like "
                    f"/suggest NVDA. The {SUGGESTED_MAX} most-suggested join that game."
                    + (f"\nSo far: {board}" if board else ""))
        if not TICKER.match(ticker):
            return f"'{esc(arg)}' doesn't look like a US stock ticker. Try something like /suggest AAPL."
        try:
            info = self.quotes([ticker]).get(ticker)
        except Exception:
            info = None
        if not info or not info.get("last"):
            return f"I can't find {esc(ticker)}. Check the ticker (like AAPL or NVDA) and try again."
        self.store.player(user_id, name)
        self.store.suggest(day.isoformat(), user_id, ticker, now.isoformat())
        board = " · ".join(f"{i['ticker']} ({i['votes']})" for i in self.store.suggested(day.isoformat())[:5])
        return (f"💡 {esc(name)} suggested <b>{esc(ticker)}</b> ({esc(info['name'])}) for the game on {day:%a %d %b}. "
                f"The {SUGGESTED_MAX} most-suggested join it.\nSo far: {board}")
