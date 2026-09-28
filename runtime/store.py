"""幂等调用记录存储：文件持久化 + 进程内并发去重。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

from runtime.models import CallResult


class InvocationStore:
    """以 JSON 文件持久化调用结果，进程重启后仍可按幂等键取回结果。

    同进程内，正在执行的幂等键对应一个共享事件，并发重复请求在该事件上
    等待同一份结果。
    """

    def __init__(self, path: str = ".runtime/invocations.json") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}

    def load(self, key: str) -> CallResult | None:
        """读取幂等键对应的已落盘结果；不存在返回 None。"""
        with self._lock:
            records = self._read_all()
            data = records.get(key)
            return CallResult.from_dict(data) if data is not None else None

    def save(self, result: CallResult) -> None:
        """把终态结果原子落盘并唤醒所有等待者。"""
        with self._lock:
            records = self._read_all()
            records[result.idempotency_key] = result.to_dict()
            self._write_all(records)
            event = self._events.pop(result.idempotency_key, None)
            if event is not None:
                event.set()

    def begin(
        self, key: str
    ) -> tuple[CallResult | None, threading.Event | None]:
        """发起一次幂等调用。

        返回 (已有终态结果, 等待事件):
        - 已有终态: (result, None)，直接复用；
        - 有正在进行的执行: (None, event)，等待事件后再读；
        - 首次: (None, None)，调用方负责执行并 save。
        """
        with self._lock:
            existing = self.load(key)
            if existing is not None and existing.terminal:
                return existing, None
            event = self._events.get(key)
            if event is not None:
                return None, event
            event = threading.Event()
            self._events[key] = event
            return None, None

    def _read_all(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"corrupt store file: {self.path}")
        return data

    def _write_all(self, records: dict[str, dict[str, Any]]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".invocations-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            os.unlink(tmp_path)
            raise
