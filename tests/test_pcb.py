"""PCB 生成与自动布线测试（需要官方封装库；装 KiCad 才跑）。"""

from __future__ import annotations

import pytest

from pcb_agent.footprints import FootprintLibrary
from pcb_agent.ir import BoardSpec, DesignIR
from pcb_agent.pcb import build_board, write_pcb
from pcb_agent.router import route_board
from pcb_agent.subcircuits import build_board as build_ir

FP = FootprintLibrary()
pytestmark = pytest.mark.skipif(not FP.available, reason="未找到 KiCad 官方封装库")


@pytest.fixture(scope="module")
def ir() -> DesignIR:
    parts, nets, _ = build_ir(
        [
            {"id": "mcu_min_system"},
            {"id": "power_ldo5v"},
            {"id": "led_indicator"},
            {"id": "i2c_sensor"},
        ]
    )
    return DesignIR(spec=BoardSpec(title="PCB 测试板"), bom=parts, nets=nets)


def test_board_places_every_component(ir):
    board = build_board(ir, FP)
    assert len(board.placements) == len(ir.bom)
    assert board.width > 40 and board.height > 30


def test_components_do_not_overlap(ir):
    board = build_board(ir, FP)
    rects = [p.rect() for p in board.placements]
    for i, a in enumerate(rects):
        for b in rects[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], "封装重叠"


def test_written_file_is_kicad_pcb(ir, tmp_path):
    board = build_board(ir, FP)
    path = write_pcb(board, tmp_path / "board.kicad_pcb")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("(kicad_pcb")
    assert '"Edge.Cuts"' in text
    assert text.count("(footprint ") == len(ir.bom)


def test_router_routes_most_nets(ir):
    board = build_board(ir, FP)
    stats = route_board(board)
    assert stats.total >= 10
    assert stats.ratio >= 0.7, stats.describe()
    assert board.segments, "至少要有走线输出"


def test_drc_has_no_error(ir, tmp_path):
    """布完线后跑 DRC，error 必须为 0（警告允许）。"""
    from pcb_agent.config import Settings
    from pcb_agent.kicad_gate import KiCadGate

    gate = KiCadGate(Settings.load())
    if not gate.available:
        pytest.skip("未安装 kicad-cli")

    board = build_board(ir, FP)
    route_board(board)
    path = write_pcb(board, tmp_path / "board.kicad_pcb")
    res = gate.run_drc(path, tmp_path / "drc.json")
    errors = [i for i in res.issues if i.severity == "error"]
    assert errors == [], [str(e) for e in errors]
