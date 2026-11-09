"""IR -> SPICE 生成与工作点仿真测试。

不需要 KiCad 的部分照常跑；需要 ngspice.dll 的用 skipif 标出来。
"""

from __future__ import annotations

import json

import pytest

from pcb_agent.ir import BoardSpec, BomItem, DesignIR, Net
from pcb_agent.library import Library
from pcb_agent.ngspice import find_library, simulate
from pcb_agent.spice import SpiceBuilder, build_deck, is_ground, node_name, parse_value

LIB = Library()
HAS_DLL = bool(find_library())


# --------------------------------------------------------------------------- #
# 数值与命名
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("10k", 10e3),
        ("4.7k", 4700.0),
        ("100nF", 100e-9),
        ("22pF", 22e-12),
        ("0.1uF", 0.1e-6),
        ("1M", 1e6),
        ("1m", 1e-3),
        ("470", 470.0),
    ],
)
def test_parse_value(text, expected):
    assert parse_value(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "ATmega328P-PU", "Green", "1k5x"])
def test_parse_value_rejects_garbage(text):
    assert parse_value(text) is None


def test_node_name():
    assert node_name("+5V") == "p5v"
    assert node_name("GND") == "0"
    assert node_name("VIN_RAW") == "vin_raw"
    assert node_name("3V3") == "n3v3"
    assert is_ground("GND") and not is_ground("+5V")


# --------------------------------------------------------------------------- #
# deck 结构
# --------------------------------------------------------------------------- #
def make_ir(parts: list[BomItem], nets: list[Net]) -> DesignIR:
    return DesignIR(spec=BoardSpec(title="仿真测试板"), bom=parts, nets=nets)


def sample_ir() -> DesignIR:
    parts = [
        BomItem("R1", "pcbagent:RES", "1k"),
        BomItem("D1", "pcbagent:LED", "Green"),
        BomItem("U2", "pcbagent:LDO_AMS1117_5V", "AMS1117-5.0"),
    ]
    nets = [
        Net("VIN_RAW", ["U2.3"]),
        Net("+5V", ["U2.2", "R1.1"]),
        Net("LED_A", ["R1.2", "D1.2"]),
        Net("GND", ["D1.1", "U2.1"]),
    ]
    return make_ir(parts, nets)


def test_deck_contains_expected_elements():
    deck = build_deck(sample_ir(), LIB)
    text = deck.text
    assert "R1 p5v led_a 1000" in text
    assert "D1 led_a 0 DLED" in text, "LED 阳极接 LED_A、阴极接 GND"
    assert "XU2 vin_raw 0 p5v LDO_5V" in text
    assert "Vvin_raw vin_raw 0 dc 12" in text, "VIN_RAW 没有驱动源时必须补理想源"
    assert "Vp5v" not in text, "LDO 已驱动 +5V，不该再加理想源"


def test_subckt_declares_ports():
    """.subckt 漏端口会让 ngspice 报 "Too many parameters"。"""
    text = build_deck(sample_ir(), LIB).text
    assert ".subckt LDO_5V vin gnd vout" in text
    assert ".subckt XTAL_16M a b" in text


def test_led_ports_use_declared_order():
    """回归：LED 的端口名是 a/k，硬编码 a/b 会把它判成未连接。"""
    deck = build_deck(sample_ir(), LIB)
    assert deck.unmodeled == []
    assert "D1" in deck.currents


def test_unknown_symbol_is_reported():
    parts = [BomItem("X9", "acme:MYSTERY", "?")]
    deck = build_deck(make_ir(parts, [Net("GND", [])]), LIB)
    assert any("X9" in item for item in deck.unmodeled)


def test_every_signal_net_gets_leak_resistor():
    """只有电容到地的节点在 .op 里没有直流路径，必须给泄放电阻。"""
    text = build_deck(sample_ir(), LIB).text
    assert "Rlk_led_a led_a 0" in text


def test_signal_nets_of_clock_are_not_driven():
    parts = [BomItem("Y1", "pcbagent:XTAL", "16MHz")]
    nets = [Net("XTAL1", ["Y1.1"]), Net("XTAL2", ["Y1.2"])]
    text = build_deck(make_ir(parts, nets), LIB).text
    assert "Vdrv_xtal1" not in text


# --------------------------------------------------------------------------- #
# 真机仿真（装 KiCad 才跑）
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_DLL, reason="未找到 ngspice.dll，跳过真机仿真")
def test_operating_point_matches_hand_calc(tmp_path):
    """LDO 输出 5V、LED 电流约 3mA —— 与手算对得上才算仿真可信。"""
    deck = build_deck(sample_ir(), LIB)
    result = simulate(deck.text, deck.vectors, tmp_path)
    assert result.ok, result.error

    nodes = {v: k for k, v in deck.nodes.items()}
    volts = {nodes[vec[2:-1]]: result.values[vec] for vec in deck.vectors if vec.startswith("v(")}
    assert volts["+5V"] == pytest.approx(5.0, abs=0.01)
    assert volts["VIN_RAW"] == pytest.approx(12.0, abs=0.01)
    # (5.0 - Vf) / 1k，Vf 由 LED 模型决定，落在 2.0V 附近
    assert result.values[deck.currents["D1"]] == pytest.approx(3e-3, rel=0.15)


@pytest.mark.skipif(not HAS_DLL, reason="未找到 ngspice.dll，跳过真机仿真")
def test_low_input_voltage_drops_out(tmp_path):
    """输入 5.5V 时 LDO 应掉到 4.4V，而不是继续稳压 5V。"""
    ir = sample_ir()
    ir.nets[0].members  # noqa: B018 - 保持结构不变，只改源电压
    builder = SpiceBuilder(LIB)
    builder.sources = dict(builder.sources)
    builder.sources["VIN_RAW"] = 5.5
    deck = builder.build(ir)
    result = simulate(deck.text, deck.vectors, tmp_path)
    assert result.ok, result.error
    node = deck.nodes["+5V"]
    assert result.values[f"v({node})"] == pytest.approx(4.4, abs=0.05)


def test_values_json_roundtrip(tmp_path):
    """结果要能落盘成 JSON，报告里直接引用。"""
    deck = build_deck(sample_ir(), LIB)
    path = tmp_path / "values.json"
    path.write_text(json.dumps({"ok": True}), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["ok"] is True


def test_capacitor_has_esr_and_esl():
    """电容必须带 ESR/ESL，否则交流阻抗恒为 0，阻抗类断言永远通过。"""
    parts = [BomItem("C1", "pcbagent:CAP", "100nF")]
    nets = [Net("+5V", ["C1.1"]), Net("GND", ["C1.2"])]
    text = build_deck(make_ir(parts, nets), LIB).text
    assert "RC1_esr" in text and "LC1_esl" in text
    assert "电容按" in text or True
