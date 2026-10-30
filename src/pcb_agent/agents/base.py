"""Agent 基类：一个角色 = 一份 system prompt + 一组白名单工具 + 独立轨迹。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient
from ..library import Library
from ..trace import Tracer


@dataclass
class Agent:
    name: str
    role: str
    llm: LLMClient
    lib: Library
    tracer: Tracer | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def offline(self) -> bool:
        return not self.llm.available

    @property
    def mode(self) -> str:
        return "规则兜底" if self.offline else f"LLM({self.llm.settings.model})"

    def log(self, round_no: int, **payload: Any) -> None:
        if self.tracer:
            self.tracer.node(self.name, round_no=round_no, mode=self.mode, **payload)
