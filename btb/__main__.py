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
                ai_ready=lambda: glm.healthy(s), banter=lambda summary: glm.banter(s, summary),
                call_mover=lambda stocks: glm.call_mover(s, stocks), clock=clock,
                explain=lambda rows: glm.why(s, rows), teach=lambda summary: glm.lesson(s, summary))


GROUPS = ("group", "supergroup")


def reply(game: Game, chat_id: int, out) -> None:
    if isinstance(out, dict):
        try:
            game.tg.send_photo(out["photo"], out.get("caption", ""), chat_id=chat_id)
        except Exception as e:
            game.log(f"photo to {chat_id} failed: {e!r}"[:300])
    elif out:
        game.send(chat_id, *out)


def handle(game: Game, tg, u: dict, now: dt.datetime) -> None:
    """One Telegram update: group joins/leaves, button taps and commands, from any group the game plays in."""
    if mcm := u.get("my_chat_member"):
        chat = mcm.get("chat") or {}
        status = (mcm.get("new_chat_member") or {}).get("status")
        if chat.get("type") in GROUPS:
            if status in ("member", "administrator"):
                game.join(int(chat["id"]), chat.get("title", ""), now)
            elif status in ("left", "kicked"):
                game.store.deactivate(int(chat["id"]))
        return
    if pa := u.get("poll_answer"):
        user = pa.get("user") or {}
        if user.get("id") and pa.get("option_ids"):
            game.on_quiz_answer(str(pa.get("poll_id")), int(user["id"]), display_name(user), int(pa["option_ids"][0]))
        return
    if cq := u.get("callback_query"):
        chat = (cq.get("message") or {}).get("chat") or {}
        user = cq.get("from") or {}
        c = game.store.chat(int(chat["id"])) if chat.get("id") else None
        if chat.get("type") not in GROUPS or not c or not c["active"]:
            tg.answer(cq["id"], "This game is played in its Telegram group.")
            return
        data = cq.get("data", "")
        tg.answer(cq["id"], game.on_button(data, int(user.get("id", -1)), display_name(user), now,
                                           chat_id=int(chat["id"])), alert=game.is_alert(data))
        return
    m = u.get("message") or {}
    chat = m.get("chat") or {}
    if m.get("migrate_to_chat_id"):
        game.store.migrate_chat(int(chat["id"]), int(m["migrate_to_chat_id"]))
        return
    if not (m.get("text") or "").startswith("/") or not chat.get("id"):
        return
    user = m.get("from") or {}
    if chat.get("type") not in GROUPS:
        if m["text"].split()[0].split("@")[0].lower() in ("/start", "/help"):
            game.send(int(chat["id"]), game.help_text() + "\n\nAdd me to a group with your friends to play.")
        return
    if game.join(int(chat["id"]), chat.get("title", ""), now):
        reply(game, int(chat["id"]),
              game.on_command(m["text"], int(user.get("id", -1)), display_name(user), now, chat_id=int(chat["id"])))


def refresh_count(game: Game, tg, now: dt.datetime) -> None:
    """Keep the 'Players so far' line of each group's open round current."""
    day = now.astimezone(NY).date()
    key = day.isoformat()
    r = game.store.round(key)
    if not r or r["status"] != "open":
        return
    stocks = None
    for chat in game.chat_ids():
        mid = game.store.post_id(key, chat)
        if not mid:
            continue
        n = game.voters(key, chat)
        if str(n) != game.store.get(f"shown_voters:{key}:{chat}", "0"):
            stocks = stocks or game.store.stocks(key)
            game.edit(chat, mid, game.round_text(day, stocks, n), game.buttons(day, stocks))
            game.store.set(f"shown_voters:{key}:{chat}", str(n))


def run(s: Settings) -> int:  # pragma: no cover - network loop
    from .telegram import Telegram
    if not s.telegram_token or not s.glm_base_url:
        print("Set BTB_TELEGRAM_TOKEN and GLM_BASE_URL in .env first (see README).")
        return 2
    from .telegram import redact

    def log(msg) -> None:
        print(redact(msg, s.telegram_token), flush=True)

    store, tg = Store(s.db_path, s.bot_name, home_chat=s.chat_id or None), Telegram(s.telegram_token, s.chat_id)
    game = make_game(s, store, tg)
    game.log = log
    try:
        game.refresh_open_rounds()
    except Exception as e:
        log(f"refresh of open rounds failed: {e!r}"[:300])
    try:
        from .game import COMMANDS
        log("command menu registered" if tg.set_commands(COMMANDS) else "command menu NOT registered")
    except Exception as e:
        log(f"command menu failed: {e!r}"[:300])
    offset = int(store.get("tg_offset", "0") or 0)
    last_tick = 0.0
    print(f"{s.game_name} running in {len(store.chats())} group(s).", flush=True)
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if time.monotonic() - last_tick >= 30:
            last_tick = time.monotonic()
            try:
                game.tick(now)
                refresh_count(game, tg, now)
            except Exception as e:
                log(f"tick failed: {e!r}"[:400])
        try:
            updates = tg.updates(offset)
        except Exception as e:
            log(f"getUpdates failed: {e!r}"[:300])
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            store.set("tg_offset", str(offset))
            try:
                handle(game, tg, u, dt.datetime.now(dt.timezone.utc))
            except Exception as e:
                log(f"update failed: {e!r}"[:400])


class Printer:
    """Stands in for Telegram in the demo."""
    def send_photo(self, png, caption="", chat_id=None):
        print(f"[photo {len(png)} bytes] {caption}")
        return 1

    def send(self, text, buttons=None, chat_id=None, silent=None):
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
    store = Store(":memory:", s.bot_name)
    store.add_chat(1, "demo", "")
    game = make_game(s, store, Printer(), clock=lambda: at(day, POST_AT))
    t0 = time.monotonic()
    game.create(day, at(day, POST_AT))
    print(f"(round built in {time.monotonic() - t0:.0f}s; the AI's hidden calls:)")
    for st in store.stocks(day.isoformat()):
        print(f"  {st['ticker']}: {st['ai_call'] or 'sat out'} ({st['ai_conf'] or 0:.2f}) · {st['ai_reason']}")
    sample = {"stocks": [{"ticker": st["ticker"], "result": st["ai_call"]} for st in store.stocks(day.isoformat())],
              "scores": [{"user_id": 0, "name": s.bot_name, "points": 4, "correct": 3, "played": 5},
                         {"user_id": 1, "name": "Ann", "points": 5, "correct": 4, "played": 5},
                         {"user_id": 2, "name": "Ben", "points": -1, "correct": 1, "played": 5}], "you": 0}
    r = store.round(day.isoformat())
    print(f"  biggest mover: {r['ai_mover'] or 'none'} · {r['ai_mover_reason']}")
    print("(sample results banter:)", glm.banter(s, sample))
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
