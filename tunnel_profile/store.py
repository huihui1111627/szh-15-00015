"""仅追加的事件日志（JSONL），服务重启后通过重放完整续算。"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, List, Optional


class EventStore:
    """内存 + JSONL 双写；任何状态都可由事件日志重放得到。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._seq = 0
        self.events: List[dict] = []
        if path and os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.events.append(json.loads(line))
        if self.events:
            self._seq = max(e["seq"] for e in self.events)

    def append(self, scenario: str, ts: int, etype: str, payload: dict,
               source: str = "system", ref: Optional[str] = None) -> dict:
        self._seq += 1
        event = {
            "id": f"e{self._seq}",
            "seq": self._seq,
            "scenario": scenario,
            "ts": ts,
            "type": etype,
            "payload": payload,
            "source": source,
        }
        if ref:
            event["ref"] = ref
        self.events.append(event)
        if self.path:
            self._persist(event)
        return event

    def append_raw(self, event: dict) -> None:
        """克隆场景时复制历史事件。"""
        self._seq = max(self._seq, event["seq"])
        self.events.append(event)
        if self.path:
            self._persist(event)

    def _persist(self, event: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def by_scenario(self, name: str) -> List[dict]:
        return [e for e in self.events if e["scenario"] == name]

    def scenarios(self) -> List[str]:
        seen: List[str] = []
        for event in self.events:
            if event["scenario"] not in seen:
                seen.append(event["scenario"])
        return seen

    def rewrite_for_clone(self, cloned: List[dict]) -> None:
        """批量导入克隆事件并落盘（一次性原子写）。"""
        self.events.extend(cloned)
        if self.events:
            self._seq = max(e["seq"] for e in self.events)
        if self.path and cloned:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for event in self.events:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
