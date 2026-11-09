"""仿真断言测试：先用构造数据锁定判据，再用真机仿真验证端到端。"""

from __future__ import annotations

import pytest

from pcb_agent import simcheck
from pcb_agent.ir import BoardSpec, BomItem, DesignIR, ERROR, WARNING, Net
from pcb_agent.library import Library
from pcb_agent.ngspice import find_library
from pcb_agent.simulate import SimReport, Simulator

LIB = Library()
HAS_DLL = bool(find_library())


def sample_ir() -> DesignIR:
    parts = [
        BomItem("R2", "pcbagent:RES", "1k"),
        BomItem("D1", "pcbagent:LED", "Green"),
        BomItem("U2", "pcbagent:LDO_AMS1117_5V", "AMS1117-5.0"),
    ]
    nets = [
        Net("VIN_RAW", ["U2.3"]),
        Net("+5V", ["U2.2", "R2.1"]),
        Net("LED_A", ["R2.2", "D1.2"]),
        Net("GND", ["D1.1", "U2.1"]),
    ]
    return DesignIR(spec=BoardSpec(title="断言测试板"), bom=parts, nets=nets)


def report(rail: float = 5.0, led: float = 3e-3, iin: float = -0.008, **kw) -> SimReport:
    values = {
        "v(vin_raw)": 12.0,
        "v(p5v)": rail,
        "v(led_a)": 2.0,
        "@r2[i]": led,
        "@d1[id]": led,
        "i(Vvin_raw)": iin,
    }
    values.update(kw.pop("values", {}))
    return SimReport(
        ok=kw.pop("ok", True),
        values=values,
        nodes={"VIN_RAW": "vin_raw", "+5V": "p5v", "LED_A": "led_a", "GND": "0"},
        currents={"R2": "@r2[i]", "D1": "@d1[id]"},
        sources={"VIN_RAW": "Vvin_raw"},
        unmodeled=kw.pop("unmodeled", []),
        error=kw.pop("error", ""),
        extras=kw.pop("extras", {}),
    )


def codes(issues) -> list[str]:
    return [i.code for i in issues]


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #
def test_healthy_design_has_no_issue():
    assert simcheck.check(sample_ir(), report(), LIB) == []


def test_rail_out_of_range():
    issues = simcheck.check(sample_ir(), report(rail=4.5), LIB)
    assert "SIM_RAIL_OUT_OF_RANGE" in codes(issues)
    assert all(i.severity == ERROR for i in issues if i.code.startswith("SIM_RAIL"))


def test_dead_rail_is_reported_separately():
    """电压塌到 0 和"偏了 5%"是两种故障，处置方式不一样。"""
    issues = simcheck.check(sample_ir(), report(rail=0.3), LIB)
    assert "SIM_RAIL_DEAD" in codes(issues)
    assert "SIM_RAIL_OUT_OF_RANGE" not in codes(issues)


def test_led_overdrive():
    issues = simcheck.check(sample_ir(), report(led=25e-3), LIB)
    assert "SIM_LED_OVERDRIVE" in codes(issues)
    assert any(i.severity == ERROR for i in issues)


def test_led_dim_is_warning_only():
    issues = simcheck.check(sample_ir(), report(led=0.1e-3), LIB)
    hit = [i for i in issues if i.code == "SIM_LED_OFF"]
    assert hit and hit[0].severity == WARNING


def test_regulator_overheat():
    # (12-5) * 0.15A = 1.05W > 0.8W
    issues = simcheck.check(sample_ir(), report(iin=-0.15), LIB)
    assert "SIM_REGULATOR_OVERHEAT" in codes(issues)


def test_failed_sim_reports_single_issue():
    issues = simcheck.check(sample_ir(), report(ok=False, error="未找到 ngspice 动态库"), LIB)
    assert codes(issues) == ["SIM_FAILED"]


def test_unmodeled_parts_are_disclosed():
    issues = simcheck.check(sample_ir(), report(unmodeled=["X9(acme:MYSTERY)"]), LIB)
    assert "SIM_UNMODELED" in codes(issues)


# --------------------------------------------------------------------------- #
# 真机：正常设计 0 错，改坏限流电阻立刻被抓
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_DLL, reason="未找到 ngspice.dll，跳过真机仿真")
def test_real_board_passes_then_catches_value_error(tmp_path):
    from pcb_agent.patch import PatchOp, apply
    from pcb_agent.subcircuits import build_board

    parts, nets, _ = build_board(
        [
            {"id": "mcu_min_system"},
            {"id": "power_ldo5v"},
            {"id": "led_indicator"},
        ]
    )
    ir = DesignIR(spec=BoardSpec(title="真机断言板"), bom=parts, nets=nets)
    sim = Simulator(library=LIB)

    good = sim.run(ir, tmp_path / "good")
    assert good.ok, good.error
    assert simcheck.check(ir, good, LIB) == []

    # 结构校验看不出问题：R2 仍是合法电阻，只是值不对
    bad_ir = apply(ir, [PatchOp("set_value", ref="R2", value="100")], LIB).ir
    bad = sim.run(bad_ir, tmp_path / "bad")
    assert bad.ok, bad.error
    issues = simcheck.check(bad_ir, bad, LIB)
    assert "SIM_LED_OVERDRIVE" in codes(issues)
    assert (bad.current("D1") or 0) > 0.02


# --------------------------------------------------------------------------- #
# 仿真结论要能被"修"，否则 E 组闭环断在半路
# --------------------------------------------------------------------------- #
def test_measurement_is_parsed_from_message():
    from pcb_agent.agents.critic import _measured_current

    assert _measured_current("D1 电流 22.5 mA，超出 20 mA 上限") == pytest.approx(0.0225)
    assert _measured_current("没有数字") is None


@pytest.mark.skipif(not HAS_DLL, reason="未找到 ngspice.dll，跳过真机仿真")
def test_autofix_targets_led_resistor(tmp_path):
    """按实测电流反推阻值，一步落到合规区间，不是拍脑袋乘 10。"""
    from pcb_agent.agents.critic import CriticAgent
    from pcb_agent.config import Settings
    from pcb_agent.ir import Issue
    from pcb_agent.llm import LLMClient
    from pcb_agent.spice import parse_value

    parts, nets, _ = build_ir()
    ir = DesignIR(spec=BoardSpec(title="修复测试板"), bom=parts, nets=nets)
    led = next(i for i in ir.bom if i.symbol.endswith(":LED"))
    issue = Issue("SIM_LED_OVERDRIVE", ERROR, f"{led.ref} 电流 60.0 mA，超出 20 mA 上限", led.ref)

    critic = CriticAgent(
        llm=LLMClient(Settings(offline=True, api_key="", model="")), lib=LIB
    )
    ops = critic._autofix(ir, [issue])
    assert ops and ops[0].op == "set_value"
    # 60mA 要降到 5mA，阻值正好放大 12 倍（I ∝ 1/R）
    before = parse_value(ir.part(ops[0].ref).value)
    assert parse_value(ops[0].value) == pytest.approx(before * 12, rel=0.02)


def build_ir():
    from pcb_agent.subcircuits import build_board

    return build_board(
        [{"id": "mcu_min_system"}, {"id": "power_ldo5v"}, {"id": "led_indicator"}]
    )


# --------------------------------------------------------------------------- #
# 三条专项断言（PDN 阻抗 / 总线上升 / 复位 RC）
# --------------------------------------------------------------------------- #
def test_decoupling_impedance_caught():
    """等效电容低于设计值 70% 时报警：说明电容没真正并在轨上。"""
    extras = {
        "pdn": {
            "applicable": True,
            "ok": True,
            "rails": {"p5v": 300e-9},
            "measures": {"z_p5v": 0.80},
        }
    }
    issues = simcheck.check(sample_ir(), report(extras=extras), LIB)
    assert "SIM_DECOUPLE_Z" in codes(issues)


def test_decoupling_ok_when_caps_connected():
    # 300nF 在 1MHz 对应 0.53Ω，实测 0.53Ω -> 等效电容与设计一致
    extras = {
        "pdn": {"applicable": True, "ok": True, "rails": {"p5v": 300e-9},
                "measures": {"z_p5v": 0.5305}}
    }
    assert "SIM_DECOUPLE_Z" not in codes(simcheck.check(sample_ir(), report(extras=extras), LIB))


def test_bus_rise_caught():
    extras = {"bus": {"applicable": True, "ok": True, "measures": {"tr_sda": 2.1e-6}}}
    issues = simcheck.check(sample_ir(), report(extras=extras), LIB)
    assert "SIM_I2C_RISE" in codes(issues)


def test_bus_rise_ok():
    extras = {"bus": {"applicable": True, "ok": True, "measures": {"tr_sda": 1e-7}}}
    assert "SIM_I2C_RISE" not in codes(simcheck.check(sample_ir(), report(extras=extras), LIB))


@pytest.mark.parametrize("tau,expected", [(1e-4, True), (1e-3, False), (1e-2, True)])
def test_reset_tau_band(tau, expected):
    extras = {"reset": {"applicable": True, "ok": True, "measures": {"tau": tau}}}
    hit = "SIM_RESET_TAU" in codes(simcheck.check(sample_ir(), report(extras=extras), LIB))
    assert hit is expected


def test_reset_not_applicable_is_silent():
    """没有复位电容时不能报问题，也不能假装通过。"""
    extras = {"reset": {"applicable": False, "ok": False, "reason": "无 R+C"}}
    assert "SIM_RESET_TAU" not in codes(simcheck.check(sample_ir(), report(extras=extras), LIB))


@pytest.mark.skipif(not HAS_DLL, reason="未找到 ngspice.dll，跳过真机仿真")
def test_pdn_bus_reset_end_to_end(tmp_path):
    from pcb_agent.spice import SpiceBuilder
    from pcb_agent.subcircuits import build_board

    parts, nets, _ = build_board(
        [{"id": "mcu_min_system"}, {"id": "power_ldo5v"}, {"id": "i2c_sensor"}]
    )
    ir = DesignIR(spec=BoardSpec(title="专项分析板"), bom=parts, nets=nets)
    sim = Simulator(library=LIB)
    rep = sim.run(ir, tmp_path)

    assert rep.ok, rep.error
    pdn = rep.extras["pdn"]
    assert pdn["ok"] and pdn["measures"], pdn
    bus = rep.extras["bus"]
    assert bus["ok"] and all(v < 3e-7 for v in bus["measures"].values()), bus
    # 该子电路没有复位电容，断言应标记为不适用
    assert rep.extras["reset"]["applicable"] is False
    assert simcheck.check(ir, rep, LIB) == []
