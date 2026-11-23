"""python -m btb run | demo | whereami | setup"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from . import glm, market, news
from .config import NY, Settings
from .game import POST_AT, Game, at
from .store import Store


def display_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or user.get("username") or f"player{user.get('id')}"


def make_game(s: Settings, store: Store, tg, clock=None) -> Game:
    return Game(s, store, tg, quotes=market.Quotes(s), news=news.headlines,
                call_ai=lambda t, name, q, h: glm.call_stock(s, t, name, q, h),
                ai_ready=lambda: glm.healthy(s), clock=clock)


def handle(game: Game, tg, chat_id: str, u: dict, now: dt.datetime) -> None:
    if cq := u.get("callback_query"):
        if str((cq.get("message") or {}).get("chat", {}).get("id")) != chat_id:
            tg.answer(cq["id"], "This game runs in its own group.")
            return
        user = cq.get("from") or {}
        tg.answer(cq["id"], game.on_vote(cq.get("data", ""), int(user.get("id", -1)), display_name(user), now))
    elif m := u.get("message"):
        if str(m.get("chat", {}).get("id")) != chat_id or not (m.get("text") or "").startswith("/"):
            return
        user = m.get("from") or {}
        reply = game.on_command(m["text"], int(user.get("id", -1)), display_name(user), now)
        if reply:
            tg.send(*reply)


def refresh_count(game: Game, tg, now: dt.datetime) -> None:
    """Keep the 'Players so far' line of the open round current (at most once per tick)."""
    from .game import round_text, buttons
    day = now.astimezone(NY).date()
    key = day.isoformat()
    r = game.store.round(key)
    if not r or r["status"] != "open" or not r["message_id"]:
        return
    n = game.voters(key)
    if str(n) != game.store.get(f"shown_voters:{key}", "0"):
        stocks = game.store.stocks(key)
        tg.edit(r["message_id"], round_text(day, stocks, n), buttons(day, stocks))
        game.store.set(f"shown_voters:{key}", str(n))


def run(s: Settings) -> int:  # pragma: no cover - network loop
    from .telegram import Telegram
    if not s.telegram_token or not s.chat_id or not s.glm_base_url:
        print("Set BTB_TELEGRAM_TOKEN, BTB_CHAT_ID and GLM_BASE_URL in .env first (see README).")
        return 2
    store, tg = Store(s.db_path), Telegram(s.telegram_token, s.chat_id)
    game = make_game(s, store, tg)
    offset = int(store.get("tg_offset", "0") or 0)
    last_tick = 0.0
    print("Beat the Bot running.", flush=True)
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if time.monotonic() - last_tick >= 30:
            last_tick = time.monotonic()
            try:
                game.tick(now)
                refresh_count(game, tg, now)
            except Exception as e:
                print(f"tick failed: {e!r}"[:400], flush=True)
        try:
            updates = tg.updates(offset)
        except Exception as e:
            print(f"getUpdates failed: {e!r}"[:300], flush=True)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            store.set("tg_offset", str(offset))
            try:
                handle(game, tg, s.chat_id, u, dt.datetime.now(dt.timezone.utc))
            except Exception as e:
                print(f"update failed: {e!r}"[:400], flush=True)


class Printer:
    """Stands in for Telegram in the demo."""
    def send(self, text, buttons=None, chat_id=None):
        from .telegram import plain
        print(plain(text))
        if buttons:
            print("  [buttons] " + "  ".join(a for row in buttons for a, _ in row))
        print("-" * 60)
        return 1

    def edit(self, *a, **k):
        pass


def demo(s: Settings) -> int:
    """A real round (live news, quotes and model) printed here; nothing is sent or saved."""
    day = dt.datetime.now(NY).date()
    if not market.is_trading_day(day):
        day = market.next_trading_day(day)
    store = Store(":memory:")
    game = make_game(s, store, Printer(), clock=lambda: at(day, POST_AT))
    t0 = time.monotonic()
    game.create(day, at(day, POST_AT))
    print(f"(round built in {time.monotonic() - t0:.0f}s; the Bot's hidden calls:)")
    for st in store.stocks(day.isoformat()):
        print(f"  {st['ticker']}: {st['ai_call'] or 'sat out'} · {st['ai_reason']}")
    return 0


def whereami(s: Settings) -> int:  # pragma: no cover - network
    """List chats that recently messaged the game bot, to find BTB_CHAT_ID."""
    from .telegram import Telegram
    if not s.telegram_token:
        print("Set BTB_TELEGRAM_TOKEN in .env first.")
        return 2
    seen = {}
    for u in Telegram(s.telegram_token, "").updates(0, timeout=0):
        m = u.get("message") or (u.get("callback_query") or {}).get("message") or u.get("my_chat_member") or {}
        c = m.get("chat") or {}
        if c.get("id"):
            seen[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
    if not seen:
        print("No chats yet: add the bot to your group and send /help there, then run this again.")
    for cid, title in seen.items():
        print(f"{cid}\t{title}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="btb")
    p.add_argument("cmd", choices=["run", "demo", "whereami", "setup"])
    a = p.parse_args(argv)
    if a.cmd == "setup":
        from .setup import main as setup
        return setup()
    s = Settings.from_env()
    return {"run": run, "demo": demo, "whereami": whereami}[a.cmd](s)


if __name__ == "__main__":
    sys.exit(main())
