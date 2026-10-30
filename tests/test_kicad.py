"""KiCad 真机集成测试：装了 KiCad 才跑，没装自动跳过。

验证三件事（也是 docs/kicad-notes.md 里的检查清单）：
1. 生成的 .kicad_sch 能被 kicad-cli 解析；
2. 从原理图导出的网表与 IR 完全一致（证明「原理图 == 网表」）；
3. ERC 没有 error 级违规。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pcb_agent.config import Settings
from pcb_agent.ir import DesignIR
from pcb_agent.kicad_gate import KiCadGate, diff_with_ir


@pytest.fixture(scope="module")
def gate() -> KiCadGate:
    settings = Settings.load()
    g = KiCadGate(settings)
    if not g.available:
        pytest.skip("本机未安装 kicad-cli，跳过真机集成测试")
    return g


def test_schematic_exports_matching_netlist(tmp_path, gate):
    from pcb_agent.subcircuits import build_board
    from pcb_agent.ir import BoardSpec
    from pcb_agent.render import write_schematic
    from pcb_agent.library import Library

    lib = Library()
    parts, nets, _ = build_board(
        [
            {"id": "mcu_min_system"},
            {"id": "power_ldo5v"},
            {"id": "led_indicator"},
            {"id": "i2c_sensor"},
        ],
        lib,
    )
    ir = DesignIR(spec=BoardSpec(title="集成测试板"), bom=parts, nets=nets)
    sch = write_schematic(ir, tmp_path / "board.kicad_sch", lib)

    res = gate.export_netlist(sch, tmp_path / "board.net")
    assert res.ok, res.raw

    issues = diff_with_ir(ir, tmp_path / "board.net")
    assert issues == [], [str(i) for i in issues]


def test_erc_has_no_error(tmp_path, gate):
    from pcb_agent.library import Library
    from pcb_agent.render import write_schematic
    from pcb_agent.subcircuits import build_board
    from pcb_agent.ir import BoardSpec

    lib = Library()
    parts, nets, _ = build_board(
        [{"id": "mcu_min_system"}, {"id": "power_ldo5v"}, {"id": "led_indicator"}], lib
    )
    ir = DesignIR(spec=BoardSpec(title="ERC 测试板"), bom=parts, nets=nets)
    sch = write_schematic(ir, tmp_path / "board.kicad_sch", lib)

    res = gate.run_erc(sch, tmp_path / "erc.json")
    assert res.available
    errors = [i for i in res.issues if i.severity == "error"]
    assert errors == [], [str(i) for i in errors]


def test_gate_reports_path(gate):
    assert Path(gate.cli).name.lower() == "kicad-cli.exe"
    assert gate.version()[0].isdigit()
