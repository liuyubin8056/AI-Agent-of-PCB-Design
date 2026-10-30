"""运行轨迹：每一步的输入、输出、耗时都落盘成 JSONL。

报告里的「实验数据」和「失败案例」全靠它：跑完一个用例，trace.jsonl 就是证据链，
不用事后回忆当时模型到底输出了什么。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Tracer:
    out_dir: Path
    events: list[dict[str, Any]] = field(default_factory=list)
    _t0: float = field(default_factory=time.perf_counter)

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.out_dir / "trace.jsonl"

    def event(self, kind: str, **payload: Any) -> dict[str, Any]:
        record = {
            "t": round(time.perf_counter() - self._t0, 4),
            "kind": kind,
            **payload,
        }
        self.events.append(record)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def node(self, name: str, round_no: int = 0, **payload: Any) -> None:
        self.event("node", node=name, round=round_no, **payload)

    def flush_summary(self, summary: dict[str, Any]) -> Path:
        path = self.out_dir / "summary.json"
        path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def text_log(self) -> str:
        lines = []
        for rec in self.events:
            kind = rec.get("kind", "")
            if kind == "node":
                lines.append(f"[{rec['t']:>6.2f}s] {rec.get('node')} 轮次{rec.get('round', 0)}")
            elif kind == "issues":
                lines.append(f"[{rec['t']:>6.2f}s] 校验：{rec.get('errors')} 错 / {rec.get('warnings')} 警")
            elif kind == "artifact":
                lines.append(f"[{rec['t']:>6.2f}s] 产物：{rec.get('name')}")
            else:
                lines.append(f"[{rec['t']:>6.2f}s] {kind}")
        return "\n".join(lines)
