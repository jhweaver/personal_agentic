"""SQLite persistence. Everything the agent does lands here.

One connection, WAL mode, called from the event loop thread. Every query is small, so
running them inline is simpler than a thread pool and fast enough for one person.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_session ON events(session, id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- main | subagent
    title TEXT,
    parent TEXT,
    routine TEXT,
    status TEXT NOT NULL,          -- running | done | error | cancelled | idle
    result TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    name TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session, archived, id);

CREATE TABLE IF NOT EXISTS preambles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,            -- note | archive | routine
    space TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL,
    embedding BLOB,
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session TEXT NOT NULL,
    kind TEXT NOT NULL,            -- approval | review | question
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,          -- open | approved | denied | resolved | expired
    escalation TEXT NOT NULL DEFAULT '[]',
    steps_done INTEGER NOT NULL DEFAULT 0,
    resolved_ts REAL,
    resolution TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kv (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS wc_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    routine TEXT NOT NULL,
    trigger TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,          -- running | done | error | skipped
    session TEXT,
    output TEXT
);
CREATE INDEX IF NOT EXISTS routine_runs_name ON routine_runs(routine, id);
"""


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _exec(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, params)

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            return _row(self.db.execute(sql, params).fetchone())

    # ---- events -------------------------------------------------------------
    def add_event(self, session: str, kind: str, source: str, data: dict[str, Any], ts: float) -> int:
        cur = self._exec(
            "INSERT INTO events (ts, session, kind, source, data) VALUES (?,?,?,?,?)",
            (ts, session, kind, source, json.dumps(data, default=str)),
        )
        return int(cur.lastrowid)

    def events(
        self,
        session: str | None = None,
        after_id: int = 0,
        limit: int = 500,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where, params = ["id > ?"], [after_id]
        if session:
            where.append("session = ?")
            params.append(session)
        if before_id:
            where.append("id < ?")
            params.append(before_id)
        rows = self._all(
            f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", [*params, limit]
        )
        rows.reverse()
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # ---- sessions -----------------------------------------------------------
    def upsert_session(self, sid: str, kind: str, **fields: Any) -> None:
        now = time.time()
        existing = self.get_session(sid)
        if existing is None:
            self._exec(
                "INSERT INTO sessions (id, kind, title, parent, routine, status, result, created, updated, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    kind,
                    fields.get("title"),
                    fields.get("parent"),
                    fields.get("routine"),
                    fields.get("status", "idle"),
                    fields.get("result"),
                    now,
                    now,
                    json.dumps(fields.get("meta", {})),
                ),
            )
            return
        sets, params = ["updated = ?"], [now]
        for key in ("title", "parent", "routine", "status", "result"):
            if key in fields:
                sets.append(f"{key} = ?")
                params.append(fields[key])
        if "meta" in fields:
            sets.append("meta = ?")
            params.append(json.dumps(fields["meta"]))
        self._exec(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", [*params, sid])

    def get_session(self, sid: str) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM sessions WHERE id = ?", (sid,))
        if r:
            r["meta"] = json.loads(r["meta"])
        return r

    def list_sessions(self, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if kind:
            rows = self._all(
                "SELECT * FROM sessions WHERE kind = ? ORDER BY created DESC LIMIT ?", (kind, limit)
            )
        else:
            rows = self._all("SELECT * FROM sessions ORDER BY created DESC LIMIT ?", (limit,))
        for r in rows:
            r["meta"] = json.loads(r["meta"])
        return rows

    # ---- messages (the model's context) --------------------------------------
    def add_message(self, session: str, msg: dict[str, Any]) -> int:
        cur = self._exec(
            "INSERT INTO messages (session, ts, role, content, tool_calls, tool_call_id, name) VALUES (?,?,?,?,?,?,?)",
            (
                session,
                time.time(),
                msg["role"],
                msg.get("content"),
                json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None,
                msg.get("tool_call_id"),
                msg.get("name"),
            ),
        )
        return int(cur.lastrowid)

    def active_messages(self, session: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM messages WHERE session = ? AND archived = 0 ORDER BY id", (session,))
        out = []
        for r in rows:
            m: dict[str, Any] = {"role": r["role"], "content": r["content"], "_id": r["id"]}
            if r["tool_calls"]:
                m["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_call_id"]:
                m["tool_call_id"] = r["tool_call_id"]
            if r["name"]:
                m["name"] = r["name"]
            out.append(m)
        return out

    def archive_messages(self, session: str, upto_id: int) -> None:
        self._exec("UPDATE messages SET archived = 1 WHERE session = ? AND id <= ?", (session, upto_id))

    def set_preamble(self, session: str, text: str) -> None:
        self._exec("INSERT INTO preambles (session, ts, text) VALUES (?,?,?)", (session, time.time(), text))

    def get_preamble(self, session: str) -> str | None:
        r = self._one("SELECT text FROM preambles WHERE session = ? ORDER BY id DESC LIMIT 1", (session,))
        return r["text"] if r else None

    # ---- memories -------------------------------------------------------------
    def add_memory(
        self, kind: str, text: str, embedding: bytes | None, space: str = "text", meta: dict | None = None
    ) -> int:
        cur = self._exec(
            "INSERT INTO memories (ts, kind, space, text, embedding, meta) VALUES (?,?,?,?,?,?)",
            (time.time(), kind, space, text, embedding, json.dumps(meta or {})),
        )
        return int(cur.lastrowid)

    def memories(self, space: str = "text", kinds: list[str] | None = None) -> list[dict[str, Any]]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self._all(
                f"SELECT * FROM memories WHERE space = ? AND kind IN ({marks}) ORDER BY id", [space, *kinds]
            )
        return self._all("SELECT * FROM memories WHERE space = ? ORDER BY id", (space,))

    def search_memories_text(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = [t for t in query.split() if len(t) > 2][:6] or [query]
        where = " OR ".join("text LIKE ?" for _ in terms)
        return self._all(
            f"SELECT * FROM memories WHERE {where} ORDER BY id DESC LIMIT ?",
            [*(f"%{t}%" for t in terms), limit],
        )

    def memories_missing_embeddings(self, limit: int = 64) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM memories WHERE embedding IS NULL ORDER BY id LIMIT ?", (limit,))

    def set_memory_embedding(self, mid: int, embedding: bytes) -> None:
        self._exec("UPDATE memories SET embedding = ? WHERE id = ?", (embedding, mid))

    # ---- attention (approvals, reviews) ---------------------------------------
    def add_attention(
        self, session: str, kind: str, title: str, body: str, escalation: list[dict], data: dict | None = None
    ) -> int:
        cur = self._exec(
            "INSERT INTO attention (ts, session, kind, title, body, status, escalation, data) VALUES (?,?,?,?,?,?,?,?)",
            (time.time(), session, kind, title, body, "open", json.dumps(escalation), json.dumps(data or {})),
        )
        return int(cur.lastrowid)

    def get_attention(self, aid: int) -> dict[str, Any] | None:
        r = self._one("SELECT * FROM attention WHERE id = ?", (aid,))
        if r:
            r["escalation"] = json.loads(r["escalation"])
            r["data"] = json.loads(r["data"])
        return r

    def open_attention(self, session: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        where, params = ["status = 'open'"], []
        if session:
            where.append("session = ?")
            params.append(session)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        rows = self._all(f"SELECT * FROM attention WHERE {' AND '.join(where)} ORDER BY id", params)
        for r in rows:
            r["escalation"] = json.loads(r["escalation"])
            r["data"] = json.loads(r["data"])
        return rows

    def resolve_attention(self, aid: int, status: str, resolution: str | None) -> None:
        self._exec(
            "UPDATE attention SET status = ?, resolution = ?, resolved_ts = ? WHERE id = ? AND status = 'open'",
            (status, resolution, time.time(), aid),
        )

    def set_attention_steps(self, aid: int, steps_done: int) -> None:
        self._exec("UPDATE attention SET steps_done = ? WHERE id = ?", (steps_done, aid))

    # ---- key/value state ------------------------------------------------------
    def kv_get(self, namespace: str, key: str, default: Any = None) -> Any:
        r = self._one("SELECT value FROM kv WHERE namespace = ? AND key = ?", (namespace, key))
        return json.loads(r["value"]) if r else default

    def kv_set(self, namespace: str, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (namespace, key, value, ts) VALUES (?,?,?,?)"
            " ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
            (namespace, key, json.dumps(value, default=str), time.time()),
        )

    def kv_delete(self, namespace: str, key: str) -> None:
        self._exec("DELETE FROM kv WHERE namespace = ? AND key = ?", (namespace, key))

    def kv_all(self, namespace: str) -> dict[str, Any]:
        rows = self._all("SELECT key, value FROM kv WHERE namespace = ?", (namespace,))
        return {r["key"]: json.loads(r["value"]) for r in rows}

    # ---- working context versions -----------------------------------------------
    def add_wc_version(self, author: str, content: str) -> None:
        self._exec(
            "INSERT INTO wc_versions (ts, author, content) VALUES (?,?,?)", (time.time(), author, content)
        )

    def wc_versions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, ts, author, length(content) AS size FROM wc_versions ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def wc_version(self, vid: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM wc_versions WHERE id = ?", (vid,))

    # ---- routine runs -------------------------------------------------------------
    def start_run(self, routine: str, trigger: str, status: str = "running") -> int:
        cur = self._exec(
            "INSERT INTO routine_runs (routine, trigger, started, status) VALUES (?,?,?,?)",
            (routine, trigger, time.time(), status),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, output: str | None, session: str | None = None) -> None:
        self._exec(
            "UPDATE routine_runs SET finished = ?, status = ?, output = ?, session = COALESCE(?, session) WHERE id = ?",
            (time.time(), status, output, session, run_id),
        )

    def recent_runs(self, routine: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if routine:
            return self._all(
                "SELECT * FROM routine_runs WHERE routine = ? ORDER BY id DESC LIMIT ?", (routine, limit)
            )
        return self._all("SELECT * FROM routine_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_run(self, routine: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM routine_runs WHERE routine = ? AND status != 'skipped' ORDER BY id DESC LIMIT 1",
            (routine,),
        )
