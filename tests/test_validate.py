"""规则校验器测试：好设计要放行，坏设计要抓住。"""

from __future__ import annotations

import pytest

from pcb_agent.ir import BoardSpec, DesignIR
from pcb_agent.library import Library
from pcb_agent.subcircuits import build_board
from pcb_agent.validate import counts, run_all

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
    return DesignIR(spec=BoardSpec(title="测试板"), bom=parts, nets=nets)


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def test_clean_design_has_no_error():
    issues = run_all(make_ir(), LIB)
    assert counts(issues)[0] == 0, [str(i) for i in issues]


def test_missing_decoupling_caught():
    ir = make_ir()
    cap = next(i for i in ir.bom if i.value == "100nF" and i.ref.startswith("C"))
    ir.bom = [x for x in ir.bom if x.ref != cap.ref]
    for net in ir.nets:
        net.members = [m for m in net.members if not m.startswith(cap.ref + ".")]
    assert "POWER_RAIL_NO_DECOUPLING" in codes(run_all(ir, LIB))


def test_missing_i2c_pullup_caught():
    ir = make_ir()
    sda = next(n for n in ir.nets if n.name == "SDA")
    pull = next(m for m in sda.members if m.startswith("R"))
    ref = pull.split(".")[0]
    ir.bom = [x for x in ir.bom if x.ref != ref]
    for net in ir.nets:
        net.members = [m for m in net.members if not m.startswith(ref + ".")]
    assert "I2C_NO_PULLUP" in codes(run_all(ir, LIB))


def test_missing_reset_pullup_caught():
    ir = make_ir()
    reset = next(n for n in ir.nets if n.name == "RESET")
    pull = next(m for m in reset.members if m.startswith("R"))
    ref = pull.split(".")[0]
    for net in ir.nets:
        net.members = [m for m in net.members if not m.startswith(ref + ".")]
    ir.bom = [x for x in ir.bom if x.ref != ref]
    assert "RESET_NO_PULLUP" in codes(run_all(ir, LIB))


def test_fake_pin_number_caught():
    """引脚幻觉必须被抓住：这是本项目最重要的一条防线。"""
    ir = make_ir()
    ir.nets.append(type(ir.nets[0])(name="FAKE", members=["U1.99", "C1.1"]))
    issues = run_all(ir, LIB)
    assert "PIN_NOT_EXIST" in codes(issues)


def test_pin_in_two_nets_caught():
    ir = make_ir()
    ir.nets.append(type(ir.nets[0])(name="EXTRA", members=["U1.7", "R1.1"]))
    assert "PIN_MULTIPLE_NETS" in codes(run_all(ir, LIB))


def test_unconnected_power_pin_caught():
    ir = make_ir()
    for net in ir.nets:
        net.members = [m for m in net.members if m != "U1.20"]
    issues = run_all(ir, LIB)
    assert "POWER_PIN_UNCONNECTED" in codes(issues)


def test_unknown_symbol_caught():
    ir = make_ir()
    ir.bom[0].symbol = "pcbagent:NOT_A_PART"
    assert "LIB_UNKNOWN_SYMBOL" in codes(run_all(ir, LIB))


def test_missing_crystal_load_cap_caught():
    ir = make_ir()
    xtal1 = next(n for n in ir.nets if n.name == "XTAL1")
    cap = next(m for m in xtal1.members if m.startswith("C"))
    ref = cap.split(".")[0]
    for net in ir.nets:
        net.members = [m for m in net.members if not m.startswith(ref + ".")]
    ir.bom = [x for x in ir.bom if x.ref != ref]
    assert "XTAL_NO_LOAD_CAP" in codes(run_all(ir, LIB))


def test_unconnected_part_caught():
    """只 add_part 不 connect（真实 LLM 样本）必须报 error，不能只是 warning。"""
    from pcb_agent.ir import BomItem

    ir = make_ir()
    ir.bom.append(BomItem("C99", "pcbagent:CAP", "10uF"))
    issues = run_all(ir, LIB)
    codes = [i.code for i in issues if i.severity == "error"]
    assert "PART_UNCONNECTED" in codes
