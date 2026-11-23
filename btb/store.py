"""SQLite state. Every stage of a round is stored, so a restart resumes where it left off."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

BOT_ID = 0          # the AI's player id
BOT_NAME = "🤖 Ah Huat"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (day TEXT PRIMARY KEY, status TEXT NOT NULL, message_id INTEGER,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stocks (day TEXT NOT NULL, pos INTEGER NOT NULL, ticker TEXT NOT NULL,
    name TEXT NOT NULL, prev_close REAL, pre_price REAL, headlines TEXT NOT NULL DEFAULT '[]',
    ai_call TEXT, ai_reason TEXT NOT NULL DEFAULT '', ai_conf REAL, ai_double INTEGER NOT NULL DEFAULT 0,
    open REAL, close REAL, result TEXT,
    PRIMARY KEY (day, ticker));
CREATE TABLE IF NOT EXISTS votes (day TEXT NOT NULL, ticker TEXT NOT NULL, user_id INTEGER NOT NULL,
    call TEXT NOT NULL, at TEXT NOT NULL, double INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, ticker, user_id));
CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""
# Columns added after the first release, for databases created before them.
MIGRATIONS = [("stocks", "ai_conf", "REAL"), ("stocks", "ai_double", "INTEGER NOT NULL DEFAULT 0"),
              ("votes", "double", "INTEGER NOT NULL DEFAULT 0")]


class Store:
    def __init__(self, path: Path | str, bot_name: str = BOT_NAME):
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
        self.db.commit()

    def get(self, k: str, default: str = "") -> str:
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def set(self, k: str, v: str) -> None:
        self.db.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()

    def round(self, day: str):
        return self.db.execute("SELECT * FROM rounds WHERE day=?", (day,)).fetchone()

    def stocks(self, day: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM stocks WHERE day=? ORDER BY pos", (day,)).fetchall()
        return [dict(r, headlines=json.loads(r["headlines"])) for r in rows]

    def add_round(self, day: str, stocks: list[dict], created_at: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO rounds (day, status, created_at) VALUES (?, 'open', ?)", (day, created_at))
            for i, s in enumerate(stocks):
                self.db.execute(
                    "INSERT INTO stocks (day, pos, ticker, name, prev_close, pre_price, headlines, ai_call, ai_reason,"
                    " ai_conf) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (day, i, s["ticker"], s["name"], s.get("prev_close"), s.get("pre_price"),
                     json.dumps(s.get("headlines", [])), s.get("ai_call"), s.get("ai_reason", ""), s.get("ai_conf")))

    def set_status(self, day: str, status: str) -> None:
        self.db.execute("UPDATE rounds SET status=? WHERE day=?", (status, day))
        self.db.commit()

    def set_message(self, day: str, message_id: int) -> None:
        self.db.execute("UPDATE rounds SET message_id=? WHERE day=?", (message_id, day))
        self.db.commit()

    def update_stock(self, day: str, ticker: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE stocks SET {cols} WHERE day=? AND ticker=?", (*fields.values(), day, ticker))
        self.db.commit()

    def player(self, user_id: int, name: str) -> None:
        self.db.execute("INSERT INTO players VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                        (user_id, name[:40]))
        self.db.commit()

    def vote(self, day: str, ticker: str, user_id: int, call: str, at: str) -> None:
        self.db.execute("INSERT INTO votes (day, ticker, user_id, call, at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(day, ticker, user_id) DO UPDATE SET call=excluded.call, at=excluded.at",
                        (day, ticker, user_id, call, at))
        self.db.commit()

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

    def scores(self, since: str = "", until: str = "9999") -> list[dict]:
        """Per player over settled stocks with day in [since, until]: points (doubles +2/-1), correct, played."""
        rows = self.db.execute(
            "SELECT p.user_id, p.name, SUM(CASE WHEN v.call = s.result THEN 1 + v.double ELSE -v.double END) AS points, "
            "SUM(v.call = s.result) AS correct, COUNT(*) AS played "
            "FROM votes v JOIN stocks s ON s.day=v.day AND s.ticker=v.ticker JOIN players p USING (user_id) "
            "WHERE s.result IN ('UP','DOWN') AND v.day BETWEEN ? AND ? "
            "GROUP BY p.user_id ORDER BY points DESC, correct DESC, played ASC, p.name", (since, until)).fetchall()
        return [dict(r) for r in rows]

    def streaks(self) -> dict[int, int]:
        """Current run of correct calls per player, over settled stocks in round order."""
        out: dict[int, int] = {}
        for r in self.db.execute(
                "SELECT v.user_id, v.call = s.result AS ok FROM votes v JOIN stocks s ON s.day=v.day AND s.ticker=v.ticker "
                "WHERE s.result IN ('UP','DOWN') ORDER BY v.day, s.pos"):
            out[r["user_id"]] = out.get(r["user_id"], 0) + 1 if r["ok"] else 0
        return out
