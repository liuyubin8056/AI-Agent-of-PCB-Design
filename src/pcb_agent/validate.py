"""电气规则校验器：确定性代码，不用 LLM。

为什么必须存在
--------------
LLM 会说「这个引脚接了」但实际没接，或者给一颗 28 脚 MCU 只配一颗去耦电容。
这些错误用几十行规则就能 100% 拦下来，比让另一个模型再看一遍便宜且可靠。

每条规则都返回 ``Issue``，评审 Agent 拿到的是「问题清单」而不是「重新猜一遍」。
"""

from __future__ import annotations

from collections import defaultdict

from .ir import ERROR, WARNING, DesignIR, Issue
from .library import Library

# 这些网络需要 SDA/SCL 上拉
_I2C_NET_HINTS = ("SDA", "SCL")
# 非电源引脚未连接时的严重级别：只做提示，不阻塞流程
_FLOATING_SEVERITY = WARNING


def _pin_type(lib: Library, symbol: str, number: str) -> str:
    pin = lib.pin(symbol, number)
    return pin["type"] if pin else "unknown"


def _pin_name(lib: Library, symbol: str, number: str) -> str:
    pin = lib.pin(symbol, number)
    return pin["name"] if pin else number


def _split(member: str) -> tuple[str, str]:
    ref, _, num = member.partition(".")
    return ref, num


class _Index:
    """把 IR 展开成方便查询的索引，避免每条规则重复遍历。"""

    def __init__(self, ir: DesignIR, lib: Library) -> None:
        self.ir = ir
        self.lib = lib
        self.parts = {item.ref: item for item in ir.bom}
        self.pin_nets: dict[str, list[str]] = defaultdict(list)
        for net in ir.nets:
            for member in net.members:
                self.pin_nets[member].append(net.name)
        self.net_members = {net.name: list(net.members) for net in ir.nets}
        self.net_kind = {net.name: net.kind for net in ir.nets}

    def pin_type(self, member: str) -> str:
        ref, num = _split(member)
        item = self.parts.get(ref)
        if not item:
            return "unknown"
        return _pin_type(self.lib, item.symbol, num)

    def pin_name(self, member: str) -> str:
        ref, num = _split(member)
        item = self.parts.get(ref)
        if not item:
            return num
        return _pin_name(self.lib, item.symbol, num)

    def symbol_of(self, ref: str) -> str:
        item = self.parts.get(ref)
        return item.symbol if item else ""

    def nets_of_kind(self, kind: str) -> list[str]:
        return [name for name, k in self.net_kind.items() if k == kind]

    def resistors_to_power(self, net_name: str) -> list[str]:
        """返回「一端在本网络、另一端在电源网络」的电阻位号。"""
        power_nets = set(self.nets_of_kind("power"))
        hits = []
        for member in self.net_members.get(net_name, []):
            ref, _ = _split(member)
            item = self.parts.get(ref)
            if not item or self.lib.category(item.symbol) != "passive":
                continue
            if self.lib.ref_prefix(item.symbol) != "R":
                continue
            for peer, peer_nets in self.pin_nets.items():
                if peer.startswith(ref + ".") and peer != member:
                    if set(peer_nets) & power_nets:
                        hits.append(ref)
                        break
        return hits

    def caps_to_ground(self, net_name: str) -> list[str]:
        ground_nets = set(self.nets_of_kind("ground"))
        hits = []
        for member in self.net_members.get(net_name, []):
            ref, _ = _split(member)
            item = self.parts.get(ref)
            if not item or self.lib.ref_prefix(item.symbol) != "C":
                continue
            for peer, peer_nets in self.pin_nets.items():
                if peer.startswith(ref + ".") and peer != member:
                    if set(peer_nets) & ground_nets:
                        hits.append(ref)
                        break
        return hits

    def power_nets_connected_to(self, ref: str) -> set[str]:
        power_nets = set(self.nets_of_kind("power"))
        out: set[str] = set()
        for member, nets in self.pin_nets.items():
            if member.startswith(ref + "."):
                out |= set(nets) & power_nets
        return out


# --------------------------------------------------------------------------- #
# 规则实现
# --------------------------------------------------------------------------- #
def check_unknown_symbols(idx: _Index) -> list[Issue]:
    issues = []
    for item in idx.ir.bom:
        if not idx.lib.has(item.symbol):
            issues.append(
                Issue("LIB_UNKNOWN_SYMBOL", ERROR,
                      f"{item.ref} 使用了器件库中不存在的符号 {item.symbol}", item.ref,
                      "改从器件库目录中选 symbol")
            )
    return issues


def check_members_exist(idx: _Index) -> list[Issue]:
    issues = []
    for net in idx.ir.nets:
        for member in net.members:
            ref, num = _split(member)
            item = idx.parts.get(ref)
            if item is None:
                issues.append(
                    Issue("NET_DANGLING_REF", ERROR,
                          f"网络 {net.name} 引用了不存在的位号 {ref}", net.name)
                )
                continue
            if idx.lib.has(item.symbol) and not idx.lib.pin_exists(item.symbol, num):
                issues.append(
                    Issue("PIN_NOT_EXIST", ERROR,
                          f"网络 {net.name} 引用了 {ref} 不存在的引脚 {num}", net.name,
                          f"{item.symbol} 没有这个引脚，检查引脚号是否从引脚表里选")
                )
    return issues


def check_pin_single_net(idx: _Index) -> list[Issue]:
    issues = []
    for member, nets in idx.pin_nets.items():
        if len(nets) > 1:
            issues.append(
                Issue("PIN_MULTIPLE_NETS", ERROR,
                      f"{member} 同时出现在 {len(nets)} 条网络：{', '.join(nets)}", member,
                      "一个引脚只能属于一条网络")
            )
    return issues


def check_power_pins_connected(idx: _Index) -> list[Issue]:
    issues = []
    for item in idx.ir.bom:
        if not idx.lib.has(item.symbol):
            continue
        for pin in idx.lib.pins(item.symbol):
            if pin["type"] != "power_in":
                continue
            member = f"{item.ref}.{pin['number']}"
            if not idx.pin_nets.get(member):
                issues.append(
                    Issue("POWER_PIN_UNCONNECTED", ERROR,
                          f"{item.ref} 的电源引脚 {pin['name']}({pin['number']}) 未连接",
                          item.ref, "电源引脚必须接到电源或地网络")
                )
    return issues


def check_power_rail_source(idx: _Index) -> list[Issue]:
    """每条电源网络都必须有驱动源：power_out 引脚，或经接插件由外部供电。"""
    issues = []
    for name in idx.nets_of_kind("power"):
        members = idx.net_members[name]
        has_source = any(idx.pin_type(m) == "power_out" for m in members)
        external = any(
            idx.lib.category(idx.symbol_of(_split(m)[0])) == "connector" for m in members
        )
        if has_source or external:
            continue
        issues.append(
            Issue("POWER_RAIL_NO_SOURCE", WARNING,
                  f"电源网络 {name} 没有 power_out 驱动源", name,
                  "检查是否缺少稳压器，或该网络应由外部供电")
        )
    return issues


def check_decoupling(idx: _Index) -> list[Issue]:
    """每个 IC 电源脚都要有一颗对地去耦电容（按数量配比做近似判断）。

    严格做法需要 PCB 布局信息才能判断「是否紧靠引脚」，原理图阶段用
    「电容数 >= 电源脚数」作为可执行的近似判据，并在报告里说明这一取舍。
    """
    issues = []
    for name in idx.nets_of_kind("power"):
        ic_pins = [m for m in idx.net_members[name] if idx.pin_type(m) == "power_in"]
        if not ic_pins:
            continue
        caps = idx.caps_to_ground(name)
        if len(caps) < len(ic_pins):
            issues.append(
                Issue("POWER_RAIL_NO_DECOUPLING", ERROR,
                      f"电源网络 {name} 上有 {len(ic_pins)} 个 IC 电源脚，"
                      f"但只有 {len(caps)} 颗对地电容", name,
                      "每个电源脚附近都应有一颗 100nF 去耦电容")
            )
    return issues


def check_reset_pullup(idx: _Index) -> list[Issue]:
    issues = []
    for net in idx.ir.nets:
        for member in net.members:
            name = idx.pin_name(member).upper()
            if "RESET" not in name:
                continue
            if idx.pin_type(member) == "power_in":
                continue
            if not idx.resistors_to_power(net.name):
                issues.append(
                    Issue("RESET_NO_PULLUP", ERROR,
                          f"复位引脚 {member} 所在网络 {net.name} 没有上拉电阻", net.name,
                          "复位脚悬空会被干扰误触发，需要 10k 上拉到电源")
                )
    return issues


def check_crystal_load_caps(idx: _Index) -> list[Issue]:
    issues = []
    for item in idx.ir.bom:
        if idx.lib.category(item.symbol) != "clock":
            continue
        for pin in idx.lib.pins(item.symbol):
            member = f"{item.ref}.{pin['number']}"
            nets = idx.pin_nets.get(member) or []
            if not nets:
                continue
            if not idx.caps_to_ground(nets[0]):
                issues.append(
                    Issue("XTAL_NO_LOAD_CAP", ERROR,
                          f"晶振 {item.ref} 的引脚 {pin['number']} 所在网络没有负载电容",
                          nets[0], "晶振两端各需一颗负载电容对地，典型 22pF")
                )
    return issues


def check_i2c_pullup(idx: _Index) -> list[Issue]:
    issues = []
    for net in idx.ir.nets:
        if not any(h in net.name.upper() for h in _I2C_NET_HINTS):
            continue
        if not idx.resistors_to_power(net.name):
            issues.append(
                Issue("I2C_NO_PULLUP", ERROR,
                      f"I2C 网络 {net.name} 缺少上拉电阻", net.name,
                      "I2C 是开漏总线，SDA/SCL 必须各有一颗上拉")
            )
    return issues


def check_floating_pins(idx: _Index) -> list[Issue]:
    """按器件聚合，避免 28 脚 MCU 刷屏。"""
    issues = []
    for item in idx.ir.bom:
        if not idx.lib.has(item.symbol):
            continue
        floating = []
        for pin in idx.lib.pins(item.symbol):
            if pin["type"] in {"power_in", "power_out", "no_connect"}:
                continue
            member = f"{item.ref}.{pin['number']}"
            if not idx.pin_nets.get(member):
                floating.append(f"{pin['number']}({pin['name']})")
        if floating:
            issues.append(
                Issue("PIN_FLOATING", _FLOATING_SEVERITY,
                      f"{item.ref} 有 {len(floating)} 个引脚未连接：{', '.join(floating[:8])}"
                      + (" ..." if len(floating) > 8 else ""),
                      item.ref, "确认这些引脚确实是空置功能")
            )
    return issues


def check_ground_uniqueness(idx: _Index) -> list[Issue]:
    grounds = idx.nets_of_kind("ground")
    if len(grounds) > 1:
        return [
            Issue("GND_MULTIPLE_NAMES", WARNING,
                  f"存在多个地网络名：{', '.join(grounds)}", "",
                  "单板建议统一为 GND；若确有模拟地请显式说明")
        ]
    return []


RULES = (
    check_unknown_symbols,
    check_members_exist,
    check_pin_single_net,
    check_power_pins_connected,
    check_power_rail_source,
    check_decoupling,
    check_reset_pullup,
    check_crystal_load_caps,
    check_i2c_pullup,
    check_floating_pins,
    check_ground_uniqueness,
)


def run_all(ir: DesignIR, lib: Library | None = None) -> list[Issue]:
    """跑完整套规则，返回按严重级别排序的问题清单。"""
    lib = lib or Library()
    idx = _Index(ir, lib)
    issues: list[Issue] = list(ir.structural_issues())
    for rule in RULES:
        issues.extend(rule(idx))
    issues.sort(key=lambda i: 0 if i.severity == ERROR else 1)
    return issues


def counts(issues: list[Issue]) -> tuple[int, int]:
    errors = sum(1 for i in issues if i.severity == ERROR)
    return errors, len(issues) - errors


def is_clean(issues: list[Issue]) -> bool:
    return counts(issues)[0] == 0


def report_text(issues: list[Issue]) -> str:
    errors, warnings = counts(issues)
    head = f"校验结果：{errors} 个错误 / {warnings} 个警告"
    body = "\n".join(f"  {i}" for i in issues)
    return head + ("\n" + body if body else "\n  （无问题）")
