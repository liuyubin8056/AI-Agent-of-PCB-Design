"""端到端测试：一句话需求 -> 产物落地 -> 闭环返修 -> 追问改需求。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcb_agent.config import Settings
from pcb_agent.orchestrator import Orchestrator


def make_orch(tmp_path: Path) -> Orchestrator:
    # 强制离线，保证 CI / 无网络 / 无密钥环境都能跑
    settings = Settings(offline=True, api_key="", model="", max_rounds=3)
    return Orchestrator(settings=settings, out_dir=tmp_path)


def test_demo_end_to_end(tmp_path):
    orch = make_orch(tmp_path)
    result = orch.design("帮我设计一块 Arduino 最小系统板，带指示灯和 I2C 传感器", tmp_path)
    assert result.ok, [str(i) for i in result.issues]
    assert len(result.ir.bom) >= 10
    assert Path(result.artifacts["原理图"]).exists()
    assert Path(result.artifacts["BOM"]).exists()
    assert Path(tmp_path / "summary.json").exists()
    assert Path(tmp_path / "trace.jsonl").exists()


def test_fault_is_repaired_by_loop(tmp_path):
    """注入故障后，Agent 必须自己发现并修好——这是闭环的证据。"""
    orch = make_orch(tmp_path)
    result = orch.design(
        "帮我设计一块 Arduino 最小系统板，带指示灯和 I2C 传感器",
        tmp_path,
        inject_fault="decoupling",
    )
    assert result.rounds >= 2
    assert result.ok
    assert any("注入故障" in x for x in result.log)
    assert any("新增" in x for x in result.log)


def test_revision_keeps_rest_of_design(tmp_path):
    orch = make_orch(tmp_path)
    base = orch.design("帮我设计一块 Arduino 最小系统板，带指示灯", tmp_path)
    before = len(base.ir.bom)
    revised = orch.revise(base.ir, "把C3换成100nF", tmp_path / "rev")
    assert revised.ir.part("C3").value == "100nF"
    assert len(revised.ir.bom) == before
    assert revised.ok


def test_revision_without_match_is_reported(tmp_path):
    orch = make_orch(tmp_path)
    base = orch.design("帮我设计一块 Arduino 最小系统板", tmp_path)
    revised = orch.revise(base.ir, "随便说点别的", tmp_path / "rev2")
    assert revised.rounds == 0
    assert any("未能" in x for x in revised.log)


def test_different_request_picks_different_subcircuits(tmp_path):
    orch = make_orch(tmp_path)
    result = orch.design("做一个纯 MCU 最小系统板，不需要传感器和指示灯", tmp_path)
    assert "i2c_sensor" not in result.ir.meta["subcircuits"]
    assert result.ok


def test_summary_json_shape(tmp_path):
    import json

    orch = make_orch(tmp_path)
    orch.design("帮我设计一块 Arduino 最小系统板", tmp_path)
    data = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    for key in ("title", "rounds", "errors", "parts", "nets", "issues"):
        assert key in data


@pytest.mark.parametrize("fault", ["decoupling", "i2c", "reset"])
def test_each_fault_gets_fixed(tmp_path, fault):
    orch = make_orch(tmp_path / fault)
    result = orch.design("Arduino 最小系统板，带指示灯和 I2C", tmp_path / fault, inject_fault=fault)
    assert result.ok
