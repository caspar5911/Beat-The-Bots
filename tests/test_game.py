import datetime as dt

import pytest

from btb import game as g, glm, market, news
from btb.__main__ import handle
from btb.config import NY, Settings
from btb.store import BOT_ID, BOT_NAME, Store

DAY = dt.date(2026, 11, 23)  # a Monday


def ny(day, h, m=0, s=0):
    return dt.datetime(day.year, day.month, day.day, h, m, s, tzinfo=NY)


class FakeTG:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []

    def send(self, text, buttons=None, chat_id=None):
        self.sent.append((text, buttons))
        return len(self.sent)

    def edit(self, mid, text, buttons=None):
        self.edits.append((mid, text))

    def answer(self, cid, text):
        self.answers.append(text)


class FakeQuotes:
    """Per-ticker snapshot rows; `raise_next` simulates OpenD being down."""
    def __init__(self):
        self.rows, self.fail = {}, False

    def set(self, t, open_=None, last=None, prev_close=100.0, pre=101.0, when=None):
        self.rows[t] = {"name": f"{t} Inc", "open": open_, "last": last, "prev_close": prev_close,
                        "pre_price": pre, "update_time": when}

    def __call__(self, tickers):
        if self.fail:
            raise RuntimeError("OpenD down")
        return {t: self.rows[t] for t in tickers if t in self.rows}


def make(ai=True, ai_ready=True, clock_at=None):
    store, tg, q = Store(":memory:"), FakeTG(), FakeQuotes()
    calls = []

    def call_ai(t, name, info, heads):
        calls.append(t)
        return ("UP", f"{t} goes up, humans") if ai else None

    clock = {"now": clock_at or ny(DAY, 8, 45)}
    game = g.Game(Settings(stocks_per_day=3), store, tg, quotes=q,
                  news=lambda t, n: [{"title": f"{t} news", "published": dt.datetime(2026, 11, 23, 12, tzinfo=dt.timezone.utc)}],
                  call_ai=call_ai, ai_ready=lambda: ai_ready, log=lambda *_: None, clock=lambda: clock["now"])
    return game, store, tg, q, calls, clock


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
    game.tick(ny(DAY, 8, 44))
    assert store.round(DAY.isoformat()) is None
    game.tick(ny(DAY, 8, 45))
    ts = tickers(store)
    for t in ts:
        q.set(t)
    return ts


def test_whole_day():
    game, store, tg, q, calls, clock = make()
    ts = full_day(game, store, tg, q, clock)
    text, buttons = tg.sent[0]
    assert "Beat the Bot" in text and "not investment advice" in text
    assert "goes up" not in text                     # the Bot's calls stay hidden until the lock
    assert all(len(cb.encode()) <= 64 for row in buttons for _, cb in row)
    assert calls == ts
    up, down = buttons[0][0][1], buttons[0][1][1]
    assert "saved" in game.on_vote(up, 7, "Ann", ny(DAY, 9, 0))
    assert "saved" in game.on_vote(down, 7, "Ann", ny(DAY, 9, 10))   # changed mind: last one counts
    for row in buttons[1:]:
        game.on_vote(row[0][1], 7, "Ann", ny(DAY, 9, 0))
    game.on_vote(buttons[0][0][1], 8, "Ben", ny(DAY, 9, 29, 59))
    game.tick(ny(DAY, 9, 30))
    assert store.round(DAY.isoformat())["status"] == "locked"
    assert "goes up, humans" in tg.sent[-1][0]
    assert "Too late" in game.on_vote(buttons[1][0][1], 8, "Ben", ny(DAY, 9, 30, 1))
    # opens arrive with the session
    for t in ts:
        q.set(t, open_=100.0, last=100.5, when=ny(DAY, 9, 36))
    game.tick(ny(DAY, 9, 36))
    assert all(s["open"] == 100.0 for s in store.stocks(DAY.isoformat()))
    # closes: first stock down, the others up
    q.set(ts[0], open_=100.0, last=98.0, when=ny(DAY, 16))
    for t in ts[1:]:
        q.set(t, open_=100.0, last=103.0, when=ny(DAY, 16))
    game.tick(ny(DAY, 16, 5))
    assert store.round(DAY.isoformat())["status"] == "locked"   # not before close + 10 min
    game.tick(ny(DAY, 16, 10))
    assert store.round(DAY.isoformat())["status"] == "settled"
    res = tg.sent[-1][0]
    assert "Results" in res and "Ann" in res and "Beat the Bot today: Ann" in res
    scores = {r["name"]: (r["points"], r["played"]) for r in store.scores()}
    assert scores["Ann"] == (3, 3)            # DOWN on #1, UP on the rest
    assert scores["Ben"] == (0, 1)
    assert scores[BOT_NAME] == (2, 3)
    game.tick(ny(DAY, 16, 30))                # settled rounds are left alone
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
    assert "sat this one out" in tg.sent[-1][0]
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
    assert "not scored" in tg.sent[-1][0]


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
    assert "No round yet" in game.on_command("/today", 1, "Ann", ny(DAY, 8))[0]
    full_day(game, store, tg, q, clock)
    text, buttons = game.on_command("/today@BeatTheBotBot", 1, "Ann", ny(DAY, 9))
    assert "Beat the Bot" in text and buttons
    assert "This week" in game.on_command("/board@BeatTheBotBot extra", 1, "Ann", ny(DAY, 9))[0]
    assert "no settled picks" in game.on_command("/me", 1, "Ann", ny(DAY, 9))[0]
    assert game.on_command("/unknown", 1, "Ann", ny(DAY, 9)) is None
    # updates from other chats are ignored; votes answer with a toast
    cb = buttons[0][0][1]
    handle(game, tg, "-100", {"callback_query": {"id": "x", "data": cb, "from": {"id": 5, "first_name": "Eve"},
                                                 "message": {"chat": {"id": -999}}}}, ny(DAY, 9))
    assert tg.answers[-1].startswith("This game") and not store.votes(DAY.isoformat())
    handle(game, tg, "-100", {"callback_query": {"id": "x", "data": cb, "from": {"id": 5, "first_name": "Eve"},
                                                 "message": {"chat": {"id": -100}}}}, ny(DAY, 9))
    assert "saved" in tg.answers[-1] and store.votes(DAY.isoformat())[0]["name"] == "Eve"
    assert "Unknown" in game.on_vote("garbage", 5, "Eve", ny(DAY, 9))
    assert "no longer" in game.on_vote("v|20261123|ZZZZ|U", 5, "Eve", ny(DAY, 9))


def test_news_relevance():
    assert news.about("Meta Platforms buys more chips", "META", "Meta Platforms")
    assert news.about("Alphabet (GOOGL) rebounds", "GOOGL", "Alphabet-A")
    assert news.about("Eli Lilly drug trial", "LLY", "Eli Lilly and Co")
    assert not news.about("How Taylor Sheridan makes a profit", "META", "Meta Platforms")
    assert not news.about("JPMorgan price prediction", "LLY", "Eli Lilly and Co")
    assert not news.about("Lovely day", "V", "Visa")


def test_parse_call():
    assert glm.parse_call('```json\n{"call":"down","reason":"meh"}\n```') == ("DOWN", "meh")
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
    with pytest.raises(RuntimeError):
        game.tick(ny(DAY, 8, 45))
    assert store.round(DAY.isoformat())["message_id"] is None
    tg.send = real
    game.tick(ny(DAY, 8, 46))
    assert store.round(DAY.isoformat())["message_id"] == 1 and "Beat the Bot" in tg.sent[0][0]
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
