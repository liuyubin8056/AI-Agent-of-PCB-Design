"""IR 数据结构测试：序列化往返 + 结构自检。"""

from __future__ import annotations

from pcb_agent.ir import BoardSpec, BomItem, DesignIR, Net, net_kind


def _minimal_ir() -> DesignIR:
    return DesignIR(
        spec=BoardSpec(title="测试板"),
        bom=[
            BomItem("U1", "pcbagent:MCU_ATmega328P", "ATmega328P-PU"),
            BomItem("C1", "pcbagent:CAP", "100nF"),
        ],
        nets=[Net("+5V", ["U1.7", "C1.1"]), Net("GND", ["U1.8", "C1.2"])],
    )


def test_roundtrip_json(tmp_path):
    ir = _minimal_ir()
    path = ir.to_json(tmp_path / "design.json")
    back = DesignIR.load_json(path)
    assert back.to_dict() == ir.to_dict()


def test_net_kind_inference():
    assert net_kind("GND") == "ground"
    assert net_kind("AGND") == "ground"
    assert net_kind("+5V") == "power"
    assert net_kind("VCC") == "power"
    assert net_kind("SDA") == "signal"


def test_structural_clean():
    assert _minimal_ir().structural_issues() == []


def test_duplicate_ref_detected():
    ir = _minimal_ir()
    ir.bom.append(BomItem("U1", "pcbagent:RES", "10k"))
    codes = [i.code for i in ir.structural_issues()]
    assert "BOM_DUP_REF" in codes


def test_bad_member_format_detected():
    ir = _minimal_ir()
    ir.nets.append(Net("BAD", ["U1", "C1.1"]))
    codes = [i.code for i in ir.structural_issues()]
    assert "NET_BAD_MEMBER" in codes


def test_summary_line():
    assert "2 个器件" in _minimal_ir().summary_line()
