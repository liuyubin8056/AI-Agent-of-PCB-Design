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
    _needs_power_flag,
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


def _flag_count(ir) -> int:
    return sum(
        1
        for n in ir.nets
        if n.kind in {"power", "ground"} and _needs_power_flag(ir, LIB, n.name)
    )


def test_symbol_count_matches_parts_plus_flags():
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    placed = [n for n in root if isinstance(n, list) and n and n[0] == "symbol"]
    assert len(placed) == len(ir.bom) + _flag_count(ir)


def test_global_label_count_matches_members():
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    labels = [n for n in root if isinstance(n, list) and n and n[0] == "global_label"]
    members = sum(len(n.members) for n in ir.nets)
    assert len(labels) == members + _flag_count(ir)


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


# --------------------------------------------------------------------------- #
# 以下用例锁定「真机 ERC 通过」所依赖的三条渲染契约
# --------------------------------------------------------------------------- #
def _top_nodes(text: str, tag: str) -> list:
    return [n for n in _top(text) if isinstance(n, list) and n and n[0] == tag]


def test_floating_pins_get_no_connect():
    """未连接引脚必须有 no_connect，否则 KiCad ERC 报 pin_not_connected。"""
    ir = make_ir()
    text = render_schematic(ir, LIB)
    connected = sum(len(n.members) for n in ir.nets)
    total_pins = sum(len(LIB.pins(i.symbol)) for i in ir.bom)
    assert len(_top_nodes(text, "no_connect")) == total_pins - connected


def test_power_flag_only_when_no_driver():
    """LDO 的 VOUT 已经驱动 +5V，就不能再加 PWR_FLAG，否则 pin_to_pin 报错。"""
    ir = make_ir()
    text = render_schematic(ir, LIB)
    flags = [n for n in _top_nodes(text, "symbol") if "PWR_FLAG" in str(n)]
    flagged_nets = {
        n[1] for n in _top_nodes(text, "global_label") if isinstance(n[1], str)
    }
    assert "GND" in flagged_nets
    # 3 条电源网络里，只有 GND 与 VIN_RAW 缺 power_out 驱动
    assert len(flags) == 2


def _at_of(node: list) -> tuple[float, float] | None:
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == "at":
            return float(child[1]), float(child[2])
    return None


def test_all_connection_points_on_grid():
    """引脚端点 / 标签 / 器件原点必须落在 1.27 mm 栅格，否则 ERC 报 endpoint_off_grid。

    只检查连接点，属性文本的位置（如 1.905）不需要对栅格。
    """
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    checked = 0
    for node in root:
        if not (isinstance(node, list) and node and node[0] in {"no_connect", "global_label", "symbol"}):
            continue
        pos = _at_of(node)
        if pos is None:
            continue
        checked += 1
        for v in pos:
            assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, f"{v} 不在 1.27 栅格上"
    assert checked >= sum(len(n.members) for n in ir.nets)


def test_symbol_lib_and_table_written(tmp_path):
    ir = make_ir()
    write_schematic(ir, tmp_path / "board.kicad_sch", LIB)
    sym = tmp_path / "pcbagent.kicad_sym"
    table = tmp_path / "sym-lib-table"
    assert sym.exists() and table.exists()
    assert sym.read_text(encoding="utf-8").startswith("(kicad_symbol_lib")
    # .kicad_sym 里符号名不带库前缀
    assert '"MCU_ATmega328P"' in sym.read_text(encoding="utf-8")
    assert "${KIPRJMOD}" in table.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 以下用例锁定「真机 ERC 通过」所依赖的三条渲染契约
# --------------------------------------------------------------------------- #
def _top_nodes(text: str, tag: str) -> list:
    return [n for n in _top(text) if isinstance(n, list) and n and n[0] == tag]


def test_floating_pins_get_no_connect():
    """未连接引脚必须有 no_connect，否则 KiCad ERC 报 pin_not_connected。"""
    ir = make_ir()
    text = render_schematic(ir, LIB)
    connected = sum(len(n.members) for n in ir.nets)
    total_pins = sum(len(LIB.pins(i.symbol)) for i in ir.bom)
    assert len(_top_nodes(text, "no_connect")) == total_pins - connected


def test_power_flag_only_when_no_driver():
    """LDO 的 VOUT 已经驱动 +5V，就不能再加 PWR_FLAG，否则 pin_to_pin 报错。"""
    ir = make_ir()
    text = render_schematic(ir, LIB)
    flags = [n for n in _top_nodes(text, "symbol") if "PWR_FLAG" in str(n)]
    flagged_nets = {
        n[1] for n in _top_nodes(text, "global_label") if isinstance(n[1], str)
    }
    assert "GND" in flagged_nets
    # 3 条电源网络里，只有 GND 与 VIN_RAW 缺 power_out 驱动
    assert len(flags) == 2


def _at_of(node: list) -> tuple[float, float] | None:
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == "at":
            return float(child[1]), float(child[2])
    return None


def test_all_connection_points_on_grid():
    """引脚端点 / 标签 / 器件原点必须落在 1.27 mm 栅格，否则 ERC 报 endpoint_off_grid。

    只检查连接点，属性文本的位置（如 1.905）不需要对栅格。
    """
    ir = make_ir()
    root = _top(render_schematic(ir, LIB))
    checked = 0
    for node in root:
        if not (isinstance(node, list) and node and node[0] in {"no_connect", "global_label", "symbol"}):
            continue
        pos = _at_of(node)
        if pos is None:
            continue
        checked += 1
        for v in pos:
            assert abs(v / 1.27 - round(v / 1.27)) < 1e-6, f"{v} 不在 1.27 栅格上"
    assert checked >= sum(len(n.members) for n in ir.nets)


def test_symbol_lib_and_table_written(tmp_path):
    ir = make_ir()
    write_schematic(ir, tmp_path / "board.kicad_sch", LIB)
    sym = tmp_path / "pcbagent.kicad_sym"
    table = tmp_path / "sym-lib-table"
    assert sym.exists() and table.exists()
    assert sym.read_text(encoding="utf-8").startswith("(kicad_symbol_lib")
    # .kicad_sym 里符号名不带库前缀
    assert '"MCU_ATmega328P"' in sym.read_text(encoding="utf-8")
    assert "${KIPRJMOD}" in table.read_text(encoding="utf-8")
