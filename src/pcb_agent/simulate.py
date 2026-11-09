"""仿真入口：IR → deck → ngspice → 结构化报告。

和 ``kicad_gate`` 的定位一样，这一层只负责"跑"和"取数"，
**判断对错在 ``simcheck``**。分开的好处是：仿真失败和电气不达标是两类问题，
日志里一眼能分清是环境问题还是设计问题。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .ir import DesignIR
from .library import Library
from .ngspice import find_library, simulate
from .spice import SimDeck, SpiceBuilder, build_deck


@dataclass
class SimReport:
    ok: bool = False
    version: str = ""
    elapsed: float = 0.0
    error: str = ""
    values: dict[str, float] = field(default_factory=dict)
    nodes: dict[str, str] = field(default_factory=dict)
    currents: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unmodeled: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)  # pdn / bus / reset 三条专项分析
    deck: str = ""

    # -- 取值 ------------------------------------------------------------ #
    def voltage(self, net: str) -> float | None:
        node = self.nodes.get(net)
        if node is None:
            return None
        return self.values.get(f"v({node})")

    def current(self, ref: str) -> float | None:
        vec = self.currents.get(ref)
        return self.values.get(vec) if vec else None

    def source_current(self, net: str) -> float | None:
        elem = self.sources.get(net)
        return self.values.get(f"i({elem})") if elem else None

    # -- 落盘 ------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "version": self.version,
            "elapsed_s": round(self.elapsed, 2),
            "error": self.error,
            "values": self.values,
            "nodes": self.nodes,
            "currents": self.currents,
            "sources": self.sources,
            "notes": self.notes,
            "unmodeled": self.unmodeled,
            "extras": self.extras,
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target


class Simulator:
    """跑一次工作点仿真。没有 ngspice 时 ``available`` 为 False，调用方跳过即可。"""

    def __init__(self, settings: Settings | None = None, library: Library | None = None):
        self.settings = settings or Settings.load()
        self.lib = library or Library()
        self.dll = find_library(self.settings)
        self.builder = SpiceBuilder(self.lib) if self.dll else None

    @property
    def available(self) -> bool:
        return bool(self.dll and self.builder)

    def run(
        self,
        ir: DesignIR,
        out_dir: str | Path | None = None,
        timeout: int = 90,
        deep: bool = True,
    ) -> SimReport:
        if not self.available:
            return SimReport(ok=False, error="未找到 ngspice 动态库（装 KiCad 即可）")

        deck: SimDeck = self.builder.build(ir) if self.builder else build_deck(ir, self.lib)
        report = SimReport(
            nodes=dict(deck.nodes),
            currents=dict(deck.currents),
            sources=dict(deck.sources),
            notes=list(deck.notes),
            unmodeled=list(deck.unmodeled),
            deck=deck.text,
        )

        target = Path(out_dir) if out_dir else Path("runs") / "sim"
        target.mkdir(parents=True, exist_ok=True)
        deck.write(target / "deck.cir")

        start = time.perf_counter()
        raw = simulate(deck.text, deck.vectors, target, self.settings, timeout=timeout)
        report.elapsed = round(time.perf_counter() - start, 2)
        report.values = raw.values
        report.version = raw.version
        report.ok = raw.ok
        report.error = raw.error
        if raw.log:
            report.notes.append("ngspice 日志尾部：" + " | ".join(raw.log[-2:]))
        if report.ok and deep:
            report.extras = self._deep(ir, target)
        if report.ok:
            report.write(target / "sim.json")
        return report

    def _deep(self, ir: DesignIR, target: Path) -> dict:
        """三条专项分析：去耦阻抗、总线上升时间、复位 RC。

        任何一条都不允许拖垮主流程：失败只记录 ok=False 与原因，断言侧会跳过。
        """
        extras: dict = {}
        plans = (
            ("pdn", "build_pdn"),
            ("bus", "build_bus"),
            ("reset", "build_reset"),
        )
        for name, method in plans:
            maker = getattr(self.builder, method, None)
            if maker is None:
                continue
            deck = maker(ir)
            if deck is None:
                extras[name] = {
                    "applicable": False,
                    "ok": False,
                    "reason": self._not_applicable(name),
                }
                continue
            res = simulate(deck.text, [], target / name, self.settings)
            extras[name] = {
                "applicable": True,
                "ok": res.ok,
                "measures": res.measures,
                "rails": deck.rails,
                "notes": deck.notes,
                "error": res.error,
            }
            if not res.ok:
                extras[name]["log"] = res.log[-3:]
        return extras

    @staticmethod
    def _not_applicable(name: str) -> str:
        return {
            "pdn": "没有可分析的电源网络",
            "bus": "没有 I2C 总线或找不到上拉电阻",
            "reset": "复位网络没有 R+C 组合（本板只有上拉，没有复位电容）",
        }.get(name, "不适用")


def summarize(report: SimReport, ir: DesignIR | None = None) -> str:
    """人可读摘要：每条网络电压 + 每个支路电流。"""
    lines = [f"ngspice {report.version or '?'} / {report.elapsed:.1f}s"]
    if not report.ok:
        lines.append(f"仿真失败：{report.error}")
        return "\n".join(lines)

    values = {net: report.voltage(net) for net in report.nodes}
    rows = [("网络", "电压(V)")] + [
        (net, f"{volts:.4f}") for net, volts in values.items() if volts is not None
    ]
    width = max(len(r[0]) for r in rows)
    lines.append("")
    for name, val in rows:
        lines.append(f"  {name.ljust(width)}  {val}")

    rows = [("器件", "电流(mA)")]
    for ref in report.currents:
        amps = report.current(ref)
        if amps is not None:
            rows.append((ref, "0.000" if abs(amps) < 5e-7 else f"{amps * 1000:.3f}"))
    lines.append("")
    width = max(len(r[0]) for r in rows)
    for name, val in rows:
        lines.append(f"  {name.ljust(width)}  {val}")

    extras = report.extras or {}
    if extras:
        lines.append("")
        lines.append("专项分析")
        node_to_net = {v: k for k, v in report.nodes.items()}
        pdn = extras.get("pdn") or {}
        for key, ohms in (pdn.get("measures") or {}).items():
            if not key.startswith("z_"):
                continue
            node = key[2:]
            design = float((pdn.get("rails") or {}).get(node, 0) or 0)
            cap = 1 / (6.283185307 * 1e6 * ohms) if ohms > 0 else 0
            lines.append(
                f"  去耦阻抗 {node_to_net.get(node, node)}：{ohms * 1000:.0f} mΩ @1MHz，"
                f"等效 {cap * 1e9:.0f} nF / 设计 {design * 1e9:.0f} nF"
            )
        if pdn and not pdn.get("ok"):
            lines.append(f"  去耦阻抗：未完成（{pdn.get('error') or pdn.get('reason')}）")

        bus = extras.get("bus") or {}
        if bus.get("measures"):
            rises = "、".join(
                f"{node_to_net.get(k[3:], k[3:])} {v * 1e9:.0f} ns"
                for k, v in bus["measures"].items() if k.startswith("tr_")
            )
            lines.append(f"  总线上升时间：{rises}")
        elif bus:
            lines.append(f"  总线上升时间：不适用（{bus.get('reason') or bus.get('error')}）")

        reset = extras.get("reset") or {}
        tau = (reset.get("measures") or {}).get("tau")
        if tau:
            lines.append(f"  复位时间常数：{tau * 1000:.2f} ms")
        elif reset:
            lines.append(f"  复位时间常数：不适用（{reset.get('reason') or reset.get('error')}）")

    if report.unmodeled:
        lines.append("")
        lines.append("未建模：" + "、".join(report.unmodeled))
    return "\n".join(lines)
