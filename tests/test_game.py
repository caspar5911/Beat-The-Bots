import datetime as dt
import json

import pytest

from btb import game as g, glm, market, news
from btb.__main__ import handle
from btb.config import NY, Settings
from btb.store import BOT_ID, BOT_NAME, Store

DAY = dt.date(2026, 11, 23)  # a Monday
CHAT = -100


def ny(day, h, m=0, s=0):
    return dt.datetime(day.year, day.month, day.day, h, m, s, tzinfo=NY)


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers, self.silent, self.to = [], [], [], [], []
        self.photos, self.left = [], []

    def send(self, text, buttons=None, chat_id=None, silent=None):
        self.sent.append((text, buttons))
        self.silent.append(silent)
        self.to.append(chat_id)
        return len(self.sent)

    def send_photo(self, png, caption="", chat_id=None, html=False):
        self.photos.append((chat_id, png, caption))
        return 1

    def leave(self, chat_id):
        self.left.append(chat_id)

    def edit(self, mid, text, buttons=None, chat_id=None):
        self.edits.append((mid, text))

    def answer(self, cid, text, alert=False):
        self.answers.append(text)
        self.alerts = getattr(self, "alerts", []) + [alert]

    def send_poll(self, question, options, correct, explanation="", chat_id=None):
        self.polls = getattr(self, "polls", []) + [(chat_id, question, options, correct, explanation)]
        return f"poll{len(self.polls)}"


class FakeQuotes:
    """Per-ticker snapshot rows; `raise_next` simulates OpenD being down."""
    def __init__(self):
        self.rows, self.fail = {}, False

    def set(self, t, open_=None, last=None, prev_close=100.0, pre=101.0, when=None, high52=None, low52=None):
        self.rows[t] = {"name": f"{t} Inc", "open": open_, "last": last, "prev_close": prev_close,
                        "pre_price": pre, "update_time": when, "high52": high52, "low52": low52}

    def __call__(self, tickers):
        if self.fail:
            raise RuntimeError("OpenD down")
        return {t: self.rows[t] for t in tickers if t in self.rows}


def make(ai=True, ai_ready=True, clock_at=None, conf=None, banter=None, mover=None, chats=(CHAT,)):
    store, tg, q = Store(":memory:"), FakeTG(), FakeQuotes()
    for c in chats:
        store.add_chat(c, f"group{c}", "")
    calls = []

    def call_ai(t, name, info, heads):
        calls.append(t)
        return ("UP", f"{t} goes up, humans", (conf or {}).get(t, 0.5)) if ai else None

    clock = {"now": clock_at or ny(DAY, 7, 30)}
    game = g.Game(Settings(stocks_per_day=3), store, tg, quotes=q,
                  news=lambda t, n: [{"title": f"{t} news", "published": dt.datetime(2026, 11, 23, 12, tzinfo=dt.timezone.utc)}],
                  call_ai=call_ai, ai_ready=lambda: ai_ready, banter=banter, call_mover=mover, log=lambda *_: None,
                  clock=lambda: clock["now"])
    button, command = game.on_button, game.on_command
    game.on_button = lambda d, u, n, now, chat_id=chats[0] if chats else None: button(d, u, n, now, chat_id)
    game.on_command = lambda t, u, n, now, chat_id=chats[0] if chats else None: command(t, u, n, now, chat_id)
    return game, store, tg, q, calls, clock


def lean_rows(b):
    """Choice rows reordered as [lean up, lean down, sure up, sure down] (each line: a name row, then ⏫ ⬆️ ⬇️ ⏬)."""
    return [[r[1], r[2], r[0], r[3]] for r in choices(b)]


def choices(b):
    """Just the 4-button choice rows of a keyboard, in order (sure up, up, down, sure down)."""
    return [r for r in b if len(r) == 4]


def tickers(store, day=DAY):
    return [s["ticker"] for s in store.stocks(day.isoformat())]


def test_pick_is_deterministic_and_distinct():
    a, b = g.pick_tickers(DAY, 5), g.pick_tickers(DAY, 5)
    assert a == b and len(set(a)) == 5
    assert g.pick_tickers(DAY + dt.timedelta(days=1), 5) != a


def test_calendar():
    assert not market.is_trading_day(dt.date(2026, 11, 26))       # Thanksgiving
    assert market.close_time(dt.date(2026, 11, 27)) == dt.time(13)
    assert market.close_time(DAY) == dt.time(16)
    assert market.next_trading_day(dt.date(2026, 11, 25)) == dt.date(2026, 11, 27)
    assert market.next_trading_day(dt.date(2026, 11, 20)) == DAY  # Friday -> Monday


def test_session_prices():
    q = {"open": 10.0, "last": 11.0, "prev_close": 9.0}
    assert market.session_prices(DAY, dict(q, update_time=ny(DAY, 16))) == (10.0, 11.0)
    assert market.session_prices(DAY, dict(q, update_time=ny(DAY, 12))) == (10.0, None)
    assert market.session_prices(DAY, dict(q, update_time=ny(DAY + dt.timedelta(days=1), 10))) == (None, 9.0)
    assert market.session_prices(DAY, dict(q, update_time=ny(DAY - dt.timedelta(days=3), 16))) == (None, None)
    assert market.session_prices(DAY, None) == (None, None)


def full_day(game, store, tg, q, clock):
    game.tick(ny(DAY, 7, 29))
    assert store.round(DAY.isoformat()) is None
    game.tick(ny(DAY, 7, 30))
    ts = tickers(store)
    for t in ts:
        q.set(t)
    return ts


def test_whole_day():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    text, buttons = tg.sent[0]
    assert [len(r) for r in buttons] == [2, 4, 2, 4, 2, 4, 1] and buttons[-1][0][1] == "k|20261123"   # + "My picks"
    assert buttons[0][1] == ("📊 More", f"x|20261123|{ts[0]}")
    assert buttons[0][0][1] == f"i|20261123|{ts[0]}" and g.ETF_LABEL[ts[0]].split(" (")[0] in buttons[0][0][0] or ts[0] == "SMH"
    assert ts[0] in game.on_button(buttons[0][0][1], 7, "Ann", ny(DAY, 8)) and game.is_alert(buttons[0][0][1])
    assert all(len(cb.encode()) <= 64 for row in buttons for _, cb in row)
    buttons = lean_rows(buttons)
    assert "Huat or Not?" in text and "not advice" in text and "not sure: skip" in text
    assert "goes up" not in text                     # the AI's calls stay hidden until the lock
    assert calls == ts
    up, down = buttons[0][0][1], buttons[0][1][1]
    assert "think so up (+3 / -4)" in game.on_button(up, 7, "Ann", ny(DAY, 9, 0))
    assert "think so down" in game.on_button(down, 7, "Ann", ny(DAY, 9, 10))    # changed mind: last one counts
    for row in buttons[1:]:
        game.on_button(row[0][1], 7, "Ann", ny(DAY, 9, 0))
    game.on_button(buttons[0][0][1], 8, "Ben", ny(DAY, 9, 29, 59))
    game.tick(ny(DAY, 9, 30))
    assert store.round(DAY.isoformat())["status"] == "locked"
    lock = tg.sent[-1][0]
    assert "goes up, humans" in lock and "group ⬆️ 1 · ⬇️ 1" in lock and "think so (50% sure)" in lock
    assert "Too late" in game.on_button(buttons[1][0][1], 8, "Ben", ny(DAY, 9, 30, 1))
    for t in ts:
        q.set(t, open_=100.0, last=100.5, when=ny(DAY, 9, 36))
    game.tick(ny(DAY, 9, 36))
    assert all(s["open"] == 100.0 for s in store.stocks(DAY.isoformat()))
    q.set(ts[0], open_=100.0, last=98.0, when=ny(DAY, 16))
    for t in ts[1:]:
        q.set(t, open_=100.0, last=103.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 5))
    assert store.round(DAY.isoformat())["status"] == "locked"   # not before close + 10 min
    game.tick(ny(DAY, 16, 10))
    assert store.round(DAY.isoformat())["status"] == "settled"
    res, res_buttons = tg.sent[-1]
    assert "Results" in res and "Beat Ah Huat today: Ann" in res and "📚 <b>Tip:</b>" in res
    assert [cb for _, cb in res_buttons[0]] == [f"w|20261123|{t}" for t in ts]
    assert "The group together got 2 of 3 right, Ah Huat got 2 of 3: a draw" in res        # group: Ann DOWN/Ben UP tie on #1 -> no call
    scores = {r["name"]: (r["points"], r["correct"], r["played"]) for r in store.scores()}
    assert scores["Ann"] == (9, 3, 3)                # three right Leans
    assert scores["Ben"] == (-4, 0, 1)
    assert scores[BOT_NAME] == (2, 2, 3)             # 3 + 3 - 4
    assert "no clear news" in game.on_button(res_buttons[0][1][1], 7, "Ann", ny(DAY, 17)) and game.is_alert("w|x")
    game.tick(ny(DAY, 16, 30))
    assert sum(t.startswith("🏁") for t, _ in tg.sent) == 1


def test_no_round_on_holiday_weekend_or_late_start():
    for day, t in [(dt.date(2026, 11, 26), (8, 50)), (dt.date(2026, 11, 28), (8, 50)), (DAY, (9, 25))]:
        game, store, *_ = make()
        game.tick(ny(day, *t))
        assert store.round(day.isoformat()) is None


def test_model_down_round_still_runs():
    game, store, tg, q, calls, clock = make(ai_ready=False)
    full_day(game, store, tg, q, clock)
    assert calls == [] and all(s["ai_call"] is None for s in store.stocks(DAY.isoformat()))
    game.tick(ny(DAY, 9, 30))
    assert "didn't play" in tg.sent[-1][0]
    assert not [v for v in store.votes(DAY.isoformat()) if v["user_id"] == BOT_ID]


def test_bad_model_answer_and_deadline():
    game, store, tg, q, calls, clock = make(ai=False)
    full_day(game, store, tg, q, clock)
    assert all(s["ai_call"] is None for s in store.stocks(DAY.isoformat()))
    game, store, tg, q, calls, clock = make(clock_at=ny(DAY, 9, 16))
    game.tick(ny(DAY, 9, 16))
    assert store.round(DAY.isoformat()) and calls == []


def test_missed_close_recovers_from_next_session():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    for t in ts:
        q.set(t, open_=100.0, last=100.0, when=ny(DAY, 9, 40))
    game.tick(ny(DAY, 9, 40))
    nxt = DAY + dt.timedelta(days=1)
    for t in ts:  # bot was down over the close; next session is trading
        q.set(t, open_=120.0, last=121.0, prev_close=104.0, when=ny(nxt, 10))
    game.tick(ny(nxt, 10, 1))
    assert all((s["close"], s["result"]) == (104.0, "UP") for s in store.stocks(DAY.isoformat()))


def test_open_never_captured_is_void():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    nxt = DAY + dt.timedelta(days=1)
    for t in ts:
        q.set(t, open_=120.0, last=121.0, prev_close=104.0, when=ny(nxt, 10))
    game.tick(ny(nxt, 10))
    assert {s["result"] for s in store.stocks(DAY.isoformat())} == {"VOID"}
    assert "no points" in tg.sent[-1][0]


def test_quotes_down_then_give_up():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    q.fail = True
    game.tick(ny(DAY, 16, 10))                # no crash, still waiting
    assert store.round(DAY.isoformat())["status"] == "locked"
    game.tick(ny(DAY + dt.timedelta(days=4), 16, 10))
    assert store.round(DAY.isoformat())["status"] == "settled"
    assert {s["result"] for s in store.stocks(DAY.isoformat())} == {"VOID"}


def test_flat_is_not_scored():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    for t in ts:
        q.set(t, open_=100.0, last=100.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 10))
    assert {s["result"] for s in store.stocks(DAY.isoformat())} == {"FLAT"}
    assert store.scores() == []


def test_commands_and_group_handling():
    game, store, tg, q, calls, clock = make()
    assert "No game yet" in game.on_command("/today", 1, "Ann", ny(DAY, 8))[0]
    full_day(game, store, tg, q, clock)
    text, buttons = game.on_command("/today@BeatTheBotBot", 1, "Ann", ny(DAY, 9))
    assert "Huat or Not?" in text and buttons
    assert "This week" in game.on_command("/board@BeatTheBotBot extra", 1, "Ann", ny(DAY, 9))[0]
    assert "no scores" in game.on_command("/me", 1, "Ann", ny(DAY, 9))[0]
    assert game.on_command("/unknown", 1, "Ann", ny(DAY, 9)) is None
    # buttons from groups the game doesn't play in are refused; votes answer with a toast
    cb = choices(buttons)[0][0][1]
    handle(game, tg, {"callback_query": {"id": "x", "data": cb, "from": {"id": 5, "first_name": "Eve"},
                                         "message": {"chat": {"id": -999, "type": "group"}}}}, ny(DAY, 9))
    assert tg.answers[-1].startswith("This game") and not store.votes(DAY.isoformat())
    handle(game, tg, {"callback_query": {"id": "x", "data": cb, "from": {"id": 5, "first_name": "Eve"},
                                         "message": {"chat": {"id": CHAT, "type": "group"}}}}, ny(DAY, 9))
    assert "very sure up" in tg.answers[-1] and store.votes(DAY.isoformat())[0]["name"] == "Eve"
    assert 5 in store.members(CHAT)
    assert "Unknown" in game.on_vote("garbage", 5, "Eve", ny(DAY, 9))
    assert "is over" in game.on_vote("v|20261123|ZZZZ|U", 5, "Eve", ny(DAY, 9))


def test_news_relevance():
    assert news.about("Meta Platforms buys more chips", "META", "Meta Platforms")
    assert news.about("Alphabet (GOOGL) rebounds", "GOOGL", "Alphabet-A")
    assert news.about("Eli Lilly drug trial", "LLY", "Eli Lilly and Co")
    assert not news.about("How Taylor Sheridan makes a profit", "META", "Meta Platforms")
    assert not news.about("JPMorgan price prediction", "LLY", "Eli Lilly and Co")
    assert not news.about("Lovely day", "V", "Visa")


def test_parse_call():
    assert glm.parse_call('```json\n{"call":"down","reason":"meh"}\n```') == ("DOWN", "meh", 0.5)
    assert glm.parse_call('{"call":"UP","confidence":7}')[2] == 1.0
    assert glm.parse_call('{"call":"UP","confidence":"x"}')[2] == 0.5
    assert glm.parse_call('{"call":"SIDEWAYS"}') is None
    assert glm.parse_call("not json") is None
    assert glm.parse_call("[1]") is None


def test_call_stock_returns_none_when_model_fails():
    import requests

    class Boom:
        def post(self, *a, **k):
            raise requests.ConnectionError("down")

    s = Settings(glm_base_url="http://x/v1")
    assert glm.call_stock(s, "AAPL", "Apple", {}, [], session=Boom(), sleep=lambda _: None) is None


def test_telegram_falls_back_to_plain_text():
    from btb.telegram import Telegram

    class Resp:
        def __init__(self, data, status=200):
            self.data, self.status_code = data, status

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.bodies = []

        def post(self, url, json=None, timeout=None):
            self.bodies.append(json)
            return Resp({"ok": False}) if len(self.bodies) == 1 else Resp({"ok": True, "result": {"message_id": 9}})

    sess = Session()
    assert Telegram("t", "1", session=sess).send("<b>hi</b> &amp; bye") == 9
    assert sess.bodies[1]["text"] == "hi & bye" and "parse_mode" not in sess.bodies[1]


def test_failed_post_is_retried_until_lock():
    game, store, tg, q, calls, clock = make()
    real = tg.send
    tg.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Telegram 502"))
    game.tick(ny(DAY, 7, 30))                    # logged, not raised
    assert store.post_id(DAY.isoformat(), CHAT) is None
    tg.send = real
    game.tick(ny(DAY, 7, 31))                    # retries are spaced
    assert not tg.sent
    game.tick(ny(DAY, 7, 33))
    assert store.post_id(DAY.isoformat(), CHAT) == 1 and "Huat or Not?" in tg.sent[0][0]
    assert len(calls) == 3                     # the model was not asked again


def test_settle_retries_are_spaced():
    game, store, tg, q, calls, clock = make()
    full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    q.fail = True
    for m in (10, 11, 15, 21):
        game.tick(ny(DAY, 16, m))
    assert store.round(DAY.isoformat())["status"] == "locked"
    q.fail = False
    for t in tickers(store):
        q.set(t, open_=100.0, last=101.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 25))                 # within 10 minutes of the 16:21 try
    assert store.round(DAY.isoformat())["status"] == "locked"
    game.tick(ny(DAY, 16, 31))
    assert store.round(DAY.isoformat())["status"] == "settled"


def test_generic_company_names():
    assert news.about("Advanced Micro Devices unveils chip", "AMD", "Advanced Micro Devices")
    assert not news.about("Advanced packaging demand soars", "AMD", "Advanced Micro Devices")
    assert news.about("Supermicro shares jump", "SMCI", "Super Micro Computer")
    assert not news.about("Super Bowl ads", "SMCI", "Super Micro Computer")
    assert news.about("Coca-Cola raises prices", "KO", "Coca-Cola")
    assert not news.about("He hurt his arm", "ARM", "Arm Holdings")
    assert news.about("Visa beats estimates", "V", "Visa")
    assert news.about("Ford recalls trucks", "F", "Ford Motor") and not news.about("F-35 jets", "F", "Ford Motor")
    assert news.about("General Motors cuts jobs", "GM", "General Motors") and not news.about("General election", "GM", "General Motors")


def test_quiet_at_night():
    from btb.telegram import night
    sg = dt.timezone(dt.timedelta(hours=8))
    assert night(dt.datetime(2026, 11, 24, 5, 10, tzinfo=sg))
    assert night(dt.datetime(2026, 11, 23, 23, 0, tzinfo=sg))
    assert not night(dt.datetime(2026, 11, 23, 22, 30, tzinfo=sg))


def test_setup_helpers(tmp_path):
    from btb.setup import group_chats, set_env
    env = tmp_path / ".env"
    env.write_text("# c\nBTB_TELEGRAM_TOKEN=\nGLM_MODEL=x\n")
    set_env(env, "BTB_TELEGRAM_TOKEN", "1:abc")
    set_env(env, "BTB_CHAT_ID", "-5")
    assert env.read_text() == "# c\nBTB_TELEGRAM_TOKEN=1:abc\nGLM_MODEL=x\nBTB_CHAT_ID=-5\n"
    assert oct(env.stat().st_mode)[-3:] == "600"
    ups = [{"my_chat_member": {"chat": {"id": -1, "type": "group", "title": "Old"},
                               "new_chat_member": {"status": "member"}}},
           {"message": {"chat": {"id": 9, "type": "private"}}},
           {"message": {"chat": {"id": -1, "type": "group", "title": "Old"}, "migrate_to_chat_id": -1001}},
           {"my_chat_member": {"chat": {"id": -2, "type": "group", "title": "Left"},
                               "new_chat_member": {"status": "left"}}}]
    assert group_chats(ups) == {-1001: "Old"}


# ---------------------------------------------------------------- upgrades

def settle_day(game, q, ts, day, closes, opens=None):
    """Lock, then close each ticker at the given price from an open of 100."""
    game.tick(ny(day, 9, 30))
    for t, c in zip(ts, closes):
        q.set(t, open_=100.0, last=c, when=ny(day, 16))
    game.tick(ny(day, 16, 10) + dt.timedelta(minutes=11 * len(game.tg.sent)))  # past any retry spacing


def test_confidence_rules_and_scoring():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    b = choices(game.buttons(DAY, store.stocks(DAY.isoformat())))
    sure_up, lean_up, lean_down, sure_down = (x[1] for x in b[0])
    assert "very sure up (+5 / -11)" in game.on_button(sure_up, 7, "Ann", ny(DAY, 9))
    assert "cancelled" in game.on_button(sure_up, 7, "Ann", ny(DAY, 9, 1))     # tap again: skip
    assert not store.votes(DAY.isoformat())
    game.on_button(sure_up, 7, "Ann", ny(DAY, 9, 2))
    game.on_button(b[1][3][1], 7, "Ann", ny(DAY, 9, 2))                     # #2 sure down
    game.on_button(b[2][2][1], 7, "Ann", ny(DAY, 9, 2))                     # #3 lean down
    assert "Unknown" in game.on_button(f"v|20261123|{ts[0]}|U|x", 7, "Ann", ny(DAY, 9))
    settle_day(game, q, ts, DAY, [101.0, 101.0, 99.0])     # #1 up (sure right), #2 up (sure wrong), #3 down (lean right)
    ann = next(r for r in store.scores() if r["user_id"] == 7)
    assert (ann["points"], ann["correct"], ann["played"]) == (5 - 11 + 3, 2, 3)
    assert game.calibration(7) == {"sure": (1, 2), "lean": (1, 1)}


def test_ai_confidence_is_shown_and_scored():
    ts = g.pick_tickers(DAY, 3)
    game, store, tg, q, calls, clock = make(conf={ts[1]: 0.8})
    full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    lock = tg.sent[-1][0]
    assert f"<b>{ts[1]}</b> group ⬆️ 0 · ⬇️ 0 · Ah Huat ⏫ very sure (80% sure)" in lock and lock.count("think so (50% sure)") == 2
    confs = {v["ticker"]: v["conf"] for v in store.votes(DAY.isoformat()) if v["user_id"] == BOT_ID}
    assert confs == {ts[0]: "lean", ts[1]: "sure", ts[2]: "lean"}


def test_reminder_tags_regulars_missing_picks():
    game, store, tg, q, calls, clock = make()
    prev = dt.date(2026, 11, 20)
    store.player(7, "Ann"); store.player(8, "Ben"); store.player(9, "Old")
    for u in (7, 8, 9):
        store.member(CHAT, u)
    store.vote(prev.isoformat(), "AAPL", 7, "UP", "x"); store.vote(prev.isoformat(), "AAPL", 8, "UP", "x")
    store.vote("2026-10-01", "AAPL", 9, "UP", "x")                           # inactive: not nagged
    ts = full_day(game, store, tg, q, clock)
    b = lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat())))
    for row in b:
        game.on_button(row[0][1], 8, "Ben", ny(DAY, 9))                       # Ben is done
    game.on_button(b[0][0][1], 7, "Ann", ny(DAY, 9))                          # Ann made 1 of 3
    game.tick(ny(DAY, 9, 19))
    assert not any("10 minutes" in t for t, _ in tg.sent)
    game.tick(ny(DAY, 9, 20))
    text = tg.sent[-1][0]
    assert "10 minutes" in text and 'tg://user?id=7' in text and "2 left" in text
    assert "id=8" not in text and "id=9" not in text
    game.tick(ny(DAY, 9, 25))
    assert sum("10 minutes" in t for t, _ in tg.sent) == 1


def test_reminder_without_regulars():
    game, store, tg, q, calls, clock = make()
    full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 21))
    assert "Last chance" in tg.sent[-1][0]


def test_halftime_live_standings():
    game, store, tg, q, calls, clock = make()
    assert "✅ On" in game.on_command("/halfway on", 1, "A", ny(DAY, 7))[0]
    ts = full_day(game, store, tg, q, clock)
    b = lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat())))
    for row in b:
        game.on_button(row[1][1], 7, "Ann", ny(DAY, 9))                       # Ann: all DOWN, the AI: all UP
    game.tick(ny(DAY, 9, 30))
    for t in ts:
        q.set(t, open_=100.0, last=97.0, when=ny(DAY, 11, 0))
    game.tick(ny(DAY, 10, 59))
    assert not any(t.startswith("⏱") for t, _ in tg.sent)
    game.tick(ny(DAY, 11, 0))
    text = tg.sent[-1][0]
    assert "Halfway update" in text and "-3.0% since the market opened" in text and "is losing" in text
    assert "Ann: 9 pts" in text and "Ah Huat: -12 pts" in text and "So far the group together has 3 right, Ah Huat has 0" in text
    assert tg.silent[-1] is False                                             # halftime pings even at midnight SGT
    game.tick(ny(DAY, 11, 30))
    assert sum(t.startswith("⏱") for t, _ in tg.sent) == 1


def test_halftime_skipped_when_late():
    game, store, tg, q, calls, clock = make()
    full_day(game, store, tg, q, clock)
    game.tick(ny(DAY, 9, 30))
    game.tick(ny(DAY, 15, 45))                                                # bot was down all day
    assert not any(t.startswith("⏱") for t, _ in tg.sent) and store.get(f"halftime:{DAY.isoformat()}") == "skipped"


def test_streaks_and_banter():
    said = []
    game, store, tg, q, calls, clock = make(banter=lambda summary: said.append(summary) or "Ann got lucky.")
    ts = full_day(game, store, tg, q, clock)
    for row in lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat()))):
        game.on_button(row[0][1], 7, "Ann", ny(DAY, 9))
    settle_day(game, q, ts, DAY, [101.0, 102.0, 103.0])
    res = tg.sent[-1][0]
    assert "💬" in res and "Ann got lucky." in res and said[0]["you"] == BOT_ID
    assert store.streaks()[7] == 3 and "🔥3" in res
    assert "🔥 3 right in a row" in game.on_command("/me", 7, "Ann", ny(DAY, 17))[0]


def test_banter_falls_back_when_model_fails():
    def boom(summary):
        raise RuntimeError("down")
    game, store, tg, q, calls, clock = make(banter=boom)
    ts = full_day(game, store, tg, q, clock)
    for row in lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat()))):
        game.on_button(row[1][1], 7, "Ann", ny(DAY, 9))
        game.on_button(row[1][1], 8, "Ben", ny(DAY, 9))
    settle_day(game, q, ts, DAY, [101.0, 102.0, 103.0])                       # the AI was right, humans all wrong
    assert "goldfish" in tg.sent[-1][0]
    assert "No humans" in g.fallback_banter("🤖 X", [{"user_id": BOT_ID, "points": 1, "correct": 1, "played": 1}])


def test_weekly_titles_on_last_trading_day():
    fri = dt.date(2026, 11, 20)
    assert g.last_of_week(fri) and not g.last_of_week(DAY)
    assert g.last_of_week(dt.date(2026, 11, 27))                              # half-day Friday
    game, store, tg, q, calls, clock = make(clock_at=ny(fri, 7, 30))
    game.tick(ny(fri, 7, 30))
    ts = tickers(store, fri)
    b = lean_rows(game.buttons(fri, store.stocks(fri.isoformat())))
    for i, row in enumerate(b):
        game.on_button(row[1 if i == 0 else 0][1], 7, "Ann", ny(fri, 9))      # Ann: #1 DOWN, rest UP (Lean)
        game.on_button(row[1][1], 8, "Ben", ny(fri, 9))                       # Ben: all DOWN
    settle_day(game, q, ts, fri, [99.0, 102.0, 103.0])                        # the AI said UP on #1 and lost
    res = next(t for t, _ in reversed(tg.sent) if t.startswith("🏁"))
    assert "This week's awards" in res and "👑 Best player, and beat Ah Huat: <b>Ann</b>" in res and "Last place: <b>Ben</b>" in res
    board = game.on_command("/board", 7, "Ann", ny(DAY, 8))[0]                # next week: last week's titles
    assert "Last week's awards" in board and "Best player" in board


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE stocks (day TEXT NOT NULL, pos INTEGER NOT NULL, ticker TEXT NOT NULL,
        name TEXT NOT NULL, prev_close REAL, pre_price REAL, headlines TEXT NOT NULL DEFAULT '[]', ai_call TEXT,
        ai_reason TEXT NOT NULL DEFAULT '', open REAL, close REAL, result TEXT, PRIMARY KEY (day, ticker));
        CREATE TABLE votes (day TEXT NOT NULL, ticker TEXT NOT NULL, user_id INTEGER NOT NULL, call TEXT NOT NULL,
        at TEXT NOT NULL, PRIMARY KEY (day, ticker, user_id));
        CREATE TABLE players (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO players VALUES (0, '🤖 The Bot');""")
    db.commit(); db.close()
    store = Store(path, "🤖 Ah Huat")
    store.player(7, "Ann")
    store.vote("2026-11-23", "AAPL", 7, "UP", "x")
    store.set_double("2026-11-23", 7, "AAPL")
    assert store.votes("2026-11-23")[0]["double"] == 1
    assert store.db.execute("SELECT name FROM players WHERE user_id=0").fetchone()[0] == "🤖 Ah Huat"


def test_send_silent_override():
    from btb.telegram import Telegram

    class Session:
        def __init__(self):
            self.bodies = []

        def post(self, url, json=None, timeout=None):
            self.bodies.append(json)

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "result": {"message_id": 1}}
            return R()

    sess = Session()
    Telegram("t", "1", session=sess).send("x", silent=False)
    Telegram("t", "1", session=sess).send("y", silent=True)
    assert [b["disable_notification"] for b in sess.bodies] == [False, True]


def test_practice_round():
    game, store, tg, q, calls, clock = make()
    now = ny(DAY, 20, 0)                                                      # evening: market closed
    for t in g.POOL:
        q.set(t, open_=100.0, last=101.0 if t < "M" else 99.0, when=ny(DAY, 16))
    text, buttons = game.on_command("/practice@HuatOrNotBot", 7, "Ann", now)
    assert "Practice game" in text and "Mon 23 Nov" in text and len(buttons) == 3
    assert "already on" in game.on_command("/practice", 7, "Ann", now)[0]
    picks = []
    for row in buttons:
        assert len(row[0][1].encode()) <= 64
        assert "practice" in game.on_button(row[0][1], 7, "Ann", now + dt.timedelta(seconds=30))
        picks.append(row[0][0].split()[1])
    game.tick(now + dt.timedelta(seconds=60))
    assert not any("Practice answers" in t for t, _ in tg.sent)
    assert "Time's up" in game.on_button(buttons[0][1][1], 8, "Ben", now + dt.timedelta(seconds=121))
    game.tick(now + dt.timedelta(seconds=150))
    res = tg.sent[-1][0]
    ups = sum(t < "M" for t in picks)
    assert "Practice answers" in res and f"Ann: {ups}/3" in res and "goes up, humans" in res
    game.tick(now + dt.timedelta(seconds=200))
    assert sum("Practice answers" in t for t, _ in tg.sent) == 1
    assert store.scores() == [] and store.db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0
    assert "Practice game" in game.on_command("/practice", 7, "Ann", now + dt.timedelta(minutes=10))[0]
    assert "over" in game.on_button(buttons[0][0][1], 7, "Ann", now + dt.timedelta(minutes=10))


def test_practice_when_quotes_down():
    game, store, tg, q, calls, clock = make()
    q.fail = True
    assert "Can't get prices" in game.on_command("/practice", 7, "Ann", ny(DAY, 20))[0]


# ---------------------------------------------------------------- round 3: groups, movers, bonus, cards

def test_pick_stocks_prefers_premarket_movers():
    q = {t: {"pre_price": 100.2, "prev_close": 100.0} for t in g.ETF_POOL}
    q["XLE"]["pre_price"], q["XLV"]["pre_price"], q["SMH"]["pre_price"] = 92.0, 106.0, 104.0
    picks = g.pick_stocks(DAY, 5, q)
    assert {"XLE", "XLV", "SMH"} <= set(picks) and len(set(picks)) == 5 and "XLK" not in picks
    assert picks == g.pick_stocks(DAY, 5, q)                  # deterministic for the day
    assert len(g.pick_stocks(DAY, 5, {})) == 5                # no pre-market prices: random


def test_round_uses_movers_and_one_quote_call():
    game, store, tg, q, calls, clock = make()
    for t in g.ETF_POOL:
        q.set(t, pre=100.5)
    q.set("XLE", pre=93.0)
    seen = []
    real = q.__call__
    game.quotes = lambda ts: seen.append(len(ts)) or real(ts)
    game.tick(ny(DAY, 7, 30))
    assert seen == [len(g.ETF_POOL)] and "XLE" in tickers(store)


def test_whole_market_line_and_extras_are_off_by_default():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    assert "is off for this group" in game.on_command("/halfway", 1, "A", ny(DAY, 8))[0]
    game.tick(ny(DAY, 9, 30))
    assert "Halfway update" not in tg.sent[-1][0]
    for t in ts:
        q.set(t, open_=100.0, last=99.0, when=ny(DAY, 11))
    game.tick(ny(DAY, 11, 0))
    assert not any(t.startswith("⏱") for t, _ in tg.sent)                  # halfway update is off
    q.set("SPY", open_=500.0, last=495.0, prev_close=502.0, when=ny(DAY, 16))
    settle_day(game, q, ts, DAY, [99.0, 98.0, 101.0])
    res = next(t for t, _ in reversed(tg.sent) if t.startswith("🏁"))
    assert "🌍 The whole US market (SPY) went -1.0% today. Most of today's picks went the same way." in res
    assert "Moved the most" not in res
    game.tick(dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    assert not getattr(tg, "polls", [])                                     # quiz is off


def test_biggest_mover_void_when_a_price_is_missing():
    stocks = [{"ticker": "A", "result": "UP", "open": 1.0, "close": 1.1},
              {"ticker": "B", "result": "VOID", "open": None, "close": None}]
    assert g.biggest_mover(stocks) == "VOID"
    stocks[1].update(result="FLAT", open=1.0, close=1.0)
    assert g.biggest_mover(stocks) == "A"


def test_two_groups_share_the_round_with_their_own_boards():
    game, store, tg, q, calls, clock = make(chats=(-1, -2))
    ts = full_day(game, store, tg, q, clock)
    assert sorted(tg.to) == [-2, -1] and store.post_id(DAY.isoformat(), -2)
    b = lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat())))
    for row in b:
        game.on_button(row[0][1], 7, "Ann", ny(DAY, 9), chat_id=-1)
        game.on_button(row[1][1], 8, "Ben", ny(DAY, 9), chat_id=-2)
    assert game.voters(DAY.isoformat(), -1) == 1 and game.voters(DAY.isoformat()) == 2
    settle_day(game, q, ts, DAY, [101.0, 102.0, 103.0])
    res = {chat: text for text, chat in zip([t for t, _ in tg.sent], tg.to) if text.startswith("🏁")}
    assert "Ann" in res[-1] and "Ben" not in res[-1]
    assert "Ben" in res[-2] and "Ann" not in res[-2]
    assert sum(t.startswith("🔒") for t, _ in tg.sent) == 2


def test_late_group_gets_the_open_round_and_removed_group_is_dropped():
    game, store, tg, q, calls, clock = make()
    full_day(game, store, tg, q, clock)
    assert game.join(-5, "Latecomers", ny(DAY, 8)) and "Huat or Not?" in tg.sent[-1][0]   # welcome
    game.tick(ny(DAY, 8, 5))
    assert store.post_id(DAY.isoformat(), -5) and tg.to[-1] == -5

    def forbidden(*a, **k):
        raise g.Forbidden("bot was kicked")
    tg.send = forbidden
    game.tick(ny(DAY, 9, 20))                                 # reminder to -100 fails with 403
    assert not store.chat(CHAT)["active"]


def test_group_cap_and_events():
    from btb.__main__ import handle
    game, store, tg, q, calls, clock = make()
    game.max_groups = 2
    assert game.join(-2, "Two", ny(DAY, 8))
    assert not game.join(-3, "Three", ny(DAY, 8)) and tg.left == [-3] and "full" in tg.sent[-1][0]
    handle(game, tg, {"my_chat_member": {"chat": {"id": -2, "type": "group", "title": "Two"},
                                         "new_chat_member": {"status": "kicked"}}}, ny(DAY, 8))
    assert not store.chat(-2)["active"]
    handle(game, tg, {"message": {"chat": {"id": CHAT, "type": "group"}, "migrate_to_chat_id": -1009}}, ny(DAY, 8))
    assert store.chat(-1009) and not store.chat(CHAT)
    handle(game, tg, {"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": "/start"}},
           ny(DAY, 8))
    assert tg.to[-1] == 42 and "Add me to a group" in tg.sent[-1][0]
    handle(game, tg, {"message": {"chat": {"id": -1009, "type": "supergroup", "title": "Renamed"},
                                  "from": {"id": 7, "first_name": "Ann"}, "text": "/help@HuatOrNotBot"}}, ny(DAY, 8))
    assert store.chat(-1009)["title"] == "Renamed" and 7 in store.members(-1009)


def test_bot_record_and_personal_card():
    game, store, tg, q, calls, clock = make()
    assert "has no results yet" in game.on_command("/bot", 7, "Ann", ny(DAY, 8))[0]
    ts = full_day(game, store, tg, q, clock)
    for row in lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat()))):
        game.on_button(row[1][1], 7, "Ann", ny(DAY, 9))
    settle_day(game, q, ts, DAY, [96.0, 101.0, 99.0])         # the AI said UP on all
    text = game.on_command("/bot", 7, "Ann", ny(DAY, 17))[0]
    assert "Worst picks" in text and f"❌ {ts[0]} ⬆️" in text and "went -4.0%" in text and "Best pick" in text
    assert "Players doing better than it: 1 of 1" in text
    card = game.on_command("/card", 7, "Ann", ny(DAY, 17))
    assert card["photo"][:4] == b"\x89PNG" and card["caption"].startswith("Ann beat Ah Huat")
    assert "no scores" in game.on_command("/card", 9, "Zed", ny(DAY, 17))[0]


def test_week_card_and_league():
    fri = dt.date(2026, 11, 20)
    game, store, tg, q, calls, clock = make(clock_at=ny(fri, 7, 30), chats=(-1, -2, -3))
    game.tick(ny(fri, 7, 30))
    ts = tickers(store, fri)
    b = lean_rows(game.buttons(fri, store.stocks(fri.isoformat())))
    for chat, users in ((-1, (1, 2)), (-2, (3, 4)), (-3, (5, 6))):
        for u in users:
            for row in b:
                game.on_button(row[0 if chat == -1 else 1][1], u, f"P{u}", ny(fri, 9), chat_id=chat)
    for chat in (-1, -2):
        assert "joined the league" in game.on_command("/league join", 1, "P1", ny(fri, 9), chat_id=chat)[0]
    settle_day(game, q, ts, fri, [101.0, 102.0, 103.0])
    res = {chat: text for text, chat in zip([t for t, _ in tg.sent], tg.to) if text.startswith("🏁")}
    assert "Groups this week" in res[-1] and "1. group-1: 9.0 (2 players)" in res[-1] and "2. group-2" in res[-1]
    assert "group-3" not in res[-1]                           # never joined: not shown
    assert {p[0] for p in tg.photos} == {-1, -2, -3} and all(p[1][:4] == b"\x89PNG" for p in tg.photos)
    text = game.on_command("/league", 5, "P5", ny(fri, 17), chat_id=-3)[0]
    assert "isn't in the league" in text and "group-1" in text
    assert "left the league" in game.on_command("/league leave", 1, "P1", ny(fri, 17), chat_id=-2)[0]


def test_single_group_database_migrates_to_groups(tmp_path):
    path = tmp_path / "live.db"
    old = Store(path)
    old.db.execute("INSERT INTO rounds (day, status, message_id, created_at) VALUES ('2026-11-23', 'open', 77, 'x')")
    old.player(7, "Ann")
    old.vote("2026-11-23", "AAPL", 7, "UP", "x")
    old.db.commit()
    new = Store(path, home_chat="-100")
    assert new.post_id("2026-11-23", -100) == 77 and new.members(-100) == {7}
    assert [c["chat_id"] for c in new.chats()] == [-100]


def test_practice_limits():
    game, store, tg, q, calls, clock = make(chats=tuple(range(-1, -9, -1)))
    for t in g.POOL:
        q.set(t, open_=100.0, last=101.0, when=ny(DAY, 16))
    now = ny(DAY, 20)
    game.on_command("/practice", 1, "A", now, chat_id=-1)
    game.practice_reveal(now + dt.timedelta(minutes=3))
    assert "Practice again in 7 min" in game.on_command("/practice", 1, "A", now + dt.timedelta(minutes=3), chat_id=-1)[0]
    assert "Practice game" in game.on_command("/practice", 1, "A", now + dt.timedelta(minutes=10), chat_id=-1)[0]
    for c in range(-2, -7, -1):                               # 6 practices with the AI within the hour...
        game.on_command("/practice", 1, "A", now + dt.timedelta(minutes=11), chat_id=c)
    n = len(calls)
    game.on_command("/practice", 1, "A", now + dt.timedelta(minutes=12), chat_id=-7)
    assert len(calls) == n                                    # ...then the AI sits practice out
    p = game._practice(-7)
    assert p and all(s["ai_call"] is None for s in p["stocks"])


def test_redact_token():
    from btb.telegram import redact
    tok = "123456789:" + "A" * 35
    msg = f"HTTPSConnectionPool: Max retries exceeded with url: /bot{tok}/getUpdates"
    assert tok not in redact(msg, tok) and "<token>" in redact(msg, tok)
    assert tok not in redact(msg, "")                         # pattern fallback


def test_send_raises_with_telegram_reason():
    from btb.telegram import Telegram

    class Session:
        def post(self, url, json=None, timeout=None):
            class R:
                status_code = 400

                def json(self):
                    return {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
            return R()

    with pytest.raises(RuntimeError, match="chat not found"):
        Telegram("t", "1", session=Session()).send("x")


def test_command_menu():
    from btb.telegram import Telegram

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, timeout=None):
            self.calls.append((url.rsplit("/", 1)[1], json))

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True}
            return R()

    sess = Session()
    assert Telegram("t", "1", session=sess).set_commands(g.COMMANDS)
    assert [c[0] for c in sess.calls] == ["setMyCommands"] * 2
    assert {c[1]["scope"]["type"] for c in sess.calls} == {"default", "all_group_chats"}
    names = [c["command"] for c in sess.calls[0][1]["commands"]]
    assert all(n.islower() and len(n) <= 32 for n in names) and "practice" in names


# ---------------------------------------------------------------- round 4: decision skills

def test_decision_reports():
    fri = dt.date(2026, 11, 20)
    game, store, tg, q, calls, clock = make(clock_at=ny(fri, 7, 30))
    game.tick(ny(fri, 7, 30))
    ts = tickers(store, fri)
    b = choices(game.buttons(fri, store.stocks(fri.isoformat())))
    game.on_button(b[0][0][1], 7, "Ann", ny(fri, 9))                         # #1 sure up
    game.on_button(b[1][0][1], 7, "Ann", ny(fri, 9))                         # #2 sure up
    game.on_button(b[2][1][1], 7, "Ann", ny(fri, 9))                         # #3 think so up
    game.on_button(b[0][2][1], 8, "Ben", ny(fri, 9))                         # Ben plays 1 stock: skips 2
    q.set("SPY", open_=500.0, last=505.0, prev_close=490.0, when=ny(fri, 16))
    settle_day(game, q, ts, fri, [101.0, 99.0, 101.0])                       # sure 1/2, think so 1/1
    report = next(t for t, _ in tg.sent if t.startswith("📊"))
    assert "Just holding the whole US market (SPY) this week: +3.1%" in report
    assert "<b>Ann</b>" in report and "When very sure: right 1/2 (50%) · ⚠️ too confident" in report
    assert "When 'think so': right 1/1 (100%) · ✅" in report
    assert "Points per pick: -1.0. Random guessing with the same buttons would average -2.2: better than guessing" in report
    assert "<b>Ben</b>" not in report                                         # under 3 picks: not reported
    mine = game.on_command("/report", 8, "Ben", ny(fri, 17))[0]
    assert "When 'think so': right 0/1" in mine and "Skipped 2 stocks" in mine and "no better than guessing" in mine
    assert "no results yet" in game.on_command("/report", 9, "Zed", ny(fri, 17))[0]
    bot = game.on_command("/bot", 7, "Ann", ny(fri, 17))[0]
    assert "When it said <b>think so</b>: right 2/3 (67%)" in bot and "sounding sure doesn't make someone right" in bot


def test_crowd_split_contrarian_and_group_brain():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    b = lean_rows(game.buttons(DAY, store.stocks(DAY.isoformat())))
    for u in (1, 2, 3):
        for row in b:
            game.on_button(row[0][1], u, f"U{u}", ny(DAY, 9))                 # three say UP everywhere
    for row in b:
        game.on_button(row[1][1], 4, "Rebel", ny(DAY, 9))                     # one says DOWN everywhere
    game.tick(ny(DAY, 9, 30))
    assert tg.sent[-1][0].count("group ⬆️ 3 · ⬇️ 1") == 3
    settle_day(game, q, ts, DAY, [99.0, 101.0, 101.0])                        # #1 went DOWN
    res = next(t for t, _ in reversed(tg.sent) if t.startswith("🏁"))
    assert f"🦅 Right when most people were wrong: Rebel on {ts[0]}" in res
    assert "The group together got 2 of 3 right, Ah Huat got 2 of 3: a draw" in res
    assert "Most of the group got" in res or "📚" in res


def test_fallback_lessons():
    stocks = [{"ticker": "MU", "pre_price": 91.0, "prev_close": 100.0, "open": 90.0, "close": 95.0, "result": "UP"}]
    assert "turned around" in g.fallback_lesson(stocks, {}) and "-9.0% before the market opened" in g.fallback_lesson(stocks, {})
    stocks = [{"ticker": "KO", "pre_price": 100.1, "prev_close": 100.0, "open": 100.0, "close": 99.0, "result": "DOWN"}]
    assert "Most of the group got KO wrong" in g.fallback_lesson(stocks, {"KO": {"UP": 4, "DOWN": 1}})
    assert g.fallback_lesson(stocks, {}) in g.LESSONS


def test_model_lesson_and_why():
    seen = {}
    game, store, tg, q, calls, clock = make()
    game.explain = lambda rows: seen.setdefault("why", rows) and {rows[0]["ticker"]: "Earnings beat lifted it."}
    game.teach = lambda summary: seen.setdefault("lesson", summary) and "Gaps fade as often as they run."
    ts = full_day(game, store, tg, q, clock)
    settle_day(game, q, ts, DAY, [101.0, 99.0, 102.0])
    res, buttons = tg.sent[-1]
    assert "📚 <b>Tip:</b> Gaps fade as often as they run." in res
    assert seen["why"][0]["headlines"] == [f"{ts[0]} news"] and abs(seen["why"][0]["move"] - 0.01) < 1e-9
    assert game.on_button(buttons[0][0][1], 7, "Ann", ny(DAY, 17)) == "Earnings beat lifted it."
    assert "no clear news" in game.on_button(buttons[0][1][1], 7, "Ann", ny(DAY, 17))


def test_quiz_bank_fits_telegram_limits():
    from btb import quiz
    assert len(quiz.BANK) >= 24
    for question, options, right, explain in quiz.BANK:
        assert len(question) <= 300 and 2 <= len(options) <= 10 and 0 <= right < len(options)
        assert all(len(o) <= 100 for o in options) and len(explain) <= 200 and len(set(options)) == len(options)
    weeks = [quiz.week_key(dt.date(2026, 11, 28) + dt.timedelta(weeks=i)) for i in range(6)]
    picks = [i for w in weeks for i in quiz.pick(w)]
    assert len(set(picks)) == len(picks)                                       # no repeats within 6 weeks


def test_quiz_game_question():
    from btb import quiz
    moves = [("2026-11-23", "MU", 0.09), ("2026-11-23", "KO", 0.002), ("2026-11-24", "AAPL", -0.01),
             ("2026-11-24", "TSLA", -0.03), ("2026-11-25", "NVDA", 0.02)]
    q_, options, right, explain = quiz.game_question(moves)
    assert options[right] == "MU" and len(options) == 4 and "+9.0%" in explain
    assert quiz.game_question(moves[:3]) is None


def test_weekend_quiz_flow():
    sg = dt.timezone(dt.timedelta(hours=8))
    game, store, tg, q, calls, clock = make()
    game.on_command("/quiz on", 1, "A", ny(DAY, 7))
    ts = full_day(game, store, tg, q, clock)                                  # a settled round this week
    settle_day(game, q, ts, DAY, [101.0, 99.0, 102.0])
    game.tick(dt.datetime(2026, 11, 28, 9, 59, tzinfo=sg))
    assert not getattr(tg, "polls", [])
    game.tick(dt.datetime(2026, 11, 28, 10, 0, tzinfo=sg))
    assert len(tg.polls) == 4 and "Weekend quiz" in tg.sent[-1][0]            # 4 from the bank (too few moves for 5)
    game.tick(dt.datetime(2026, 11, 28, 11, 0, tzinfo=sg))
    assert len(tg.polls) == 4                                                  # once per weekend
    from btb.__main__ import handle
    right = tg.polls[0][3]
    handle(game, tg, {"poll_answer": {"poll_id": "poll1", "user": {"id": 7, "first_name": "Ann"}, "option_ids": [right]}},
           dt.datetime(2026, 11, 28, 12, tzinfo=sg))
    handle(game, tg, {"poll_answer": {"poll_id": "poll2", "user": {"id": 7, "first_name": "Ann"}, "option_ids": [9]}},
           dt.datetime(2026, 11, 28, 12, tzinfo=sg))
    handle(game, tg, {"poll_answer": {"poll_id": "nope", "user": {"id": 8, "first_name": "Ben"}, "option_ids": [0]}},
           dt.datetime(2026, 11, 28, 12, tzinfo=sg))
    game.tick(dt.datetime(2026, 11, 29, 19, 59, tzinfo=sg))
    assert not tg.sent[-1][0].startswith("🧩 <b>Weekend quiz scores")
    game.tick(dt.datetime(2026, 11, 29, 20, 0, tzinfo=sg))
    assert "🥇 Ann: 1/2" in tg.sent[-1][0] and 7 in store.members(CHAT)
    game.tick(dt.datetime(2026, 11, 29, 20, 30, tzinfo=sg))
    assert sum("quiz scores" in t for t, _ in tg.sent) == 1


def test_why_answers_are_alerts():
    from btb.__main__ import handle
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    settle_day(game, q, ts, DAY, [101.0, 99.0, 102.0])
    handle(game, tg, {"callback_query": {"id": "x", "data": f"w|20261123|{ts[0]}", "from": {"id": 7, "first_name": "A"},
                                         "message": {"chat": {"id": CHAT, "type": "group"}}}}, ny(DAY, 17))
    assert tg.alerts[-1] is True and ts[0] in tg.answers[-1]


def test_open_round_switches_to_new_rules_on_start():
    game, store, tg, q, calls, clock = make()
    full_day(game, store, tg, q, clock)
    key, t = DAY.isoformat(), tickers(store)[0]
    store.player(7, "Ann")
    store.vote(key, t, 7, "UP", "x")                       # an old-rules pick (no confidence), doubled
    store.set_double(key, 7, t)
    game.refresh_open_rounds()
    v = store.votes(key)[0]
    assert (v["conf"], v["double"]) == ("lean", 0)
    assert tg.edits and "not sure: skip" in tg.edits[-1][1]
    assert "Unknown" in game.on_button(f"d|20261123|{t}", 7, "Ann", ny(DAY, 9))   # double down is gone


# ---------------------------------------------------------------- own picks

def test_own_pick_during_the_session():
    game, store, tg, q, calls, clock = make()
    q.set("NVDA", open_=100.0, last=102.0, prev_close=101.0, when=ny(DAY, 11))
    text, buttons = game.on_command("/pick nvda", 7, "Ann", ny(DAY, 11))
    assert "Own pick: NVDA" in text and "Starts <b>now</b>" in text and "+1.0% today" in text
    assert [cb for _, cb in buttons[0]] == ["o|NVDA|U|s", "o|NVDA|U|l", "o|NVDA|D|l", "o|NVDA|D|s"]
    toast = game.on_button("o|NVDA|U|s", 7, "Ann", ny(DAY, 11, 1))
    assert "from $102.00 now until today's close" in toast and "+5 if right, -11 if wrong" in toast
    assert "already have a NVDA pick" in game.on_button("o|NVDA|D|l", 7, "Ann", ny(DAY, 11, 2))
    assert "Lean" not in game.on_button("o|NVDA|D|l", 8, "Ben", ny(DAY, 11, 2))   # others can join
    q.set("NVDA", open_=100.0, last=104.0, prev_close=101.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 9))
    assert not any(t.startswith("📌 <b>Own picks results") for t, _ in tg.sent)
    game.tick(ny(DAY, 16, 10))
    res = tg.sent[-1][0]
    assert res.startswith("📌 <b>Own picks results") and "✅ Ann: NVDA ⏫ 102.00 → 104.00 (+2.0%) · 5 pts" in res
    assert "❌ Ben: NVDA ⬇️" in res and "-4 pts" in res
    game.tick(ny(DAY, 16, 30))
    assert sum(t.startswith("📌 <b>Own picks results") for t, _ in tg.sent) == 1
    assert "📌 Own picks: 5 pts, right 1 of 1" in game.on_command("/me", 7, "Ann", ny(DAY, 17))[0]
    assert "Own picks this week" in game.on_command("/board", 7, "Ann", ny(DAY, 17))[0]
    assert store.scores() == []                                                # separate from the daily game


def test_own_pick_before_the_open_waits_for_it_and_can_be_cancelled():
    game, store, tg, q, calls, clock = make()
    q.set("KO", last=60.0, prev_close=60.0, when=ny(DAY - dt.timedelta(days=3), 16))
    text, _ = game.on_command("/pick $KO", 7, "Ann", ny(DAY, 8))
    assert "next market open: <b>Mon 23 Nov, 22:30 Singapore time</b>" in text
    assert "from the open on Mon 23 Nov" in game.on_button("o|KO|D|l", 7, "Ann", ny(DAY, 8))
    assert "cancelled" in game.on_button("o|KO|D|l", 7, "Ann", ny(DAY, 8, 1))
    game.on_button("o|KO|D|l", 7, "Ann", ny(DAY, 8, 2))
    q.set("KO", open_=61.0, last=60.5, prev_close=60.0, when=ny(DAY, 9, 40))
    game.tick(ny(DAY, 9, 40))
    assert store.own()[0]["start_price"] == 61.0
    assert "already have" in game.on_button("o|KO|D|l", 7, "Ann", ny(DAY, 9, 41))   # started: can't cancel
    q.set("KO", open_=61.0, last=59.0, prev_close=60.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 10))
    assert "✅ Ann: KO ⬇️ 61.00 → 59.00" in tg.sent[-1][0]


def test_own_pick_timing_and_limits():
    game, store, tg, q, calls, clock = make()
    assert game.own_start(ny(DAY, 15, 31)) == ("open", dt.date(2026, 11, 24))   # last 30 minutes: next session
    assert game.own_start(ny(DAY, 15, 29)) == ("now", DAY)
    assert game.own_start(ny(dt.date(2026, 11, 25), 17)) == ("open", dt.date(2026, 11, 27))  # Thanksgiving skipped
    assert game.own_start(ny(dt.date(2026, 11, 28), 12)) == ("open", dt.date(2026, 11, 30))  # weekend
    for t in ("AAPL", "MSFT", "TSLA", "AMD"):
        q.set(t, last=10.0, prev_close=10.0, when=ny(DAY, 12))
    for t in ("AAPL", "MSFT", "TSLA"):
        game.on_button(f"o|{t}|U|l", 7, "Ann", ny(DAY, 12))
    assert "3 own picks for today" in game.on_button("o|AMD|U|l", 7, "Ann", ny(DAY, 12))
    assert "Unknown" in game.on_button("o|AMD|X|l", 8, "Ben", ny(DAY, 12))
    assert "can't find ZZZZ" in game.on_command("/pick ZZZZ", 7, "Ann", ny(DAY, 12))[0]
    assert "doesn't look like" in game.on_command("/pick hello123", 7, "Ann", ny(DAY, 12))[0]
    assert "/pick NVDA" in game.on_command("/pick", 7, "Ann", ny(DAY, 12))[0]


def test_own_pick_without_a_captured_open_is_void():
    game, store, tg, q, calls, clock = make()
    q.set("KO", last=60.0, prev_close=60.0, when=ny(DAY - dt.timedelta(days=3), 16))
    game.on_button("o|KO|U|l", 7, "Ann", ny(DAY, 8))
    nxt = DAY + dt.timedelta(days=1)
    q.set("KO", open_=62.0, last=63.0, prev_close=61.0, when=ny(nxt, 10))      # bot was down all of the 23rd
    game.tick(ny(nxt, 10))
    assert store.own()[0]["result"] == "VOID" and "no result" in tg.sent[-1][0]



def test_my_picks_popup_and_toast():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    b = game.buttons(DAY, store.stocks(DAY.isoformat()))
    mine = b[-1][0][1]
    b = choices(b)
    assert game.is_alert(mine)
    assert "haven't picked" in game.on_button(mine, 7, "Ann", ny(DAY, 9))
    toast = game.on_button(b[0][1][1], 7, "Ann", ny(DAY, 9))
    assert toast.startswith(f"✅ {ts[0]} ⬆️ think so up (+3 / -4). Your picks: {ts[0]} ⬆️ · {ts[1]} – · {ts[2]} –")
    game.on_button(b[1][3][1], 7, "Ann", ny(DAY, 9))
    pop = game.on_button(mine, 7, "Ann", ny(DAY, 9, 1))
    assert f"{ts[0]} ⬆️ think so" in pop and f"{ts[1]} ⏬ very sure" in pop and f"{ts[2]}: skipped" in pop
    assert "Change until 22:30" in pop and len(pop) <= 200
    assert "cancelled. Your picks:" in game.on_button(b[0][1][1], 7, "Ann", ny(DAY, 9, 2))
    game.on_button(b[0][1][1], 7, "Ann", ny(DAY, 9, 3))
    settle_day(game, q, ts, DAY, [101.0, 99.0, 101.0])
    pop = game.on_button(mine, 7, "Ann", ny(DAY, 17))
    assert f"{ts[0]} ⬆️ think so ✅ 3 pts" in pop and f"{ts[1]} ⏬ very sure ✅ 5 pts" in pop and "Change" not in pop



def test_funds_come_from_different_industries():
    q = {t: {"pre_price": 100.1, "prev_close": 100.0} for t in g.ETF_POOL}
    q["SMH"]["pre_price"], q["XLK"]["pre_price"] = 95.0, 96.0           # chips and tech move together
    picks = g.pick_stocks(DAY, 5, q)
    assert "SMH" in picks and "XLK" not in picks and len({g.ETF_GROUP[t] for t in picks}) == 5
    for d in range(60):
        assert len({g.ETF_GROUP[t] for t in g.pick_tickers(DAY + dt.timedelta(days=d), 5)}) == 5
    assert set(g.ETF_POOL) == set(g.ETF_LABEL) == set(g.ETF_NEWS) == set(g.ETF_GROUP)
    assert set(g.POOL) == set(g.SECTOR) and len(g.POOL) == len(set(g.POOL))   # stocks stay for practice


def test_suggestions_join_the_next_game():
    game, store, tg, q, calls, clock = make()
    for t in ("NVDA", "TSLA", "KO"):
        q.set(t, last=100.0, prev_close=99.0, pre=101.0, when=ny(DAY - dt.timedelta(days=3), 16))
    assert "/suggest NVDA" in game.on_command("/suggest", 7, "Ann", ny(DAY, 6))[0]
    reply = game.on_command("/suggest nvda", 7, "Ann", ny(DAY, 6))[0]
    assert "suggested <b>NVDA</b>" in reply and "Mon 23 Nov" in reply and "NVDA (1)" in reply
    game.on_command("/suggest KO", 8, "Ben", ny(DAY, 6))
    game.on_command("/suggest NVDA", 8, "Ben", ny(DAY, 6, 1))                # Ben changes his mind: 1 each
    game.on_command("/suggest TSLA", 9, "Cat", ny(DAY, 6, 2))
    assert "NVDA (2) · TSLA (1)" in game.on_command("/suggest", 9, "Cat", ny(DAY, 6, 3))[0]
    assert "can't find" in game.on_command("/suggest ZZZZ", 9, "Cat", ny(DAY, 6))[0]
    assert "Tue 24 Nov" in game.on_command("/suggest KO", 9, "Cat", ny(DAY, 8))[0]   # after 07:30: next game
    game.tick(ny(DAY, 7, 30))
    ts = tickers(store)
    assert ts[:2] == ["NVDA", "TSLA"] and ts[2] in g.ETF_POOL                 # 3 per day in tests: 2 ideas + 1 fund
    text = tg.sent[0][0]
    assert "💡 suggested by Ann, Ben: <b>NVDA</b>" in text and "💡 suggested by Cat: <b>TSLA</b>" in text
    assert "except the stocks players added" in text


def test_funds_get_industry_news():
    from btb import news

    class Resp:
        status_code = 200
        text = ("<rss><channel>"
                "<item><title>Oil jumps as OPEC cuts output - Reuters</title><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate></item>"
                "<item><title>Celebrity wedding news - Gossip</title><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate></item>"
                "</channel></rss>")

    class Session:
        def get(self, url, headers=None, timeout=None):
            assert "oil%20prices" in url
            return Resp()

    got = news.topic(*g.ETF_NEWS["XLE"], now=dt.datetime(2026, 11, 23, 12, tzinfo=dt.timezone.utc), session=Session())
    assert [h["title"] for h in got] == ["Oil jumps as OPEC cuts output - Reuters"]



def test_intro_matches_what_is_in_the_round():
    fund = {"ticker": "XLE"}
    stock = {"ticker": "AMZN"}
    assert "whole industry" in g.Game.what_lines([fund, fund])
    assert "players added" in g.Game.what_lines([stock, fund])
    assert g.Game.what_lines([stock, stock]) == ""



def test_add_a_stock_to_todays_game():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    q.set("NVDA", last=180.0, prev_close=176.0, pre=180.0, when=ny(DAY - dt.timedelta(days=3), 16))
    text, row = game.on_command("/add nvda", 7, "Ann", ny(DAY, 8))
    assert "Ann added <b>NVDA</b>" in text and "Skip it" in text and [cb for _, cb in row[1]][0] == "v|20261123|NVDA|U|s"
    assert row[0][0][1] == "i|20261123|NVDA"
    stocks = store.stocks(DAY.isoformat())
    assert [s["ticker"] for s in stocks] == ts + ["NVDA"] and stocks[-1]["note"] == "➕ added by Ann"
    assert stocks[-1]["ai_call"] == "UP"                                       # the AI picks it too
    assert "➕ added by Ann: <b>NVDA</b>" in tg.edits[-1][1]   # game message updated
    assert "already in today's game" in game.on_command("/add NVDA", 8, "Ben", ny(DAY, 8))[0]
    for t in ("TSLA", "KO"):
        q.set(t, last=10.0, prev_close=10.0, when=ny(DAY - dt.timedelta(days=3), 16))
        game.on_command(f"/add {t}", 8, "Ben", ny(DAY, 8))
    q.set("AAPL", last=10.0, prev_close=10.0, when=ny(DAY - dt.timedelta(days=3), 16))
    assert "already has 3 added" in game.on_command("/add AAPL", 8, "Ben", ny(DAY, 8))[0]
    # picking an added stock is optional: the reminder only counts the main stocks
    store.player(9, "Cat"); store.member(CHAT, 9); store.vote("2026-11-20", "XLK", 9, "UP", "x")
    for r in choices(game.buttons(DAY, store.stocks(DAY.isoformat())))[:3]:
        game.on_button(r[1][1], 9, "Cat", ny(DAY, 9))
    game.on_button(row[1][1][1], 8, "Ben", ny(DAY, 9))                       # Ben has an opinion on NVDA
    game.tick(ny(DAY, 9, 20))
    assert "Cat" not in tg.sent[-1][0]
    game.tick(ny(DAY, 9, 30))
    assert "<b>NVDA</b> group ⬆️ 1" in tg.sent[-1][0]
    assert "picks are closed" in game.on_command("/add AAPL", 7, "Ann", ny(DAY, 9, 31))[0]
    for t in ts + ["NVDA", "TSLA", "KO"]:
        q.set(t, open_=100.0, last=100.0, when=ny(DAY, 9, 36))
    settle_day(game, q, ts + ["NVDA", "TSLA", "KO"], DAY, [101.0, 101.0, 101.0, 102.0, 99.0, 99.0])
    assert "NVDA" in next(t for t, _ in reversed(tg.sent) if t.startswith("🏁"))
    ben = next(r for r in store.scores() if r["user_id"] == 8)
    assert (ben["points"], ben["played"]) == (3, 1)


def test_add_before_the_game_is_out_counts_as_suggestion():
    game, store, tg, q, calls, clock = make()
    q.set("NVDA", last=180.0, prev_close=176.0, when=ny(DAY - dt.timedelta(days=3), 16))
    reply = game.on_command("/add NVDA", 7, "Ann", ny(DAY, 6))[0]
    assert "suggested <b>NVDA</b>" in reply and "counts as a suggestion" in reply



def test_info_popup_before_the_open():
    game, store, tg, q, calls, clock = make()
    game.summarize = lambda name, heads: "Big banks slip as hopes for rate cuts fade"
    ts = full_day(game, store, tg, q, clock)
    t = ts[0]
    q.set(t, last=50.4, prev_close=50.0, pre=50.15, when=ny(DAY - dt.timedelta(days=3), 16), high52=51.0, low52=35.0)
    q.set("SPY", last=600.0, prev_close=598.0, pre=597.0, when=ny(DAY - dt.timedelta(days=3), 16))
    b = game.buttons(DAY, store.stocks(DAY.isoformat()))
    pop = game.on_button(b[0][0][1], 7, "Ann", ny(DAY, 8, 30))
    assert len(pop) <= 200 and f"({t})" in pop.splitlines()[0]
    assert "Inside: " + g.ETF_LABEL[t].split(" (", 1)[1].rstrip(")") in pop
    assert "Before the open: -0.5% (whole market -0.5%)" in pop                # against the LATEST close (50.40)
    assert "Yesterday: +0.8% · near its highest this year" in pop
    assert "News: Big banks slip as hopes for rate cuts fade" in pop
    q.fail = True                                                              # cached for a minute: no new fetch
    assert game.on_button(b[0][0][1], 8, "Ben", ny(DAY, 8, 30, 30)) == pop
    assert "News:" in game.on_button(b[2][0][1], 8, "Ben", ny(DAY, 8, 30, 30))            # fails soft


def test_info_popup_during_the_session_and_fallback_news():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    t = ts[1]
    store.update_stock(DAY.isoformat(), t, headlines=json.dumps([{"title": "Thị trường chứng khoán giảm mạnh - Laodong", "published": None}]))
    q.set(t, open_=100.0, last=102.0, prev_close=100.0, when=ny(DAY, 11), high52=150.0, low52=90.0)
    q.set("SPY", open_=600.0, last=603.0, prev_close=600.0, when=ny(DAY, 11))
    b = game.buttons(DAY, store.stocks(DAY.isoformat()))
    pop = game.on_button(b[2][0][1], 7, "Ann", ny(DAY, 11))
    assert "Today so far: +2.0% (whole market +0.5%)" in pop and "News: No big news today." in pop
    assert g.clean_headline("Oil jumps as OPEC cuts - Reuters") == "Oil jumps as OPEC cuts"
    assert g.Game.year_position({"high52": 150.0, "low52": 90.0, "last": 120.0}) == " · 20% below its 1-year high"
    assert g.Game.year_position({"high52": 150.0, "low52": 90.0, "last": 91.0}) == " · near its lowest this year"


def test_news_lines_written_when_the_game_is_made():
    seen = []
    game, store, tg, q, calls, clock = make()
    game.summarize = lambda name, heads: seen.append(name) or f"{name}: calm day"
    full_day(game, store, tg, q, clock)
    assert len(seen) == 3 and all(s["news_line"].endswith("calm day") for s in store.stocks(DAY.isoformat()))
    game.refresh_open_rounds()                                                 # already written: not asked again
    assert len(seen) == 3



def test_moves_before_the_open_use_the_latest_close():
    from btb import market
    friday_close = {"last": 342.56, "prev_close": 349.21, "pre_price": 348.39,
                    "update_time": dt.datetime(2026, 11, 20, 16, 0, tzinfo=NY)}   # Monday pre-market snapshot
    assert market.ref_close(friday_close, DAY) == 342.56
    assert round(g.pre_move(friday_close, DAY), 4) == round(348.39 / 342.56 - 1, 4)  # +1.7%, not -0.2%
    one_second_early = dict(friday_close, update_time=dt.datetime(2026, 11, 20, 15, 59, 59, tzinfo=NY))
    assert market.ref_close(one_second_early, DAY) == 342.56                         # an earlier day: session over
    intraday = dict(friday_close, update_time=dt.datetime(2026, 11, 23, 11, 0, tzinfo=NY))
    assert market.ref_close(intraday, DAY) == 349.21                                # during a session: prev_close
    half_day = dict(friday_close, update_time=dt.datetime(2026, 11, 27, 13, 0, tzinfo=NY))
    assert market.ref_close(half_day, dt.date(2026, 11, 27)) == 342.56


def test_more_details_in_private_chat():
    from btb.__main__ import handle
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    t = ts[0]
    days = [DAY - dt.timedelta(days=i) for i in range(30, 0, -1) if (DAY - dt.timedelta(days=i)).weekday() < 5]
    hist = [{"date": d, "open": 100.0, "close": 101.0 if i % 3 else 99.0} for i, d in enumerate(days)]
    game.history = lambda ticker: hist
    q.set(t, last=50.0, prev_close=49.0, pre=50.5, when=ny(DAY - dt.timedelta(days=3), 16), high52=60.0, low52=40.0)
    q.set("SPY", last=600.0, prev_close=598.0, pre=606.0, when=ny(DAY - dt.timedelta(days=3), 16))
    text, chart = game.details(DAY.isoformat(), t, ny(DAY, 8))
    assert "Before the open: +1.0% (whole market +1.0%)" in text and "A normal day (open to close): about ±1.0%" in text
    assert "Last 20 days: ended higher than it opened <b>13</b> times, lower 7 times" in text
    assert "Last 5 days:" in text and "17% below its 1-year high" in text and chart[:4] == b"\x89PNG"
    cq = {"id": "c", "data": f"x|20261123|{t}", "from": {"id": 7, "first_name": "Ann"},
          "message": {"chat": {"id": CHAT, "type": "group"}}}
    handle(game, tg, {"callback_query": cq}, ny(DAY, 8))
    assert tg.photos[-1][0] == 7 and "Sent to your private chat" in tg.answers[-1]

    def blocked(*a, **k):
        raise g.Forbidden("bot can't initiate conversation with a user")
    tg.send_photo = blocked
    game.bot_username = "HuatOrNotBot"
    urls = []
    tg.answer = lambda cid, text, alert=False, url=None: urls.append(url)
    handle(game, tg, {"callback_query": cq}, ny(DAY, 8))
    assert urls[-1] == f"https://t.me/HuatOrNotBot?start=more_20261123_{t}"
    tg.send_photo = lambda png, caption="", chat_id=None, html=False: tg.photos.append((chat_id, png, caption))
    handle(game, tg, {"message": {"chat": {"id": 7, "type": "private"}, "from": {"id": 7, "first_name": "Ann"},
                                  "text": f"/start more_20261123_{t}"}}, ny(DAY, 8))
    assert tg.photos[-1][0] == 7 and t in tg.photos[-1][2]


def test_more_details_without_history():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    game.history = lambda ticker: []
    text, chart = game.details(DAY.isoformat(), ts[0], ny(DAY, 8))
    assert chart is None and "normal day" not in text and "Just a game" in text



def test_set_todays_lines():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    for t in ("SMH", "NVDA", "XLF"):
        q.set(t, last=100.0, prev_close=99.0, pre=101.0, when=ny(DAY - dt.timedelta(days=3), 16))
    assert game.resolve(["chips"]) == (["SMH"], [])
    assert game.resolve(["NVDA", "industry"]) == (["SMH"], [])
    assert game.resolve(["chips", "banks", "NVDA"]) == (["SMH", "XLF", "NVDA"], [])
    assert game.resolve(["chips", "blah!"])[1] == ["blah!"]
    b = choices(game.buttons(DAY, store.stocks(DAY.isoformat())))
    game.on_button(b[0][1][1], 7, "Ann", ny(DAY, 8))                     # a pick on a line that will go
    keep = "SMH" if "SMH" in ts else None
    reply = game.on_command("/set NVDA industry", 7, "Ann", ny(DAY, 8, 10))[0]
    assert "changed today's game to: 🔌 Chip makers" in reply
    assert [s["ticker"] for s in store.stocks(DAY.isoformat())] == ["SMH"]
    assert ("1 pick was dropped from removed lines" in reply) == (ts[0] != "SMH")
    assert "Chip makers" in tg.edits[-1][1] or "SMH" in str(tg.edits[-1])
    reply = game.on_command("/set chips NVDA", 7, "Ann", ny(DAY, 8, 11))[0]
    assert [s["ticker"] for s in store.stocks(DAY.isoformat())] == ["SMH", "NVDA"]
    assert "can't change" in game.on_command("/set XLF", 7, "Ann", ny(DAY, 9, 31))[0]
    assert "set the game for Tue 24 Nov" in game.on_command("/set XLF", 7, "Ann", ny(DAY, 17))[0]   # after the close
    assert "don't know" in game.on_command("/set chips blah!", 7, "Ann", ny(DAY, 8))[0]


def test_set_before_the_game_is_posted_and_only_in_the_main_group():
    store, tg, q = Store(":memory:"), FakeTG(), FakeQuotes()
    store.add_chat(CHAT, "main", ""); store.add_chat(-200, "other", "")
    game = g.Game(Settings(stocks_per_day=3, chat_id=str(CHAT)), store, tg, quotes=q, news=lambda t, n: [],
                  call_ai=lambda *a: ("UP", "x", 0.5), ai_ready=lambda: True, log=lambda *_: None,
                  clock=lambda: ny(DAY, 7, 30))
    q.set("XLE", last=80.0, prev_close=79.0, pre=81.0, when=ny(DAY - dt.timedelta(days=3), 16))
    assert "Only the main group" in game.on_command("/set oil", 7, "Ann", ny(DAY, 6), chat_id=-200)[0]
    assert "posted at 20:30" in game.on_command("/set oil", 7, "Ann", ny(DAY, 6), chat_id=CHAT)[0]
    game.tick(ny(DAY, 7, 30))
    assert [s["ticker"] for s in store.stocks(DAY.isoformat())] == ["XLE"]
