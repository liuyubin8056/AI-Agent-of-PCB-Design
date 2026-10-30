"""电路设计 Agent：BoardSpec -> DesignIR。

关键约束（也是本项目的核心手法）：它**不写网表**，只做选择题：
选哪几个子电路、每个子电路的参数填什么。真正的引脚级连接由 ``subcircuits``
里的确定性模板生成，引脚号从器件库查，所以模型没有机会编造引脚。
"""

from __future__ import annotations

import json

from .. import prompts
from ..ir import BoardSpec, DesignIR, Issue
from ..llm import LLMError
from ..subcircuits import SUB_CIRCUITS, build_board
from .base import Agent


class DesignerAgent(Agent):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "designer")
        kwargs.setdefault("role", "电路设计")
        super().__init__(**kwargs)

    def design(
        self,
        spec: BoardSpec,
        feedback: list[Issue] | None = None,
        round_no: int = 1,
    ) -> DesignIR:
        feedback = feedback or []
        selections: list[dict] = []
        source = "heuristic"

        if not self.offline:
            try:
                feedback_text = ""
                if feedback:
                    feedback_text = "\n".join(f"- {i}" for i in feedback[:10])
                data = self.llm.complete_json(
                    prompts.DESIGNER_SYSTEM,
                    prompts.designer_user(
                        json.dumps(spec.to_dict(), ensure_ascii=False), self.lib, feedback_text
                    ),
                )
                selections = self._sanitize(data.get("subcircuits") or [])
                source = "llm"
            except (LLMError, ValueError, TypeError) as exc:
                self.notes.append(f"设计 LLM 失败，转规则兜底：{exc}")
                selections = []

        if not selections:
            selections = self._select_offline(spec)
            if source == "llm":
                source = "llm->heuristic"

        parts, nets, log = build_board(selections, self.lib)
        ir = DesignIR(
            spec=spec,
            bom=parts,
            nets=nets,
            meta={
                "subcircuits": [s["id"] for s in selections],
                "selection_source": source,
                "build_log": log,
            },
        )
        self.log(
            round_no,
            action="design",
            source=source,
            subcircuits=ir.meta["subcircuits"],
            parts=len(parts),
            nets=len(nets),
        )
        return ir

    # ------------------------------------------------------------------ #
    def _sanitize(self, raw: list) -> list[dict]:
        """把模型给的子电路选择过滤成「器件库里真实存在的」。"""
        clean: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id", "")).strip()
            if sid not in SUB_CIRCUITS:
                self.notes.append(f"忽略不存在的子电路 {sid!r}")
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            allowed = SUB_CIRCUITS[sid].params
            kept = {k: v for k, v in params.items() if k in allowed}
            if len(kept) != len(params):
                dropped = set(params) - set(kept)
                self.notes.append(f"{sid} 丢弃未声明参数：{', '.join(sorted(dropped))}")
            clean.append({"id": sid, "params": kept})
        return clean

    def _select_offline(self, spec: BoardSpec) -> list[dict]:
        blob = " ".join(
            [spec.title, spec.summary, " ".join(spec.functions), " ".join(spec.interfaces)]
        ).lower()
        selections = [{"id": "mcu_min_system", "params": {}}]
        # 任何一块板都需要供电，默认上 5V 稳压
        selections.append({"id": "power_ldo5v", "params": {}})
        if any(k in blob for k in ("led", "指示", "心跳")):
            selections.append({"id": "led_indicator", "params": {}})
        if any(k in blob for k in ("i2c", "传感器", "sensor")):
            selections.append({"id": "i2c_sensor", "params": {}})
        return selections
