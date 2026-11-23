"""SQLite state. Every stage of a round is stored, so a restart resumes where it left off.

One round per trading day is shared by every group (same stocks, same AI calls); each group has its own post of
it. A player's picks are global for the day, and they count on the board of every group they play in."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BOT_ID = 0          # the AI's player id
BOT_NAME = "🤖 Ah Huat"
VOID = "VOID"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (day TEXT PRIMARY KEY, status TEXT NOT NULL, message_id INTEGER,
    created_at TEXT NOT NULL, mover TEXT, ai_mover TEXT, ai_mover_reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS stocks (day TEXT NOT NULL, pos INTEGER NOT NULL, ticker TEXT NOT NULL,
    name TEXT NOT NULL, prev_close REAL, pre_price REAL, headlines TEXT NOT NULL DEFAULT '[]',
    ai_call TEXT, ai_reason TEXT NOT NULL DEFAULT '', ai_conf REAL, ai_double INTEGER NOT NULL DEFAULT 0,
    open REAL, close REAL, result TEXT,
    PRIMARY KEY (day, ticker));
CREATE TABLE IF NOT EXISTS votes (day TEXT NOT NULL, ticker TEXT NOT NULL, user_id INTEGER NOT NULL,
    call TEXT NOT NULL, at TEXT NOT NULL, double INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, ticker, user_id));
CREATE TABLE IF NOT EXISTS bonus (day TEXT NOT NULL, user_id INTEGER NOT NULL, pick TEXT NOT NULL,
    at TEXT NOT NULL, PRIMARY KEY (day, user_id));
CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1, league INTEGER NOT NULL DEFAULT 0, joined_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS members (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id));
CREATE TABLE IF NOT EXISTS posts (day TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY (day, chat_id));
CREATE TABLE IF NOT EXISTS reasons (day TEXT NOT NULL, user_id INTEGER NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY (day, user_id));
CREATE TABLE IF NOT EXISTS quiz (poll_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, week TEXT NOT NULL,
    idx INTEGER NOT NULL, correct INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quiz_answers (poll_id TEXT NOT NULL, user_id INTEGER NOT NULL, choice INTEGER NOT NULL,
    PRIMARY KEY (poll_id, user_id));
CREATE TABLE IF NOT EXISTS own_picks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, ticker TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', call TEXT NOT NULL,
    conf TEXT NOT NULL, made_at TEXT NOT NULL, made_day TEXT NOT NULL, day TEXT NOT NULL, start_price REAL,
    start_kind TEXT NOT NULL, close REAL, result TEXT, posted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""
# Columns added after the first release, for databases created before them.
MIGRATIONS = [("stocks", "ai_conf", "REAL"), ("stocks", "ai_double", "INTEGER NOT NULL DEFAULT 0"),
              ("votes", "double", "INTEGER NOT NULL DEFAULT 0"), ("rounds", "mover", "TEXT"),
              ("rounds", "ai_mover", "TEXT"), ("rounds", "ai_mover_reason", "TEXT NOT NULL DEFAULT ''"),
              ("votes", "conf", "TEXT"), ("stocks", "why", "TEXT"), ("rounds", "lesson", "TEXT")]

# Confidence picks: (points if right, points if wrong). The costs make "sure" pay only when you are right more
# than 11/16 = 69% of the time, and "lean" more than 4/7 = 57%: overconfidence loses, an honest skip scores 0.
CONF = {"lean": (3, -4), "sure": (5, -11)}

# Points per pick, shared by every score query: confidence picks per CONF (older rounds: 1 per right call, doubles
# +2/-1), a right biggest-mover bonus +2.
POINTS = """
WITH pts AS (
  SELECT v.day, v.user_id,
         CASE WHEN v.conf = 'sure' THEN (CASE WHEN v.call = s.result THEN 5 ELSE -11 END)
              WHEN v.conf = 'lean' THEN (CASE WHEN v.call = s.result THEN 3 ELSE -4 END)
              WHEN v.call = s.result THEN 1 + v.double ELSE -v.double END AS p,
         (v.call = s.result) AS c, 1 AS n
  FROM votes v JOIN stocks s ON s.day = v.day AND s.ticker = v.ticker WHERE s.result IN ('UP', 'DOWN')
  UNION ALL
  SELECT b.day, b.user_id, CASE WHEN b.pick = r.mover THEN 2 ELSE 0 END, 0, 0
  FROM bonus b JOIN rounds r ON r.day = b.day WHERE r.mover IS NOT NULL AND r.mover != 'VOID')
"""


class Store:
    def __init__(self, path: Path | str, bot_name: str = BOT_NAME, home_chat: str | int | None = None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            if col not in {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.bot_name = bot_name or BOT_NAME
        self.db.execute("INSERT INTO players VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                        (BOT_ID, self.bot_name))
        if home_chat:
            home = int(home_chat)
            self.db.execute("INSERT OR IGNORE INTO chats (chat_id, title) VALUES (?, 'home')", (home,))
            # Single-group databases kept the post id on the round and had no member list.
            self.db.execute("INSERT OR IGNORE INTO posts SELECT day, ?, message_id FROM rounds "
                            "WHERE message_id IS NOT NULL", (home,))
            self.db.execute("INSERT OR IGNORE INTO members SELECT DISTINCT ?, user_id FROM votes WHERE user_id != ?",
                            (home, BOT_ID))
        self.db.commit()

    # ---------------------------------------------------------------- kv
    def get(self, k: str, default: str = "") -> str:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()

    # ---------------------------------------------------------------- groups
    def chats(self, active: bool = True) -> list[dict]:
        q = "SELECT * FROM chats" + (" WHERE active=1" if active else "") + " ORDER BY joined_at, chat_id"
        return [dict(r) for r in self.db.execute(q)]

    def chat(self, chat_id: int):
        return self.db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

    def add_chat(self, chat_id: int, title: str, at: str) -> None:
        self.db.execute("INSERT INTO chats (chat_id, title, active, joined_at) VALUES (?, ?, 1, ?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=1", (chat_id, title[:60], at))
        self.db.commit()

    def deactivate(self, chat_id: int) -> None:
        self.db.execute("UPDATE chats SET active=0 WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def set_league(self, chat_id: int, on: bool) -> None:
        self.db.execute("UPDATE chats SET league=? WHERE chat_id=?", (int(on), chat_id))
        self.db.commit()

    def migrate_chat(self, old: int, new: int) -> None:
        """A group upgraded to a supergroup gets a new id."""
        with self.db:
            for table in ("chats", "members", "posts"):
                self.db.execute(f"UPDATE OR IGNORE {table} SET chat_id=? WHERE chat_id=?", (new, old))

    def member(self, chat_id: int, user_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO members VALUES (?, ?)", (chat_id, user_id))
        self.db.commit()

    def members(self, chat_id: int) -> set[int]:
        return {r["user_id"] for r in self.db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,))}

    def post_id(self, day: str, chat_id: int) -> int | None:
        r = self.db.execute("SELECT message_id FROM posts WHERE day=? AND chat_id=?", (day, chat_id)).fetchone()
        return r["message_id"] if r else None

    def set_post(self, day: str, chat_id: int, message_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO posts VALUES (?, ?, ?)", (day, chat_id, message_id))
        self.db.commit()

    # ---------------------------------------------------------------- rounds
    def round(self, day: str):
        return self.db.execute("SELECT * FROM rounds WHERE day=?", (day,)).fetchone()

    def stocks(self, day: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM stocks WHERE day=? ORDER BY pos", (day,)).fetchall()
        return [dict(r, headlines=json.loads(r["headlines"])) for r in rows]

    def add_round(self, day: str, stocks: list[dict], created_at: str, ai_mover: tuple | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO rounds (day, status, created_at, ai_mover, ai_mover_reason) "
                            "VALUES (?, 'open', ?, ?, ?)",
                            (day, created_at, ai_mover[0] if ai_mover else None, ai_mover[1] if ai_mover else ""))
            for i, s in enumerate(stocks):
                self.db.execute(
                    "INSERT INTO stocks (day, pos, ticker, name, prev_close, pre_price, headlines, ai_call, ai_reason,"
                    " ai_conf) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (day, i, s["ticker"], s["name"], s.get("prev_close"), s.get("pre_price"),
                     json.dumps(s.get("headlines", [])), s.get("ai_call"), s.get("ai_reason", ""), s.get("ai_conf")))

    def set_status(self, day: str, status: str) -> None:
        self.db.execute("UPDATE rounds SET status=? WHERE day=?", (status, day))
        self.db.commit()

    def set_round(self, day: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE rounds SET {cols} WHERE day=?", (*fields.values(), day))
        self.db.commit()

    def set_mover(self, day: str, mover: str) -> None:
        self.db.execute("UPDATE rounds SET mover=? WHERE day=?", (mover, day))
        self.db.commit()

    def update_stock(self, day: str, ticker: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE stocks SET {cols} WHERE day=? AND ticker=?", (*fields.values(), day, ticker))
        self.db.commit()

    # ---------------------------------------------------------------- picks
    def player(self, user_id: int, name: str) -> None:
        self.db.execute("INSERT INTO players VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                        (user_id, name[:40]))
        self.db.commit()

    def vote(self, day: str, ticker: str, user_id: int, call: str, at: str, conf: str | None = None) -> None:
        self.db.execute("INSERT INTO votes (day, ticker, user_id, call, at, conf) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(day, ticker, user_id) DO UPDATE SET call=excluded.call, at=excluded.at, "
                        "conf=excluded.conf", (day, ticker, user_id, call, at, conf))
        self.db.commit()

    def unvote(self, day: str, ticker: str, user_id: int) -> None:
        self.db.execute("DELETE FROM votes WHERE day=? AND ticker=? AND user_id=?", (day, ticker, user_id))
        self.db.commit()

    def set_reason(self, day: str, user_id: int, reason: str) -> None:
        self.db.execute("INSERT INTO reasons VALUES (?,?,?) ON CONFLICT(day, user_id) DO UPDATE SET reason=excluded.reason",
                        (day, user_id, reason))
        self.db.commit()

    def reason(self, day: str, user_id: int) -> str | None:
        r = self.db.execute("SELECT reason FROM reasons WHERE day=? AND user_id=?", (day, user_id)).fetchone()
        return r["reason"] if r else None

    def decisions(self, user_id: int, since: str = "", until: str = "9999") -> list[dict]:
        """Settled confidence picks of a player with the day's reason tag."""
        return [dict(r) for r in self.db.execute(
            "SELECT v.day, v.ticker, v.call, v.conf, s.result, r.reason FROM votes v "
            "JOIN stocks s ON s.day=v.day AND s.ticker=v.ticker "
            "LEFT JOIN reasons r ON r.day=v.day AND r.user_id=v.user_id "
            "WHERE v.user_id=? AND v.conf IS NOT NULL AND s.result IN ('UP','DOWN') AND v.day BETWEEN ? AND ? "
            "ORDER BY v.day", (user_id, since, until))]

    def skipped(self, user_id: int, since: str = "", until: str = "9999") -> list[dict]:
        """Settled stocks of days the player played (any pick) but left this stock unpicked."""
        return [dict(r) for r in self.db.execute(
            "SELECT s.day, s.ticker, s.result FROM stocks s WHERE s.result IN ('UP','DOWN') AND s.day BETWEEN ? AND ? "
            "AND EXISTS (SELECT 1 FROM votes v WHERE v.day=s.day AND v.user_id=? AND v.conf IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM votes v WHERE v.day=s.day AND v.ticker=s.ticker AND v.user_id=?)",
            (since, until, user_id, user_id))]

    def add_quiz(self, poll_id: str, chat_id: int, week: str, idx: int, correct: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO quiz VALUES (?,?,?,?,?)", (poll_id, chat_id, week, idx, correct))
        self.db.commit()

    def quiz_answer(self, poll_id: str, user_id: int, choice: int) -> bool:
        """First answer counts (quiz polls can't be changed anyway). False for unknown polls."""
        if not self.db.execute("SELECT 1 FROM quiz WHERE poll_id=?", (poll_id,)).fetchone():
            return False
        self.db.execute("INSERT OR IGNORE INTO quiz_answers VALUES (?,?,?)", (poll_id, user_id, choice))
        self.db.commit()
        return True

    def quiz_scores(self, chat_id: int, week: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT a.user_id, p.name, SUM(a.choice = q.correct) AS ok, COUNT(*) AS answered FROM quiz q "
            "JOIN quiz_answers a USING (poll_id) JOIN players p ON p.user_id=a.user_id "
            "WHERE q.chat_id=? AND q.week=? GROUP BY a.user_id ORDER BY ok DESC, answered ASC, p.name",
            (chat_id, week))]

    def set_double(self, day: str, user_id: int, ticker: str | None) -> None:
        """At most one doubled pick per player per day; None clears it."""
        with self.db:
            self.db.execute("UPDATE votes SET double=0 WHERE day=? AND user_id=?", (day, user_id))
            if ticker:
                self.db.execute("UPDATE votes SET double=1 WHERE day=? AND user_id=? AND ticker=?",
                                (day, user_id, ticker))

    def votes(self, day: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT v.*, p.name FROM votes v JOIN players p USING (user_id) WHERE v.day=?", (day,))]

    def set_bonus(self, day: str, user_id: int, pick: str, at: str) -> None:
        self.db.execute("INSERT INTO bonus VALUES (?,?,?,?) ON CONFLICT(day, user_id) "
                        "DO UPDATE SET pick=excluded.pick, at=excluded.at", (day, user_id, pick, at))
        self.db.commit()

    def bonus(self, day: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT b.*, p.name FROM bonus b JOIN players p USING (user_id) WHERE b.day=?", (day,))]

    # ---------------------------------------------------------------- scores
    def scores(self, since: str = "", until: str = "9999", chat_id: int | None = None) -> list[dict]:
        """Per player over settled picks with day in [since, until]: points, correct calls, calls played.
        With chat_id: that group's players plus the AI."""
        where, args = "pts.day BETWEEN ? AND ?", [since, until]
        if chat_id is not None:
            where += " AND (pts.user_id = ? OR pts.user_id IN (SELECT user_id FROM members WHERE chat_id = ?))"
            args += [BOT_ID, chat_id]
        rows = self.db.execute(
            POINTS + "SELECT p.user_id, p.name, SUM(pts.p) AS points, SUM(pts.c) AS correct, SUM(pts.n) AS played "
            f"FROM pts JOIN players p USING (user_id) WHERE {where} "
            "GROUP BY p.user_id ORDER BY points DESC, correct DESC, played ASC, p.name", args).fetchall()
        return [dict(r) for r in rows]

    def streaks(self) -> dict[int, int]:
        """Current run of correct calls per player, over settled stocks in round order."""
        out: dict[int, int] = {}
        for r in self.db.execute(
                "SELECT v.user_id, v.call = s.result AS ok FROM votes v JOIN stocks s ON s.day=v.day AND s.ticker=v.ticker "
                "WHERE s.result IN ('UP','DOWN') ORDER BY v.day, s.pos"):
            out[r["user_id"]] = out.get(r["user_id"], 0) + 1 if r["ok"] else 0
        return out

    def calls(self, user_id: int) -> list[dict]:
        """Settled calls of one player, newest first, with the stock's move."""
        return [dict(r) for r in self.db.execute(
            "SELECT v.day, v.ticker, v.call, v.double, v.conf, s.result, s.open, s.close, s.ai_reason "
            "FROM votes v JOIN stocks s ON s.day=v.day AND s.ticker=v.ticker "
            "WHERE v.user_id=? AND s.result IN ('UP','DOWN') ORDER BY v.day DESC, s.pos", (user_id,))]

    def mover_record(self, user_id: int) -> tuple[int, int]:
        r = self.db.execute("SELECT SUM(b.pick = r.mover) AS won, COUNT(*) AS played FROM bonus b "
                            "JOIN rounds r ON r.day=b.day WHERE b.user_id=? AND r.mover IS NOT NULL AND r.mover != 'VOID'",
                            (user_id,)).fetchone()
        return (r["won"] or 0, r["played"] or 0)

    # ---------------------------------------------------------------- own picks
    def add_own(self, **f) -> int:
        cols = ", ".join(f)
        cur = self.db.execute(f"INSERT INTO own_picks ({cols}) VALUES ({', '.join('?' * len(f))})", tuple(f.values()))
        self.db.commit()
        return cur.lastrowid

    def own(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            f"SELECT o.*, p.name AS player FROM own_picks o JOIN players p USING (user_id) WHERE {where} ORDER BY o.id",
            args)]

    def update_own(self, pick_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE own_picks SET {cols} WHERE id=?", (*fields.values(), pick_id))
        self.db.commit()

    def delete_own(self, pick_id: int) -> None:
        self.db.execute("DELETE FROM own_picks WHERE id=?", (pick_id,))
        self.db.commit()

    def own_scores(self, since: str = "", until: str = "9999", chat_id: int | None = None) -> list[dict]:
        where, args = "o.result IN ('UP','DOWN') AND o.day BETWEEN ? AND ?", [since, until]
        if chat_id is not None:
            where += " AND o.chat_id = ?"
            args.append(chat_id)
        return [dict(r) for r in self.db.execute(
            "SELECT o.user_id, p.name, SUM(CASE WHEN o.conf='sure' THEN (CASE WHEN o.call=o.result THEN 5 ELSE -11 END) "
            "ELSE (CASE WHEN o.call=o.result THEN 3 ELSE -4 END) END) AS points, SUM(o.call=o.result) AS correct, "
            f"COUNT(*) AS played FROM own_picks o JOIN players p USING (user_id) WHERE {where} "
            "GROUP BY o.user_id ORDER BY points DESC, correct DESC, p.name", args)]

    def league(self, since: str, until: str, min_players: int = 2) -> list[dict]:
        """Opted-in groups ranked by average points per active player over [since, until]."""
        out = []
        for c in self.db.execute("SELECT chat_id, title FROM chats WHERE active=1 AND league=1"):
            humans = [r for r in self.scores(since, until, c["chat_id"]) if r["user_id"] != BOT_ID]
            if len(humans) >= min_players:
                out.append({"chat_id": c["chat_id"], "title": c["title"], "players": len(humans),
                            "avg": sum(r["points"] for r in humans) / len(humans)})
        return sorted(out, key=lambda r: -r["avg"])
