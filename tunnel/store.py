"""事件存储：每个施工方案一条仅追加 JSONL 日志。

重启后通过重放事件重建全部状态；写入采用临时文件 + os.replace 原子提交。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, Any, List, Optional


@dataclass
class Event:
    uid: str
    session_id: str
    scenario_id: str
    seq: int                    # 方案内全局递增
    ring_no: int
    event_time_ms: int
    type: str
    payload: Dict[str, Any]
    source_uid: Optional[str] = None   # 关联的传感读数 uid（重放去重）

    def to_line(self) -> str:
        return json.dumps({
            "uid": self.uid,
            "session_id": self.session_id,
            "scenario_id": self.scenario_id,
            "seq": self.seq,
            "ring_no": self.ring_no,
            "event_time_ms": self.event_time_ms,
            "type": self.type,
            "payload": self.payload,
            "source_uid": self.source_uid,
        }, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> "Event":
        d = json.loads(line)
        return cls(
            uid=d["uid"], scenario_id=d["scenario_id"], session_id=d["session_id"],
            seq=d["seq"], ring_no=d["ring_no"], event_time_ms=d["event_time_ms"],
            type=d["type"], payload=d["payload"], source_uid=d.get("source_uid"),
        )


class EventStore:
    def __init__(self, root_dir: str, session_id: str):
        self.root_dir = root_dir
        self.session_id = session_id
        self.events_dir = os.path.join(root_dir, "events", session_id)
        os.makedirs(self.events_dir, exist_ok=True)
        os.makedirs(os.path.join(root_dir, "strata"), exist_ok=True)

    # ------------------------------------------------------------- 追加
    def append(self, scenario_id: str, ring_no: int, event_time_ms: int,
               type_: str, payload: Dict[str, Any],
               source_uid: Optional[str] = None) -> Event:
        path = self._path(scenario_id)
        next_seq = 1
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                last = ""
                for line in fh:
                    if line.strip():
                        last = line
                if last:
                    next_seq = json.loads(last)["seq"] + 1
        uid = f"e-{scenario_id}-{next_seq}"
        event = Event(uid, self.session_id, scenario_id, next_seq,
                      ring_no, event_time_ms, type_, payload, source_uid)
        self._atomic_append(path, event.to_line())
        return event

    @staticmethod
    def _atomic_append(path: str, line: str) -> None:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".evt-", suffix=".tmp")
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    with open(path, "r", encoding="utf-8") as src:
                        for chunk in iter(lambda: src.read(1 << 16), ""):
                            fh.write(chunk)
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------- 读取
    def _path(self, scenario_id: str) -> str:
        return os.path.join(self.events_dir, f"{scenario_id}.jsonl")

    def load(self, scenario_id: str) -> List[Event]:
        path = self._path(scenario_id)
        if not os.path.exists(path):
            return []
        events: List[Event] = []
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(Event.from_line(line))
        return events

    def list_scenarios(self) -> List[str]:
        if not os.path.isdir(self.events_dir):
            return []
        return sorted(
            f[:-6] for f in os.listdir(self.events_dir) if f.endswith(".jsonl")
        )

    # ------------------------------------------------------------- 地层
    def strata_path(self, line_id: str) -> str:
        return os.path.join(self.root_dir, "strata", f"{line_id}.json")

    def save_strata(self, line_id: str, columns: List[Dict[str, Any]]) -> None:
        path = self.strata_path(line_id)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".st-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(columns, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load_strata(self, line_id: str) -> List[Dict[str, Any]]:
        path = self.strata_path(line_id)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
