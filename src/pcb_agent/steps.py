"""步骤模型：把编排层的事件流，折叠成"每一步 + 输入 + 输出 + 状态"。

为什么要单独一层：终端界面、纯文本日志、事后回放（trace.jsonl）需要的是同一份数据，
差别只在怎么画。把折叠逻辑放在这里，界面层就只负责渲染，可以被单测覆盖。

事件协议（由 orchestrator 发出）::

    {"kind": "step", "phase": "analyze", "status": "begin", "input": {...}}
    {"kind": "step", "phase": "analyze", "status": "end",   "output": {...}, "ok": true}

`status` 取 begin / end / skip。end 默认 ok，`ok=False` 记为失败，
`warn=True` 记为"完成但有警告"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

TITLES = {
    "analyze": "需求分析",
    "design": "电路设计",
    "inject": "故障注入",
    "evaluate": "渲染 + 校验",
    "critic": "评审",
    "patch": "应用补丁",
    "revise": "解析修改",
    "finalize": "落盘",
}

GLYPHS = {
    "pending": "○",
    "running": "▶",
    "ok": "✓",
    "warn": "!",
    "fail": "✗",
    "skip": "–",
}


@dataclass
class Step:
    """一步执行。key 只用于定位，界面显示 title。"""

    key: str
    phase: str
    title: str
    status: str = "running"
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    t_start: float = 0.0
    elapsed: float | None = None

    def glyph(self) -> str:
        return GLYPHS.get(self.status, "?")

    def duration_text(self) -> str:
        if self.elapsed is None:
            return "…"
        if self.elapsed < 0.001:
            return "<1ms"
        if self.elapsed < 1:
            return f"{self.elapsed * 1000:.0f}ms"
        return f"{self.elapsed:.2f}s"

    STATUS_TEXT = {"ok": "完成", "warn": "有警告", "fail": "失败", "running": "执行中", "skip": "跳过"}

    def status_text(self) -> str:
        return self.STATUS_TEXT.get(self.status, self.status)

    def summary(self) -> str:
        """列表里步骤下方那行短摘要，取自这一步真实的输出。"""
        out = self.outputs
        if self.status == "running":
            return "执行中…"
        if self.phase == "evaluate" and "错误" in out:
            return f"{out['错误']} 错 / {out.get('警告', 0)} 警"
        if self.phase == "design" and "器件" in out:
            return f"{out['器件']} 器件 / {out.get('网络', '?')} 网络"
        if self.phase == "critic" and "补丁" in out:
            return f"{out['补丁']} 条补丁"
        if self.phase == "patch" and "应用" in out:
            return f"应用 {out['应用']} / 拒绝 {out.get('拒绝', 0)}"
        if self.phase == "analyze" and out.get("板卡"):
            return str(out["板卡"])
        if self.phase == "inject" and out.get("日志"):
            return str(out["日志"][0])[:20]
        return ""


@dataclass
class StepRecorder:
    """按事件顺序折叠出步骤列表，并维护全局计数（耗时、轮次、调用）。"""

    steps: list[Step] = field(default_factory=list)
    errors: int = 0
    warnings: int = 0
    calls: int = 0
    tokens: int = 0
    round_no: int = 0
    max_rounds: int = 3
    started_at: float = field(default_factory=time.perf_counter)
    finished: bool = False
    revision: int = 0

    # -- 订阅 Tracer ------------------------------------------------------ #
    def consume(self, record: dict[str, Any]) -> bool:
        """吃一条事件，返回步骤列表是否变化（界面据此决定要不要重画）。"""
        kind = str(record.get("kind", ""))
        if kind == "step":
            return self._on_step(record)
        if kind == "issues":
            self.errors = int(record.get("errors") or 0)
            self.warnings = int(record.get("warnings") or 0)
            self.round_no = int(record.get("round") or self.round_no)
            self._attach_note(record, f"第 {self.round_no} 轮：{self.errors} 错 / {self.warnings} 警")
            return True
        if kind == "sim":
            self._attach_note(
                record,
                f"仿真 {record.get('version') or ''} {record.get('elapsed') or 0}s"
                f"｜未建模 {record.get('unmodeled') or 0} 个",
            )
            return False
        if kind == "prune":
            self._attach_note(record, f"护栏剔除子电路 {record.get('subcircuit')}", phase="design")
            return False
        if kind == "inject_fault":
            lines = record.get("log") or []
            self._attach_note(record, "；".join(str(x) for x in lines[:2]), phase="inject")
            return False
        if kind == "revise":
            self.revision += 1
            self._attach_note(record, f"修订 #{self.revision}：{record.get('request')}", phase="revise")
            return False
        return False

    def _on_step(self, record: dict[str, Any]) -> bool:
        phase = str(record.get("phase") or "step")
        status = str(record.get("status") or "end")
        if status == "begin":
            self.steps.append(
                Step(
                    key=f"{phase}#{len(self.steps) + 1}",
                    phase=phase,
                    title=str(record.get("title") or TITLES.get(phase, phase)),
                    status="running",
                    inputs=dict(record.get("input") or {}),
                    t_start=float(record.get("t") or 0.0),
                )
            )
            return True

        step = self._last(phase, status_open=True)
        if step is None:  # 没有 begin 也要能显示（例如外部直接发 end）
            step = Step(
                key=f"{phase}#{len(self.steps) + 1}",
                phase=phase,
                title=str(record.get("title") or TITLES.get(phase, phase)),
                t_start=float(record.get("t") or 0.0),
            )
            self.steps.append(step)

        step.outputs.update(record.get("output") or {})
        step.notes.extend(str(x) for x in (record.get("notes") or []))
        # 显式的 0.0 是"很快"，不是"没测到"；只有字段缺失才是 None
        raw_elapsed = record.get("elapsed")
        step.elapsed = float(raw_elapsed) if raw_elapsed is not None else None
        if status == "skip":
            step.status = "skip"
        elif record.get("ok") is False:
            step.status = "fail"
        elif record.get("warn"):
            step.status = "warn"
        else:
            step.status = "ok"
        return True

    def _last(self, phase: str, status_open: bool = False) -> Step | None:
        for step in reversed(self.steps):
            if step.phase != phase:
                continue
            if status_open and step.status != "running":
                continue
            return step
        return None

    def _attach_note(self, record: dict[str, Any], text: str, phase: str = "evaluate") -> None:
        if not text:
            return
        step = self._last(phase)
        if step is not None:
            step.notes.append(text)

    # -- 供界面使用 ------------------------------------------------------ #
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started_at

    def running(self) -> Step | None:
        for step in reversed(self.steps):
            if step.status == "running":
                return step
        return None

    def counts(self) -> dict[str, int]:
        out = {"ok": 0, "warn": 0, "fail": 0, "running": 0, "skip": 0}
        for step in self.steps:
            out[step.status] = out.get(step.status, 0) + 1
        return out

    def replay(self, events: Iterable[dict[str, Any]]) -> "StepRecorder":
        for record in events:
            self.consume(record)
        return self
