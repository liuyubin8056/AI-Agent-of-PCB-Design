"""渲染器测试：在没装 KiCad 的机器上也能验证结构正确性。

三层自检：
1. 括号/字符串配平；
2. 能被 s-expression 解析器完整解析；
3. 器件数、标签数、引脚端点与 IR 严格对应（这是「原理图 == 网表」的证据）。
"""

from __future__ import annotations

from pcb_agent.ir import BoardSpec, DesignIR
from pcb_agent.kicad_gate import parse_sexpr
from pcb_agent.library import Library
from pcb_agent.render import (
    BODY_HALF_W,
    PIN_LENGTH,
    balance_check,
    geometry,
    render_schematic,
    write_bom_csv,
    write_schematic,
)
from pcb_agent.subcircuits import build_board

LIB = Library()


def make_ir() -> DesignIR:
    parts, nets, _ = build_board(
        [
            {"id": "mcu_min_system"},
            {"id": "power_ldo5v"},
            {"id": "led_indicator"},
            {"id": "i2c_sensor"},
        ],
        LIB,
    )
    return DesignIR(spec=BoardSpec(title="渲染测试板", summary="用于验证渲染器"), bom=parts, nets=nets)


def _top(text: str) -> list:
    tree = parse_sexpr(text)
    assert len(tree) == 1 and tree[0][0] == "kicad_sch"
    return tree[0]


def test_balance_and_parse():
    text = render_schematic(make_ir(), LIB)
    ok, msg = balance_check(text)
    assert ok, msg
    root = _top(text)


def test_header_fields():
    root = _top(render_schematic(make_ir(), LIB))
    tags = {n[0] for n in root if isinstance(n, list)}
    assert {"version", "uuid", "paper", "title_block", "lib_symbols", "sheet_instances"} <= tags


def test_symbol_count_matches_parts_plus_flags():
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    placed = [n for n in root if isinstance(n, list) and n and n[0] == "symbol"]
    power_nets = [n for n in ir.nets if n.kind in {"power", "ground"}]
    assert len(placed) == len(ir.bom) + len(power_nets)


def test_global_label_count_matches_members():
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    labels = [n for n in root if isinstance(n, list) and n and n[0] == "global_label"]
    members = sum(len(n.members) for n in ir.nets)
    power_nets = [n for n in ir.nets if n.kind in {"power", "ground"}]
    assert len(labels) == members + len(power_nets)


def test_every_used_symbol_is_embedded():
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    lib_symbols = next(n for n in root if isinstance(n, list) and n[0] == "lib_symbols")
    defined = {s[1] for s in lib_symbols[1:] if isinstance(s, list) and s[0] == "symbol"}
    for item in ir.bom:
        assert item.symbol in defined


def test_geometry_pin_endpoints_outside_body():
    geom = geometry(LIB, "pcbagent:MCU_ATmega328P")
    assert len(geom.pins) == 28
    for pin in geom.pins:
        assert abs(pin.px) == BODY_HALF_W + PIN_LENGTH


def test_write_artifacts(tmp_path):
    ir = make_ir()
    sch = write_schematic(ir, tmp_path / "board.kicad_sch", LIB)
    bom = write_bom_csv(ir, tmp_path / "bom.csv")
    assert sch.read_text(encoding="utf-8").startswith("(kicad_sch")
    header = bom.read_text(encoding="utf-8-sig").splitlines()[0]
    assert "位号" in header
    assert len(bom.read_text(encoding="utf-8-sig").splitlines()) == len(ir.bom) + 1
