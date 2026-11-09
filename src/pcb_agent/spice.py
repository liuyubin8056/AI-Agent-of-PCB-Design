"""IR -> SPICE netlist（deck）生成器。

设计原则和 ``render.py`` 一致：**LLM 不参与，全部确定性生成**。
器件怎么落到 SPICE、引脚怎么连到节点，都由 ``data/spice_models.json`` 决定，
所以同一份 IR 生成的 deck 逐字可复现，仿真结果能进报告当实验数据。

三个必须显式处理的边界
----------------------
1. **未建模器件不许静默当理想导线**。连接器、PWR_FLAG 记为 SKIP；库里没有的
   符号记为 unmodeled 并写进结果。否则「仿真通过」是假的。
2. **每个信号网加 1G 泄放电阻**。只有电容到地的节点在 ``.op`` 里没有直流路径，
   ngspice 会报奇异矩阵或给出漂移值。
3. **MCU 不是模拟器件**。用「电源脚对地等效负载 + 信号脚 5V 驱动」近似，
   并在结果里注明，避免把近似当精确。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_DIR
from .ir import ERROR, GROUND_NAMES, BomItem, DesignIR, Issue, Net
from .library import Library

MODELS_PATH = DATA_DIR / "spice_models.json"

_MULTIPLIER = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "": 1.0,
}
_VALUE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([pnuµmkKMGT]?)\s*([A-Za-zΩ]*)\s*$")
_NAME_RE = re.compile(r"[^a-z0-9_]")


def parse_value(text: str) -> float | None:
    """把 ``10k`` / ``100nF`` / ``4.7uH`` 解析成 SI 数值；解析不了返回 None。

    注意 ``M`` 与 ``m`` 必须区分大小写：1M=1e6，1m=1e-3，这是 SPICE 里最经典的坑。
    """
    if not text:
        return None
    match = _VALUE_RE.match(text)
    if not match:
        return None
    number, mult, _unit = match.groups()
    try:
        return float(number) * _MULTIPLIER[mult]
    except (KeyError, ValueError):
        return None


def format_value(value: float, unit: str = "") -> str:
    """把 4700 写成 ``4.7k``，补丁回灌给人看时才像话。"""
    for scale, suffix in (
        (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"),
        (1e-6, "u"), (1e-9, "n"), (1e-12, "p"),
    ):
        if abs(value) >= scale:
            return f"{value / scale:g}{suffix}{unit}"
    return f"{value:g}{unit}"


def is_ground(name: str) -> bool:
    upper = name.strip().upper()
    return upper in GROUND_NAMES or upper.startswith("GND")


def node_name(name: str) -> str:
    """网名 -> SPICE 节点名。``+5V`` 这种带符号的名字必须转义。"""
    if is_ground(name):
        return "0"
    text = name.strip().lower().replace("+", "p").replace("-", "m")
    text = _NAME_RE.sub("_", text).strip("_")
    if not text:
        text = "net"
    if text[0].isdigit():
        text = "n" + text
    return text


def _elem(ref: str, letter: str) -> str:
    """元件名：R1/C1/D1 直接用，Y1/U2/SW1 加类型字母（SPICE 认首字母）。"""
    upper = ref.upper()
    return ref if upper[:1] == letter.upper() else f"{letter}{ref}"


@dataclass
class SimDeck:
    """一份可执行的 SPICE deck 及其元数据。"""

    text: str
    nodes: dict[str, str] = field(default_factory=dict)  # 网名 -> 节点
    vectors: list[str] = field(default_factory=list)
    currents: dict[str, str] = field(default_factory=dict)  # 位号 -> 电流向量
    sources: dict[str, str] = field(default_factory=dict)  # 电源网名 -> 理想源元件名
    rails: dict[str, float] = field(default_factory=dict)  # 节点 -> 该轨设计电容总和(F)
    unmodeled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text, encoding="utf-8")
        return target

    def summary(self) -> str:
        lines = [f"deck：{len(self.vectors)} 个观测量"]
        if self.unmodeled:
            lines.append("未建模：" + "、".join(self.unmodeled))
        return "；".join(lines)


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #
class SpiceBuilder:
    def __init__(self, library: Library | None = None, models_path: str | Path | None = None):
        self.lib = library or Library()
        path = Path(models_path) if models_path else MODELS_PATH
        self.models = json.loads(path.read_text(encoding="utf-8"))
        self.defaults = self.models.get("defaults", {})
        self.map = self.models.get("models", {})
        self.subckts = self.models.get("subckts", {})
        self.device_models = self.models.get("device_models", {})
        self.sources = self.models.get("sources", {})

    # -- 小工具 ---------------------------------------------------------- #
    def _pin_type(self, symbol: str, number: str) -> str:
        try:
            pin = self.lib.pin(symbol, number)
        except KeyError:
            return "unknown"
        return pin.get("type", "unknown") if pin else "unknown"

    def _has_out_driver(self, ir: DesignIR, net: Net) -> bool:
        """网络上是否已有 power_out（稳压器输出）驱动，有就不该再加理想源。"""
        for member in net.members:
            ref, _, pin_no = member.partition(".")
            item = ir.part(ref)
            if not item or self.lib.category(item.symbol) == "virtual":
                continue
            if self._pin_type(item.symbol, pin_no) == "power_out":
                return True
        return False

    def _source_value(self, net: Net) -> tuple[float, str]:
        if net.name in self.sources:
            return float(self.sources[net.name]), ""
        found = re.search(r"(\d+(?:\.\d+)?)\s*V", net.name, re.IGNORECASE)
        if found:
            return float(found.group(1)), ""
        return (
            float(self.defaults.get("drive_voltage", 5.0)),
            f"电源网络 {net.name} 未配置电压，按 5V 仿真",
        )

    # -- 主体 ------------------------------------------------------------ #
    def build(self, ir: DesignIR, analyses: tuple[str, ...] = ("op",)) -> SimDeck:
        defaults = self.defaults
        nodes = self._nodes(ir)
        net_of_pin = {m: nodes[n.name] for n in ir.nets for m in n.members}

        deck = SimDeck(text="", nodes=nodes)
        elements: list[str] = []
        currents: dict[str, str] = {}

        for item in ir.bom:
            elements.extend(self._device(item, net_of_pin, deck, currents))

        elements.extend(self._sources(ir, deck))
        elements.extend(self._drives(ir, net_of_pin, nodes))
        elements.extend(self._leaks(ir, nodes))

        vectors = [f"v({n})" for n in nodes.values() if n != "0"]
        vectors += list(currents.values())
        vectors += [f"i({elem})" for elem in deck.sources.values()]
        deck.vectors = vectors
        deck.currents = currents

        head = [
            f"* pcb-agent 生成的 SPICE deck：{ir.spec.title or '未命名板卡'}",
            f"* 器件 {len(ir.bom)} 个 / 网络 {len(ir.nets)} 条 / 未建模 {len(deck.unmodeled)} 个",
            f".options TEMP={defaults.get('ambient_c', 27)}",
        ]
        body = []
        for name, spec in self.subckts.items():
            ports = " ".join(spec.get("ports") or [])
            lines = spec.get("lines") or []
            # 端口列表必须显式写出：漏了它 ngspice 会把实例连线当成多余参数
            body.append(f".subckt {name} {ports}\n    " + "\n    ".join(lines) + "\n.ends")
        body += [f".model {name} {card}" for name, card in self.device_models.items()]
        body += elements
        body += [f".{kind}" for kind in analyses]
        body += [".end"]
        deck.text = "\n".join(head + [""] + body) + "\n"
        return deck

    # -- 节点映射 -------------------------------------------------------- #
    def _nodes(self, ir: DesignIR) -> dict[str, str]:
        nodes: dict[str, str] = {}
        used: set[str] = set()
        for net in ir.nets:
            node = node_name(net.name)
            if node != "0":
                base, k = node, 2
                while node in used:
                    node = f"{base}_{k}"
                    k += 1
                used.add(node)
            nodes[net.name] = node
        return nodes

    def _pin_nodes(self, ir: DesignIR) -> tuple[dict[str, str], dict[str, str]]:
        nodes = self._nodes(ir)
        return nodes, {m: nodes[n.name] for n in ir.nets for m in n.members}

    def _other_net(self, ir: DesignIR, ref: str, member: str, exclude: str) -> Net | None:
        for net in ir.nets:
            if net.name == exclude:
                continue
            if any(m.startswith(f"{ref}.") for m in net.members) and member not in net.members:
                return net
            if any(m.startswith(f"{ref}.") for m in net.members) and net.name != exclude:
                return net
        return None

    # -- PDN 阻抗（去耦电容的交流阻抗） ---------------------------------- #
    def build_pdn(self, ir: DesignIR) -> SimDeck:
        """只保留无源 PDN（电容 + ESR/ESL + 负载电阻），注入 1A 交流电流测阻抗。

        稳压器在这里被当作**开路**：理想受控源在 .ac 里会把输出阻抗压成 0，
        测出来永远合格，那是一种假断言。这里测的是设计者能改的那部分——
        电容组本身的阻抗，等价于"去耦电容到底在不在、容值对不对"。
        """
        nodes, pin_node = self._pin_nodes(ir)
        rail_nodes = {nodes[n.name] for n in ir.nets if n.kind == "power"}
        probe = float(self.defaults.get("pdn_probe_hz", 1e6))
        deck = SimDeck(text="", nodes=nodes)
        elements: list[str] = []
        chars: dict[str, float] = {}

        for item in ir.bom:
            spec = self.map.get(item.symbol) or {}
            kind = str(spec.get("kind", "")).upper()
            ports = list((spec.get("pins") or {}).values())
            if kind == "C" and len(ports) >= 2:
                a = pin_node.get(f"{item.ref}.{_pin_of(spec, ports[0])}")
                b = pin_node.get(f"{item.ref}.{_pin_of(spec, ports[1])}")
                farads = parse_value(item.value) or parse_value(
                    self.lib.default_value(item.symbol)
                )
                if not a or not b or not farads:
                    continue
                elements += self._capacitor(_elem(item.ref, "C"), a, b, farads, deck)
                for node in (a, b):
                    if node in rail_nodes:
                        chars[node] = chars.get(node, 0.0) + farads
            elif kind == "R" and len(ports) >= 2:
                a = pin_node.get(f"{item.ref}.{_pin_of(spec, ports[0])}")
                b = pin_node.get(f"{item.ref}.{_pin_of(spec, ports[1])}")
                ohms = parse_value(item.value) or parse_value(self.lib.default_value(item.symbol))
                if a and b and ohms:
                    elements.append(f"{_elem(item.ref, 'R')} {a} {b} {ohms:g}")
            elif kind == "LOAD":
                load = float(spec.get("power_load_ohm", 1e4))
                for pin in self.lib.pins(item.symbol):
                    if pin.get("type") not in {"power_in", "power_out"}:
                        continue
                    node = pin_node.get(f"{item.ref}.{pin['number']}")
                    if node and node != "0":
                        elements.append(f"R{item.ref}_{pin['number']} {node} 0 {load:g}")

        # 每个节点都要有直流路径：只挂一个电阻的孤立节点会让 .ac 报奇异矩阵
        for node in sorted({v for v in nodes.values() if v != "0"}):
            elements.append(f"Rlk_{node} {node} 0 1g")

        meas: list[str] = []
        for node in sorted(rail_nodes):
            elements.append(f"Iac_{node} {node} 0 ac 1")
            meas.append(f".meas ac z_{node} FIND vm({node}) AT={probe:g}")

        deck.rails = chars
        deck.text = "\n".join(
            ["* PDN 阻抗：1A 交流注入，稳压器视作开路", *elements,
             f".ac dec 20 10 {probe * 20:g}", *meas, ".end"]
        ) + "\n"
        return deck

    # -- I2C 总线上升时间 ------------------------------------------------ #
    def build_bus(self, ir: DesignIR) -> SimDeck | None:
        """按设计里的上拉电阻与总线电容做一次充放电，测 30%→70% 上升时间。"""
        nodes, pin_node = self._pin_nodes(ir)
        targets = [
            n for n in ir.nets
            if n.kind == "signal" and any(k in n.name.upper() for k in ("SDA", "SCL"))
        ]
        if not targets:
            return None

        trace = float(self.defaults.get("trace_cap_pf", 10.0))
        deck = SimDeck(text="", nodes=nodes)
        elements = ["Vdd vdd 0 dc 5"]
        ics: list[str] = []
        meas: list[str] = []
        taus: list[float] = []

        for net in targets:
            node = nodes[net.name]
            rp = 0.0
            for member in net.members:
                item = ir.part(member.partition(".")[0])
                if not item or not item.symbol.endswith(":RES"):
                    continue
                other = self._other_net(ir, item.ref, member, net.name)
                if other is not None and other.kind == "power":
                    rp = parse_value(item.value) or 0.0
                    break
            if rp <= 0:
                deck.notes.append(f"{net.name} 找不到接电源的上拉电阻，跳过上升时间")
                continue

            cbus_pf = trace
            for member in net.members:
                item = ir.part(member.partition(".")[0])
                spec = self.map.get(item.symbol) if item else None
                if spec and str(spec.get("kind", "")).upper() == "LOAD":
                    cbus_pf += float(spec.get("pin_cap_pf", 0) or 0)

            cb = cbus_pf * 1e-12
            tau = rp * cb
            taus.append(tau)
            elements += [
                f"Rpu_{node} vdd {node} {rp:g}",
                f"Cb_{node} {node} 0 {cb:.6g}",
            ]
            ics.append(f".ic V({node})=0")
            meas.append(
                f".meas tran tr_{node} TRIG v({node}) VAL=1.5 RISE=1 "
                f"TARG v({node}) VAL=3.5 RISE=1"
            )
            deck.rails[node] = cb

        if not meas:
            return None
        # 一个 deck 只能有一条 .tran：多条会让后一条覆盖前一条的图，
        # 先测的那条就会"测量失败"。取最长窗口 + 最细步长，两条总线共用。
        step = min(max(tau / 100, 1e-9) for tau in taus)
        stop = max(max(taus) * 12, 2e-7)
        deck.text = "\n".join(
            ["* I2C 总线上升时间", *elements, *ics,
             f".tran {step:g} {stop:g} uic", *meas, ".end"]
        ) + "\n"
        return deck

    # -- 复位 RC --------------------------------------------------------- #
    def build_reset(self, ir: DesignIR) -> SimDeck | None:
        """复位网络若有 R 上拉 + C 对地，测充电到 63.2% 的时间常数。"""
        net = next((n for n in ir.nets if "RESET" in n.name.upper()), None)
        if net is None:
            return None
        nodes, pin_node = self._pin_nodes(ir)
        node = nodes[net.name]

        r_val = c_val = 0.0
        for member in net.members:
            item = ir.part(member.partition(".")[0])
            if not item:
                continue
            value = parse_value(item.value) or 0.0
            if item.symbol.endswith(":RES"):
                other = self._other_net(ir, item.ref, member, net.name)
                if other is not None and other.kind == "power":
                    r_val = value
            elif item.symbol.endswith(":CAP"):
                other = self._other_net(ir, item.ref, member, net.name)
                if other is not None and other.kind == "ground":
                    c_val = value

        if r_val <= 0 or c_val <= 0:
            return None

        deck = SimDeck(text="", nodes=nodes)
        deck.rails[node] = c_val
        deck.text = "\n".join([
            "* 复位 RC 时间常数",
            "Vdd vdd 0 dc 5",
            f"Rpu_{node} vdd {node} {r_val:g}",
            f"C_{node} {node} 0 {c_val:.6g}",
            f".ic V({node})=0",
            f".tran {max(r_val * c_val / 100, 1e-7):g} {r_val * c_val * 10:g} uic",
            f".meas tran tau WHEN v({node})=3.16 RISE=1",
            ".end",
        ]) + "\n"
        return deck

    # -- 器件 ------------------------------------------------------------ #
    def _device(
        self, item: BomItem, net_of_pin: dict[str, str], deck: SimDeck, currents: dict[str, str]
    ) -> list[str]:
        spec = self.map.get(item.symbol)
        if not spec:
            deck.unmodeled.append(f"{item.ref}({item.symbol})")
            deck.notes.append(f"{item.ref} 没有 SPICE 模型，未参与仿真")
            return []
        kind = str(spec.get("kind", "SKIP")).upper()

        if kind == "SKIP":
            return []

        if kind in {"R", "C", "D"}:
            return self._two_terminal(item, spec, kind, net_of_pin, deck, currents)

        if kind == "SW":
            a = net_of_pin.get(f"{item.ref}.{_pin_of(spec, 'a')}")
            b = net_of_pin.get(f"{item.ref}.{_pin_of(spec, 'b')}")
            if not a or not b:
                deck.unmodeled.append(f"{item.ref}(缺连接)")
                return []
            # 默认断开：按键按下是瞬态事件，.op 里按静态处理更合理
            ohms = float(self.defaults.get("switch_open_ohm", 1e12))
            deck.notes.append(f"{item.ref} 按断开处理（R={ohms:g}Ω）")
            return [f"{_elem(item.ref, 'R')} {a} {b} {ohms:g}"]

        if kind == "X":
            ports = spec.get("ports") or []
            conn = []
            for port in ports:
                pin_no = _pin_of(spec, port)
                node = net_of_pin.get(f"{item.ref}.{pin_no}")
                if not node:
                    deck.unmodeled.append(f"{item.ref}(端口 {port} 未连接)")
                    return []
                conn.append(node)
            if not conn:
                deck.unmodeled.append(f"{item.ref}(无端口)")
                return []
            return [f"{_elem(item.ref, 'X')} {' '.join(conn)} {spec.get('subckt', 'UNKNOWN')}"]

        if kind == "LOAD":
            return self._load(item, spec, net_of_pin, deck)

        deck.unmodeled.append(f"{item.ref}(未知类型 {kind})")
        return []

    def _two_terminal(
        self,
        item: BomItem,
        spec: dict,
        kind: str,
        net_of_pin: dict[str, str],
        deck: SimDeck,
        currents: dict[str, str],
    ) -> list[str]:
        # 两端元件的端口名不一定叫 a/b：LED 是 a/k，所以按声明顺序取，
        # 别硬编码 'a'/'b'——第一版就是这样把 LED 判成"引脚未连接"的。
        ports = list((spec.get("pins") or {}).values())
        if len(ports) < 2:
            deck.unmodeled.append(f"{item.ref}(端口未定义)")
            return []
        a = net_of_pin.get(f"{item.ref}.{_pin_of(spec, ports[0])}")
        b = net_of_pin.get(f"{item.ref}.{_pin_of(spec, ports[1])}")
        if not a or not b:
            deck.unmodeled.append(f"{item.ref}(引脚未连接)")
            return []

        elem = _elem(item.ref, kind)
        if kind == "D":
            model = spec.get("model", "DLED")
            currents[item.ref] = f"@{elem.lower()}[id]"
            return [f"{elem} {a} {b} {model}"]

        value = parse_value(item.value)
        if value is None:
            fallback = self.lib.default_value(item.symbol)
            value = parse_value(fallback)
            if value is None:
                deck.unmodeled.append(f"{item.ref}(值 {item.value!r} 无法解析)")
                return []
            deck.notes.append(f"{item.ref} 的值 {item.value!r} 无法解析，用库默认 {fallback}")
        if kind == "C":
            # .op 下电容电流恒为 0，列进电流表只会误导
            return self._capacitor(elem, a, b, value, deck)
        currents[item.ref] = f"@{elem.lower()}[i]"
        return [f"{elem} {a} {b} {value:.6g}"]

    def _capacitor(self, elem: str, a: str, b: str, farads: float, deck: SimDeck) -> list[str]:
        """电容串 ESR + ESL：没有这两个参数，交流阻抗恒为 0，任何阈值都永远通过。

        参数取 0603 陶瓷电容的典型值（ESR 12mΩ / ESL 0.6nH），写在 defaults 里可改。
        串接后直流工作点不变（电容隔直），只影响 .ac / .tran。
        """
        esr = float(self.defaults.get("cap_esr_ohm", 0) or 0)
        esl = float(self.defaults.get("cap_esl_nh", 0) or 0)
        if esr <= 0 and esl <= 0:
            return [f"{elem} {a} {b} {farads:.6g}"]

        nodes = [a]
        parts = []
        if esl > 0:
            nodes.append(f"m{elem}_l")
        nodes.append(f"m{elem}_r")
        nodes.append(b)
        parts.append(f"{elem} {nodes[0]} {nodes[1]} {farads:.6g}")
        if esl > 0:
            parts.append(f"L{elem}_esl {nodes[1]} {nodes[2]} {esl * 1e-9:.6g}")
        if esr > 0:
            parts.append(f"R{elem}_esr {nodes[-2]} {nodes[-1]} {esr:g}")
        if "电容按" not in " ".join(deck.notes):
            deck.notes.append(
                f"电容按理想容值 + {esr * 1000:.0f}mΩ ESR + {esl:.1f}nH ESL 建模"
            )
        return parts

    def _load(
        self, item: BomItem, spec: dict, net_of_pin: dict[str, str], deck: SimDeck
    ) -> list[str]:
        """IC / 模组的等效负载：电源脚对地电阻 + 信号脚对地电容。"""
        out: list[str] = []
        load = float(spec.get("power_load_ohm", 1e4))
        cap_pf = float(spec.get("pin_cap_pf", 0) or 0)
        power_pins = 0
        for pin in self.lib.pins(item.symbol):
            number = str(pin.get("number"))
            node = net_of_pin.get(f"{item.ref}.{number}")
            if not node or node == "0":
                continue
            if pin.get("type") in {"power_in", "power_out"}:
                out.append(f"R{item.ref}_{number} {node} 0 {load:g}")
                power_pins += 1
            elif cap_pf:
                out.append(f"C{item.ref}_{number} {node} 0 {cap_pf * 1e-12:.6g}")
        if power_pins:
            deck.notes.append(
                f"{item.ref} 按等效负载建模：每个电源脚 {load:g}Ω 对地（约 {5 / load * 1000:.1f}mA）"
            )
        return out

    # -- 激励 ------------------------------------------------------------ #
    def _sources(self, ir: DesignIR, deck: SimDeck) -> list[str]:
        out: list[str] = []
        for net in ir.nets:
            if net.kind != "power":
                continue
            if self._has_out_driver(ir, net):
                continue
            value, note = self._source_value(net)
            if note:
                deck.notes.append(note)
            node = deck.nodes[net.name]
            elem = f"V{node}"
            deck.sources[net.name] = elem
            out.append(f"{elem} {node} 0 dc {value:g}")
        return out

    def _drives(self, ir: DesignIR, net_of_pin: dict[str, str], nodes: dict[str, str]) -> list[str]:
        """MCU 的 GPIO 在 ``.op`` 里不会自己动，显式拉高当最坏情况。"""
        out: list[str] = []
        volts = float(self.defaults.get("drive_voltage", 5.0))
        series = float(self.defaults.get("drive_series_ohm", 22))
        for net in ir.nets:
            if net.kind != "signal" or is_ground(net.name):
                continue
            # 晶振网络由振荡器驱动，不是 GPIO 的直流电平；拉高会把
            # XTAL1/XTAL2 谎报成 5V，掩盖真实的起振条件。
            if self._has_clock(ir, net):
                continue
            driven = False
            for member in net.members:
                ref, _, pin_no = member.partition(".")
                item = ir.part(ref)
                if not item or self.lib.category(item.symbol) != "mcu":
                    continue
                if self._pin_type(item.symbol, pin_no) in {"bidirectional", "output"}:
                    driven = True
                    break
            if not driven:
                continue
            node = nodes[net.name]
            out.append(f"Vdrv_{node} {node}_d 0 dc {volts:g}")
            out.append(f"Rdrv_{node} {node}_d {node} {series:g}")
        return out

    def _has_clock(self, ir: DesignIR, net: Net) -> bool:
        for member in net.members:
            item = ir.part(member.partition(".")[0])
            if item and self.lib.category(item.symbol) == "clock":
                return True
        return False

    def _leaks(self, ir: DesignIR, nodes: dict[str, str]) -> list[str]:
        ohms = float(self.defaults.get("leak_ohm", 1e9))
        out: list[str] = []
        for net in ir.nets:
            if net.kind != "signal" or is_ground(net.name):
                continue
            out.append(f"Rlk_{nodes[net.name]} {nodes[net.name]} 0 {ohms:g}")
        return out


def rail_expectations() -> dict[str, float]:
    """电源网名 -> 期望电压。数据来自 spice_models.json 的 sources。"""
    try:
        data = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): float(v) for k, v in (data.get("sources") or {}).items()}


def expected_rail(name: str, table: dict[str, float] | None = None) -> float | None:
    """先查表，再按网名里的数字推断（如 +12V -> 12）。"""
    table = rail_expectations() if table is None else table
    if name in table:
        return table[name]
    upper = name.upper()
    if upper in GROUND_NAMES or upper.startswith("GND"):
        return 0.0
    found = re.search(r"(\d+(?:\.\d+)?)\s*V", name, re.IGNORECASE)
    return float(found.group(1)) if found else None


def pdn_probe_hz() -> float:
    """PDN 阻抗的探测频点（默认 1 MHz），与生成 deck 时用的值保持一致。"""
    try:
        data = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
        return float((data.get("defaults") or {}).get("pdn_probe_hz", 1e6))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 1e6


def limits() -> dict[str, float]:
    try:
        data = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): float(v) for k, v in (data.get("limits") or {}).items()}


def _pin_of(spec: dict, port: str) -> str:
    """反查：端口名 -> 引脚号。"""
    for pin, name in (spec.get("pins") or {}).items():
        if name == port:
            return str(pin)
    return ""


def build_deck(
    ir: DesignIR, library: Library | None = None, analyses: tuple[str, ...] = ("op",)
) -> SimDeck:
    return SpiceBuilder(library).build(ir, analyses)


# --------------------------------------------------------------------------- #
# 结果对照
# --------------------------------------------------------------------------- #
def summarize(ir: DesignIR, deck: SimDeck, values: dict[str, float]) -> str:
    """把原始向量整理成人看的表。"""
    lines: list[str] = []
    reverse = {v: k for k, v in deck.nodes.items()}

    rows = [("网络", "电压(V)")]
    for node, net_name in sorted(reverse.items(), key=lambda kv: kv[1]):
        key = f"v({node})"
        if key in values:
            rows.append((net_name, f"{values[key]:.4f}"))
    width = max(len(r[0]) for r in rows)
    lines.append("工作点电压")
    for name, val in rows:
        lines.append(f"  {name.ljust(width)}  {val}")

    if deck.currents:
        lines.append("")
        lines.append("支路电流")
        rows = [("器件", "电流")]
        for ref, vec in deck.currents.items():
            if vec in values:
                amps = values[vec]
                rows.append((ref, "0.000 mA" if abs(amps) < 5e-7 else f"{amps * 1000:.3f} mA"))
        width = max(len(r[0]) for r in rows)
        for name, val in rows:
            lines.append(f"  {name.ljust(width)}  {val}")

    if deck.unmodeled:
        lines.append("")
        lines.append("未建模（未参与仿真）：" + "、".join(deck.unmodeled))
    return "\n".join(lines)


def issues_from_sim(deck: SimDeck, error: str = "") -> list[Issue]:
    """仿真本身失败时转成 Issue，参与现有闭环。"""
    if not error:
        return []
    return [Issue("SIM_FAILED", ERROR, f"仿真未完成：{error}", "spice", "检查 deck / 模型定义")]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse

    from .ngspice import find_library, simulate

    parser = argparse.ArgumentParser(description="IR -> SPICE deck，并跑一次工作点仿真")
    parser.add_argument("--design", default="examples/design.json")
    parser.add_argument("--out", default="runs/sim")
    parser.add_argument("--analysis", default="op", help="目前支持 op")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    ir = DesignIR.load_json(args.design)
    deck = build_deck(ir, analyses=(args.analysis,))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    deck.write(out_dir / "deck.cir")

    dll = find_library()
    print(f"设计：{ir.spec.title}（{len(ir.bom)} 器件 / {len(ir.nets)} 网络）")
    print(f"ngspice：{dll or '未找到'}")
    print(f"deck：{out_dir / 'deck.cir'}（{len(deck.text.splitlines())} 行）")
    if deck.notes:
        print("建模说明：")
        for note in deck.notes:
            print(f"  - {note}")

    if not dll:
        print("未找到 ngspice.dll，只生成 deck 不仿真。")
        return 1

    result = simulate(deck.text, deck.vectors, out_dir)
    if not result.ok:
        print(f"仿真失败：{result.error}")
        for line in result.log[-5:]:
            print("  ", line)
        return 1

    (out_dir / "values.json").write_text(
        json.dumps({"values": result.values, "version": result.version}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(result.values, ensure_ascii=False, indent=2))
    else:
        print()
        print(summarize(ir, deck, result.values))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
