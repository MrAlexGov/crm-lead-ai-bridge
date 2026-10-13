"""SQLite: очередь задач, дедупликация событий, связи лидов между CRM, журнал ошибок."""
import json
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,          -- повторный вебхук с тем же лидом не создаст вторую задачу
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | dead
    attempts INTEGER NOT NULL DEFAULT 0,
    next_run_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, next_run_at);
CREATE TABLE IF NOT EXISTS mappings (        -- какой объект мы создали/обработали в какой CRM
    dedup_key TEXT NOT NULL,
    target TEXT NOT NULL,                    -- bitrix | amo | bitrix_task
    entity_id TEXT NOT NULL,
    created_by_us INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (dedup_key, target)
);
CREATE UNIQUE INDEX IF NOT EXISTS mappings_entity ON mappings(target, entity_id);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, at REAL NOT NULL, retryable INTEGER NOT NULL, message TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- очередь ---
    def enqueue(self, dedup_key: str, payload: dict) -> tuple[int, bool]:
        """Возвращает (job_id, created). created=False — такой лид уже в работе/обработан."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO jobs(dedup_key, payload, next_run_at, created_at, updated_at) VALUES (?,?,?,?,?)",
                (dedup_key, json.dumps(payload, ensure_ascii=False), now, now, now),
            )
            if cur.rowcount:
                return cur.lastrowid, True
            row = c.execute("SELECT id FROM jobs WHERE dedup_key=?", (dedup_key,)).fetchone()
            return row["id"], False

    def claim_due(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM jobs WHERE status='pending' AND next_run_at<=? ORDER BY next_run_at LIMIT ?",
                (time.time(), limit),
            ).fetchall()

    def save_progress(self, job_id: int, payload: dict) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET payload=?, updated_at=? WHERE id=?",
                      (json.dumps(payload, ensure_ascii=False), time.time(), job_id))

    def mark_done(self, job_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?", (time.time(), job_id))

    def mark_failed(self, job_id: int, error: str, retryable: bool, delay: float | None) -> None:
        """delay=None — попытки кончились или ошибка постоянная: задача уходит в dead."""
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO errors(job_id, at, retryable, message) VALUES (?,?,?,?)",
                      (job_id, now, int(retryable), error[:2000]))
            if delay is None:
                c.execute("UPDATE jobs SET status='dead', attempts=attempts+1, last_error=?, updated_at=? WHERE id=?",
                          (error[:2000], now, job_id))
            else:
                c.execute("UPDATE jobs SET attempts=attempts+1, next_run_at=?, last_error=?, updated_at=? WHERE id=?",
                          (now + delay, error[:2000], now, job_id))

    def retry_dead(self, job_id: int) -> bool:
        with self._conn() as c:
            cur = c.execute("UPDATE jobs SET status='pending', attempts=0, next_run_at=? WHERE id=? AND status='dead'",
                            (time.time(), job_id))
            return bool(cur.rowcount)

    def stats(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
            errs = c.execute("SELECT job_id, at, retryable, message FROM errors ORDER BY id DESC LIMIT 20").fetchall()
        return {"jobs": {r["status"]: r["n"] for r in rows}, "last_errors": [dict(e) for e in errs]}

    # --- связи между CRM ---
    def get_mapping(self, dedup_key: str, target: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT entity_id FROM mappings WHERE dedup_key=? AND target=?", (dedup_key, target)).fetchone()
            return row["entity_id"] if row else None

    def set_mapping(self, dedup_key: str, target: str, entity_id: str, created_by_us: bool = True) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO mappings VALUES (?,?,?,?)", (dedup_key, target, entity_id, int(created_by_us)))

    def is_created_by_us(self, target: str, entity_id: str) -> bool:
        """Защита от петли: лид, который мы сами создали зеркалом, придёт к нам вебхуком — его не трогаем."""
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM mappings WHERE target=? AND entity_id=? AND created_by_us=1",
                            (target, str(entity_id))).fetchone()
            return row is not None
