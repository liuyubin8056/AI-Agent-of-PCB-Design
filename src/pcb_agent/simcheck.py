"""仿真断言：把实测值变成 ``Issue``，接进现有「校验 → 评审 → 返修」闭环。

和 ``validate.py`` 的分工：那里查的是**连接与结构**（引脚是否悬空、去耦够不够），
这里查的是**电气行为**（电压对不对、电流超没超）。两者互补，缺一不可——
把限流电阻从 1k 改成 100Ω，结构校验一路绿灯，只有仿真会喊。
"""

from __future__ import annotations

from .ir import ERROR, WARNING, DesignIR, Issue
from .library import Library
from .simulate import SimReport
from .spice import expected_rail, limits

DEFAULT_LIMITS = {
    "rail_tolerance": 0.05,
    "rail_dead_ratio": 0.5,
    "led_current_min": 0.0005,
    "led_current_max": 0.02,
    "regulator_max_w": 0.8,
}


def _limit(name: str) -> float:
    value = limits().get(name)
    return float(DEFAULT_LIMITS[name] if value is None else value)


def check(ir: DesignIR, report: SimReport, library: Library | None = None) -> list[Issue]:
    """跑全部断言。仿真本身失败只报一条，不再往下猜。"""
    if not report.ok:
        return [
            Issue(
                "SIM_FAILED", ERROR,
                f"仿真未完成：{report.error or '未知原因'}", "spice",
                "检查 deck 与器件模型定义",
            )
        ]
    lib = library or Library()
    issues: list[Issue] = []
    issues.extend(_rails(ir, report))
    issues.extend(_leds(ir, report, lib))
    issues.extend(_regulators(ir, report, lib))
    issues.extend(_coverage(report))
    return issues


def _rails(ir: DesignIR, report: SimReport) -> list[Issue]:
    tol = _limit("rail_tolerance")
    dead = _limit("rail_dead_ratio")
    issues: list[Issue] = []
    for net in ir.nets:
        if net.kind != "power":
            continue
        expect = expected_rail(net.name)
        if not expect:
            continue
        actual = report.voltage(net.name)
        if actual is None:
            continue
        if actual < expect * dead:
            issues.append(
                Issue(
                    "SIM_RAIL_DEAD", ERROR,
                    f"电源网络 {net.name} 只有 {actual:.3f} V，远低于期望 {expect:g} V",
                    net.name, "检查稳压器输出、输入源与负载是否被短路",
                )
            )
        elif abs(actual - expect) > tol * expect:
            issues.append(
                Issue(
                    "SIM_RAIL_OUT_OF_RANGE", ERROR,
                    f"电源网络 {net.name} 实测 {actual:.3f} V，"
                    f"超出 {expect:g} V ±{tol * 100:.0f}% 范围",
                    net.name, "检查稳压值与分压电阻；若是压差不足，需提高输入电压",
                )
            )
    return issues


def _leds(ir: DesignIR, report: SimReport, lib: Library) -> list[Issue]:
    low, high = _limit("led_current_min"), _limit("led_current_max")
    issues: list[Issue] = []
    for item in ir.bom:
        if lib.category(item.symbol) != "indicator":
            continue
        amps = report.current(item.ref)
        if amps is None:
            continue  # 未建模的情况由 _coverage 统一报
        if amps < low:
            issues.append(
                Issue(
                    "SIM_LED_OFF", WARNING,
                    f"{item.ref} 电流仅 {amps * 1000:.3f} mA，指示灯不会亮", item.ref,
                    "检查限流电阻是否过大、驱动脚是否被拉低、LED 极性是否反接",
                )
            )
        elif amps > high:
            issues.append(
                Issue(
                    "SIM_LED_OVERDRIVE", ERROR,
                    f"{item.ref} 电流 {amps * 1000:.1f} mA，超出 {high * 1000:.0f} mA 上限",
                    item.ref, f"把限流电阻改大，目标 {low * 1000:.0f}~{high * 1000:.0f} mA",
                )
            )
    return issues


def _regulators(ir: DesignIR, report: SimReport, lib: Library) -> list[Issue]:
    """线性稳压器的功耗 = (Vin - Vout) × Iin，SOT-223 不加散热片约 0.8W 上限。"""
    max_w = _limit("regulator_max_w")
    issues: list[Issue] = []
    for item in ir.bom:
        if "LDO" not in item.symbol.upper() and lib.category(item.symbol) != "power":
            continue
        try:
            pins = lib.pins(item.symbol)
        except KeyError:
            continue
        vout_pins = [p for p in pins if p.get("type") == "power_out"]
        vin_pins = [p for p in pins if p.get("type") == "power_in"]
        if not vout_pins or not vin_pins:
            continue

        def net_of(number: str) -> str | None:
            for net in ir.nets:
                if f"{item.ref}.{number}" in net.members:
                    return net.name
            return None

        vout_net = net_of(str(vout_pins[0]["number"]))
        # GND 也是 power_in，必须挑出真正的那条输入轨：取电压最高的那条，
        # 否则会拿 GND 当输入算出负功耗，静默漏报（第一版就是这样）。
        candidates = []
        for pin in vin_pins:
            net = net_of(str(pin["number"]))
            volts = report.voltage(net) if net else None
            if net and volts is not None:
                candidates.append((volts, net))
        if not vout_net or not candidates:
            continue
        v_in, vin_net = max(candidates)
        v_out = report.voltage(vout_net)
        i_in = report.source_current(vin_net)
        if v_in is None or v_out is None or i_in is None:
            continue
        watts = (v_in - v_out) * abs(i_in)
        if watts > max_w:
            issues.append(
                Issue(
                    "SIM_REGULATOR_OVERHEAT", WARNING,
                    f"{item.ref} 功耗 {watts:.2f} W（{v_in:.1f}V→{v_out:.1f}V，"
                    f"{abs(i_in) * 1000:.1f}mA），超过 {max_w:g} W",
                    item.ref, "降低输入电压或改用开关电源，也可换更大封装",
                )
            )
    return issues


def _coverage(report: SimReport) -> list[Issue]:
    if not report.unmodeled:
        return []
    return [
        Issue(
            "SIM_UNMODELED", WARNING,
            f"{len(report.unmodeled)} 个器件没有 SPICE 模型，未参与仿真："
            + "、".join(report.unmodeled),
            "spice", "结论只覆盖已建模器件；补模型后再看这一组数值",
        )
    ]
