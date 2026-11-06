"""设计护栏测试：模型可以"提议"，但每个可选子电路必须有需求依据。"""

from __future__ import annotations

from pcb_agent.agents.designer import DesignerAgent
from pcb_agent.config import Settings
from pcb_agent.ir import BoardSpec
from pcb_agent.library import Library
from pcb_agent.llm import LLMClient

LIB = Library()


def make_agent() -> DesignerAgent:
    settings = Settings(offline=True, api_key="", model="", kicad_enabled=False)
    return DesignerAgent(llm=LLMClient(settings), lib=LIB)


def spec_of(request: str) -> BoardSpec:
    # 模拟需求分析 Agent「按常见做法补齐」：把指示灯写进了 functions
    return BoardSpec(
        title="测试板",
        summary=request,
        functions=["MCU 最小系统", "电源指示"],  # 分析 Agent 自己加的
        interfaces=["I2C"],
    )


ALL = [
    {"id": "mcu_min_system", "params": {}},
    {"id": "power_ldo5v", "params": {}},
    {"id": "led_indicator", "params": {}},
    {"id": "i2c_sensor", "params": {}},
]


def test_prune_uses_raw_request_not_spec():
    """需求原话没提指示灯，就不该因为它出现在 spec.functions 里而保留。"""
    agent = make_agent()
    kept = agent._prune_unsupported(
        list(ALL), spec_of("做一块最小系统板"), "llm", raw_request="做一块最小系统板"
    )
    ids = [s["id"] for s in kept]
    assert "led_indicator" not in ids
    assert "i2c_sensor" not in ids
    assert ids[0] == "mcu_min_system"


def test_prune_keeps_requested_modules():
    agent = make_agent()
    req = "做一块带指示灯和 I2C 传感器接口的最小系统板"
    kept = agent._prune_unsupported(list(ALL), spec_of(req), "llm", raw_request=req)
    ids = [s["id"] for s in kept]
    assert "led_indicator" in ids and "i2c_sensor" in ids


def test_prune_respects_negation():
    agent = make_agent()
    req = "做一块最小系统板，不要指示灯、不要传感器接口"
    kept = agent._prune_unsupported(list(ALL), spec_of(req), "llm", raw_request=req)
    ids = [s["id"] for s in kept]
    assert "led_indicator" not in ids and "i2c_sensor" not in ids


def test_prune_restores_mcu():
    agent = make_agent()
    kept = agent._prune_unsupported(
        [{"id": "led_indicator", "params": {}}], spec_of("灯"), "llm", raw_request="灯"
    )
    assert any(s["id"] == "mcu_min_system" for s in kept)
