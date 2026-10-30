"""需求分析 Agent：自然语言 -> BoardSpec。

这一环的失败模式是「过度解读」：用户说「做个最小系统板」，它给你加上蓝牙和屏幕。
所以 prompt 明确要求「只写需求层面的东西」，并且离线兜底只做关键词映射。
"""

from __future__ import annotations

import json

from .. import prompts
from ..ir import BoardSpec
from ..llm import LLMError
from .base import Agent

_KEYWORD_MAP = (
    ("arduino", "兼容 Arduino 的 MCU 最小系统"),
    ("最小系统", "MCU 最小系统"),
    ("单片机", "MCU 主控"),
    ("i2c", "I2C 传感器接入"),
    ("传感器", "传感器接入"),
    ("led", "LED 状态指示"),
    ("指示", "LED 状态指示"),
    ("电源", "稳压供电"),
    ("串口", "UART 调试接口"),
    ("uart", "UART 调试接口"),
)


class AnalystAgent(Agent):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "analyst")
        kwargs.setdefault("role", "需求分析")
        super().__init__(**kwargs)

    def analyze(self, request: str, round_no: int = 1) -> BoardSpec:
        spec: BoardSpec
        if self.offline:
            spec = self._heuristic(request)
            self.log(round_no, action="heuristic", title=spec.title)
        else:
            try:
                data = self.llm.complete_json(
                    prompts.ANALYST_SYSTEM, prompts.analyst_user(request)
                )
                spec = BoardSpec.from_dict(data)
                self.log(round_no, action="llm", title=spec.title)
            except (LLMError, ValueError) as exc:
                self.notes.append(f"需求分析 LLM 失败，转规则兜底：{exc}")
                spec = self._heuristic(request)
                self.log(round_no, action="fallback", error=str(exc)[:120])
        if not spec.title:
            spec.title = "未命名板卡"
        self.last_spec_json = json.dumps(spec.to_dict(), ensure_ascii=False)
        return spec

    # ------------------------------------------------------------------ #
    def _heuristic(self, request: str) -> BoardSpec:
        text = (request or "").strip()
        low = text.lower()
        functions: list[str] = []
        interfaces: list[str] = []
        for key, desc in _KEYWORD_MAP:
            if key in low and desc not in functions:
                functions.append(desc)
        if any(k in low for k in ("arduino", "最小系统", "单片机")):
            functions = [f for f in functions if f not in {"MCU 最小系统", "兼容 Arduino 的 MCU 最小系统"}]
            functions.insert(0, "兼容 Arduino 的 MCU 最小系统")
            interfaces.append("GPIO 排针")
        if any(k in low for k in ("i2c", "传感器")):
            interfaces.append("I2C")
        if any(k in low for k in ("电源", "适配器", "12v", "供电")):
            functions.append("7~12V 输入稳压到 5V")

        title = "Arduino 最小系统板" if "arduino" in low or "最小系统" in low else "自定义功能板"
        return BoardSpec(
            title=title,
            summary=text[:80] or "由一句话需求生成的电路板",
            functions=functions or ["MCU 最小系统"],
            supply_rails=["+5V", "GND"],
            interfaces=interfaces or ["GPIO"],
            key_parts=["ATmega328P", "AMS1117-5.0"],
            constraints={"供电": "7~12V 直流输入", "尺寸": "约 60 x 40 mm"},
        )
