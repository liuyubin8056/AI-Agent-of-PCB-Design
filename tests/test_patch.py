"""补丁机制测试：每个原子操作的行为契约。"""

from __future__ import annotations

from pcb_agent.ir import BoardSpec, DesignIR
from pcb_agent.library import Library
from pcb_agent.patch import PatchOp, apply, ops_from_text
from pcb_agent.subcircuits import build_board

LIB = Library()


def make_ir() -> DesignIR:
    parts, nets, _ = build_board(
        [{"id": "mcu_min_system"}, {"id": "power_ldo5v"}, {"id": "i2c_sensor"}], LIB
    )
    return DesignIR(spec=BoardSpec(title="测试板"), bom=parts, nets=nets)


def test_set_value_changes_only_target():
    ir = make_ir()
    new, log = apply(ir, [PatchOp("set_value", ref="C3", value="100nF")], LIB)
    assert new.part("C3").value == "100nF"
    assert ir.part("C3").value != "100nF"  # 原对象不被修改
    assert len(new.bom) == len(ir.bom)
    assert any("C3" in x for x in log)


def test_remove_part_cleans_nets():
    ir = make_ir()
    new, _ = apply(ir, [PatchOp("remove_part", ref="D1")], LIB)
    assert new.part("D1") is None
    for net in new.nets:
        assert not any(m.startswith("D1.") for m in net.members)


def test_add_part_and_connect():
    ir = make_ir()
    ops = [
        PatchOp("add_part", ref="C99", symbol="pcbagent:CAP", value="1uF"),
        PatchOp("connect", net="+5V", member="C99.1"),
        PatchOp("connect", net="GND", member="C99.2"),
    ]
    new, _ = apply(ir, ops, LIB)
    assert new.part("C99").value == "1uF"
    assert "C99.1" in new.net("+5V").members
    assert "C99.2" in new.net("GND").members


def test_disconnect_removes_member():
    ir = make_ir()
    new, _ = apply(ir, [PatchOp("disconnect", net="SDA", member="R3.1")], LIB)
    assert "R3.1" not in new.net("SDA").members


def test_rename_net():
    ir = make_ir()
    new, _ = apply(ir, [PatchOp("rename_net", net="SDA", new_name="I2C_SDA")], LIB)
    assert new.net("I2C_SDA") is not None
    assert new.net("SDA") is None


def test_unknown_ref_is_skipped_not_crash():
    ir = make_ir()
    new, log = apply(ir, [PatchOp("set_value", ref="X99", value="1k")], LIB)
    assert new.to_dict() == ir.to_dict()
    assert any("找不到 X99" in x for x in log)


def test_ops_from_text_set_value():
    ir = make_ir()
    ops = ops_from_text("把C3换成100nF", ir)
    assert len(ops) == 1
    assert ops[0].op == "set_value" and ops[0].ref == "C3" and ops[0].value == "100nF"


def test_ops_from_text_add_part():
    ir = make_ir()
    ops = ops_from_text("增加一颗 1uF 电容", ir)
    assert ops and ops[0].op == "add_part"


def test_ops_from_text_no_match():
    ir = make_ir()
    assert ops_from_text("随便聊两句天气", ir) == []


# --------------------------------------------------------------------------- #
# 原子性与位号纠错：这两条来自真实 LLM 的失败样本
# --------------------------------------------------------------------------- #
def test_unknown_symbol_rejects_whole_group():
    """模型给 Device:XYZ 这类库里没有的符号时，配套 connect 也必须一起丢弃。"""
    ir = make_ir()
    ops = [
        PatchOp("add_part", ref="C99", symbol="Device:XYZ", value="100nF"),
        PatchOp("connect", net="+5V", member="C99.1"),
        PatchOp("connect", net="GND", member="C99.2"),
    ]
    res = apply(ir, ops, LIB)
    assert res.applied == 0
    assert res.rejected == 3
    assert res.ir.to_dict() == ir.to_dict()  # 原设计一字未改
    assert any("器件库中找不到" in x for x in res.log)


def test_kicad_official_lib_name_is_aliased():
    """Device:C / Device:R 这类官方库名应被别名表接住，不能直接拒。"""
    ir = make_ir()
    ops = [
        PatchOp("add_part", ref="C99", symbol="Device:C", value="1uF"),
        PatchOp("connect", net="+5V", member="C99.1"),
        PatchOp("connect", net="GND", member="C99.2"),
    ]
    res = apply(ir, ops, LIB)
    assert res.applied == 3
    assert res.ir.part("C99").symbol == "pcbagent:CAP"


def test_ref_typo_is_redirected():
    """模型写 add_part ref=C1 却 connect C8.1，应重定向到 C1 而不是留下悬空引用。"""
    ir = make_ir()
    ops = [
        PatchOp("add_part", ref="C99", symbol="pcbagent:CAP", value="100nF"),
        PatchOp("connect", net="+5V", member="C98.1"),
        PatchOp("connect", net="GND", member="C98.2"),
    ]
    res = apply(ir, ops, LIB)
    assert res.applied == 3
    assert "C99.1" in res.ir.net("+5V").members
    assert not any(m.startswith("C98.") for n in res.ir.nets for m in n.members)
    assert any("位号笔误纠正" in x for x in res.log)


def test_connect_to_ghost_ref_rejected():
    """没有任何 add_part 支撑的 connect 直接剔除，绝不允许悬空引用进网表。"""
    ir = make_ir()
    res = apply(ir, [PatchOp("connect", net="+5V", member="C77.1")], LIB)
    assert res.applied == 0
    assert res.rejected == 1
    assert res.ir.to_dict() == ir.to_dict()
