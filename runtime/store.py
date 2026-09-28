"""基于 SQLite 的持久化存储。

终态记录（SUCCEEDED / FAILED / TIMED_OUT）持久保留，进程重启后幂等键仍命中
同一份结果；重启时未完成的非终态记录会被清理，允许该幂等键重新执行。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from runtime.models import InvocationRecord, InvocationStatus

_TERMINAL = (
    InvocationStatus.SUCCEEDED.value,
    InvocationStatus.FAILED.value,
    InvocationStatus.TIMED_OUT.value,
)


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(text: str | None) -> Any:
    if text is None:
        return None
    return json.loads(text)


class InvocationStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS invocations (
                    idempotency_key TEXT PRIMARY KEY,
                    function_name   TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    payload         TEXT,
                    result          TEXT,
                    error           TEXT,
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    heartbeat       REAL NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL
                )
                """
            )

    def load(self, idempotency_key: str) -> InvocationRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invocations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return InvocationRecord(
            idempotency_key=row["idempotency_key"],
            function_name=row["function_name"],
            status=InvocationStatus(row["status"]),
            payload=_loads(row["payload"]),
            result=_loads(row["result"]),
            error=_loads(row["error"]),
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def insert(self, record: InvocationRecord) -> bool:
        """插入新记录；幂等键已存在时返回 False。"""
        now = time.time()
        record.created_at = now
        record.updated_at = now
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO invocations
                        (idempotency_key, function_name, status, payload, result,
                         error, attempts, heartbeat, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.idempotency_key,
                        record.function_name,
                        record.status.value,
                        _dumps(record.payload),
                        _dumps(record.result),
                        _dumps(record.error),
                        record.attempts,
                        now,
                        record.created_at,
                        record.updated_at,
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update(
        self,
        record: InvocationRecord,
        *,
        heartbeat: bool = True,
    ) -> None:
        record.updated_at = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE invocations
                   SET status = ?, payload = ?, result = ?, error = ?,
                       attempts = ?, heartbeat = ?, updated_at = ?
                 WHERE idempotency_key = ?
                """,
                (
                    record.status.value,
                    _dumps(record.payload),
                    _dumps(record.result),
                    _dumps(record.error),
                    record.attempts,
                    record.updated_at if heartbeat else record.updated_at,
                    record.updated_at,
                    record.idempotency_key,
                ),
            )

    def reset_stale_running(self) -> int:
        """清理所有非终态记录（用于进程重启后恢复），返回删除条数。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM invocations WHERE status NOT IN (?, ?, ?)",
                _TERMINAL,
            )
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
