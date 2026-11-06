"""补丁机制：把「用户追问」表达成一组可审计的原子操作。

为什么不是「重新生成一遍」
--------------------------
重新生成会丢掉已经确认过的部分，方差大、token 贵、不可解释。
本模块把修改收敛成 6 个原子操作，每次改动都有 diff 可看：

    set_value   C3 -> 100nF
    add_part    新增一颗 CAP
    remove_part 删掉 D2
    connect     把 U1.13 接到 LED_CTRL
    disconnect  把 U1.13 从 LED_CTRL 上摘掉
    rename_net  SDA1 -> SDA

这同时覆盖作业要求的「基础第 5 步」与「DRC 闭环里的自动修复」。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from .ir import BomItem, DesignIR, Net
from .library import Library

OPS = ("set_value", "add_part", "remove_part", "connect", "disconnect", "rename_net")


@dataclass
class PatchOp:
    op: str
    ref: str = ""
    value: str = ""
    symbol: str = ""
    net: str = ""
    member: str = ""
    new_name: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchOp":
        return cls(
            op=str(data.get("op", "")).strip(),
            ref=str(data.get("ref", "")).strip(),
            value=str(data.get("value", "")).strip(),
            symbol=str(data.get("symbol", "")).strip(),
            net=str(data.get("net", "")).strip(),
            member=str(data.get("member", "")).strip(),
            new_name=str(data.get("new_name", "")).strip(),
            note=str(data.get("note", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        out = {"op": self.op}
        for key in ("ref", "value", "symbol", "net", "member", "new_name", "note"):
            val = getattr(self, key)
            if val:
                out[key] = val
        return out

    def describe(self) -> str:
        if self.op == "set_value":
            return f"{self.ref} 的值改为 {self.value}"
        if self.op == "add_part":
            return f"新增 {self.symbol} {self.value}"
        if self.op == "remove_part":
            return f"删除 {self.ref}"
        if self.op == "connect":
            return f"把 {self.member} 接入 {self.net}"
        if self.op == "disconnect":
            return f"把 {self.member} 从 {self.net} 摘除"
        if self.op == "rename_net":
            return f"网络 {self.net} 改名为 {self.new_name}"
        return f"{self.op}?"


@dataclass
class PatchResult:
    """补丁执行结果。

    支持 ``ir, log = apply(...)`` 的二元解包写法，同时暴露 applied / rejected，
    让编排器能判断「这批补丁是不是全废了」。
    """

    ir: DesignIR
    log: list[str] = field(default_factory=list)
    applied: int = 0
    rejected: int = 0

    def __iter__(self):
        yield self.ir
        yield self.log


def _next_ref(ir: DesignIR, prefix: str, used_extra: set[str] | None = None) -> str:
    used = set(ir.refs()) | (used_extra or set())
    n = 0
    while True:
        n += 1
        candidate = f"{prefix}{n}"
        if candidate not in used:
            return candidate


def apply(
    ir: DesignIR, ops: list[PatchOp], lib: Library | None = None
) -> PatchResult:
    """对 IR 应用补丁，返回新 IR（不改原对象）与逐条日志。

    分两阶段，保证原子性：
    1. 预解析：把 add_part 的 symbol 解析成库内 lib_id，并先分配好位号。
       解析失败的位号进入 ``bad_refs``；
    2. 执行：引用 ``bad_refs`` 的所有操作（含配套的 connect）整组丢弃。

    没有这一步，模型给一个 ``Device:C`` 就能留下「接了线但器件不存在」的半截设计。
    """
    lib = lib or Library()
    new = copy.deepcopy(ir)
    log: list[str] = []
    applied = 0
    rejected = 0

    # ---- 阶段 1：预解析 ----
    bad_refs: set[str] = set()
    if ops:
        used: set[str] = set()
        for op in ops:
            if op.op != "add_part":
                continue
            try:
                sid = lib.resolve(op.symbol or "pcbagent:CAP")
            except KeyError:
                ref = op.ref or "<未命名>"
                bad_refs.add(ref)
                log.append(
                    f"拒绝补丁：器件库中找不到 {op.symbol!r}，"
                    f"丢弃与 {ref} 相关的全部操作"
                )
                continue
            op.symbol = sid
            if not op.ref:
                op.ref = _next_ref(new, lib.ref_prefix(sid), used)
            used.add(op.ref)

    def _bad(op: PatchOp) -> bool:
        if not bad_refs:
            return False
        if op.ref and op.ref in bad_refs:
            return True
        for member in (op.member,):
            if member and member.split(".")[0] in bad_refs:
                return True
        return False

    # ---- 阶段 1.5：位号一致性与悬空引用 ----
    # 模型常见笔误：add_part ref=C1，但 connect 写成 C8.1。
    # 处理策略，按优先级：
    #   a) 本批只有一个新增器件且前缀相同 → 把笔误位号重定向到真位号（可审计）；
    #   b) 其余引用不存在位号的 connect/disconnect → 整条剔除，绝不制造悬空引用。
    adds = [op for op in ops if op.op == "add_part" and op.ref and op.ref not in bad_refs]
    known = set(new.refs()) | {op.ref for op in adds}
    if len(adds) == 1:
        real = adds[0].ref
        for op in ops:
            if op.op == "add_part" or not op.member:
                continue
            ref = op.member.split(".")[0]
            if ref in known or ref in bad_refs or not ref:
                continue
            if ref[0] == real[0]:
                op.member = real + op.member[len(ref):]
                log.append(f"位号笔误纠正：{ref} -> {real}")

    skip: set[int] = set()
    for op in ops:
        if op.op not in {"connect", "disconnect"} or not op.member:
            continue
        ref = op.member.split(".")[0]
        if ref and ref not in known:
            log.append(f"拒绝补丁：{op.member} 引用了不存在的位号 {ref}")
            skip.add(id(op))

    # ---- 阶段 2：执行 ----
    for op in ops:
        if _bad(op) or id(op) in skip:
            rejected += 1
            continue
        try:
            if op.op == "set_value":
                item = new.part(op.ref)
                if not item:
                    log.append(f"跳过：找不到 {op.ref}")
                    rejected += 1
                    continue
                old = item.value
                item.value = op.value
                log.append(f"{op.ref} 值 {old} -> {op.value}")

            elif op.op == "add_part":
                sid = lib.resolve(op.symbol)
                ref = op.ref
                value = op.value or lib.default_value(sid)
                new.bom.append(
                    BomItem(ref, sid, value, lib.footprint(sid),
                            role="用户新增", note=op.note)
                )
                log.append(f"新增 {ref} ({sid} {value})")

            elif op.op == "remove_part":
                target = new.part(op.ref)
                if not target:
                    log.append(f"跳过：找不到 {op.ref}")
                    rejected += 1
                    continue
                new.bom = [x for x in new.bom if x.ref != op.ref]
                for net in new.nets:
                    net.members = [m for m in net.members if not m.startswith(op.ref + ".")]
                log.append(f"删除 {op.ref}")

            elif op.op == "connect":
                if not op.net or not op.member:
                    log.append("跳过：connect 缺少 net 或 member")
                    rejected += 1
                    continue
                net = new.net(op.net)
                if net is None:
                    new.nets.append(Net(name=op.net, members=[op.member]))
                elif op.member not in net.members:
                    net.members.append(op.member)
                log.append(f"{op.member} -> {op.net}")

            elif op.op == "disconnect":
                net = new.net(op.net)
                if not net:
                    log.append(f"跳过：找不到网络 {op.net}")
                    rejected += 1
                    continue
                net.members = [m for m in net.members if m != op.member]
                log.append(f"{op.member} 从 {op.net} 摘除")

            elif op.op == "rename_net":
                net = new.net(op.net)
                if not net:
                    log.append(f"跳过：找不到网络 {op.net}")
                    rejected += 1
                    continue
                net.name = op.new_name
                net.kind = Net(name=op.new_name).kind
                log.append(f"{op.net} 改名为 {op.new_name}")

            else:
                log.append(f"跳过未知操作 {op.op!r}")
                rejected += 1
                continue
        except KeyError as exc:
            log.append(f"跳过 {op.op}：{exc}")
            rejected += 1
            continue
        applied += 1

    # 审计提示：本批新增的器件若没有任何连接，明确写进日志
    touched = {m.split(".")[0] for n in new.nets for m in n.members}
    for op in ops:
        if op.op == "add_part" and op.ref and op.ref not in touched:
            log.append(f"注意：新增的 {op.ref} 没有被任何网络连接")

    new.nets = [n for n in new.nets if n.members]
    return PatchResult(new, log, applied, rejected)


# --------------------------------------------------------------------------- #
# 离线兜底：从自然语言里抽最简单的修改意图
# --------------------------------------------------------------------------- #
_REF = r"([A-Z]{1,3}\d+)"
_SET_PATTERNS = (
    re.compile(rf"{_REF}\s*(?:的[^，。]*?)?(?:换成|改为|改成|更换为|换为)\s*([0-9A-Za-z\.\u4e00-\u9fa5]+)"),
    re.compile(rf"(?:把|将)\s*{_REF}\s*(?:的[^，。]*?)?(?:换成|改为|改成|更换为)\s*([0-9A-Za-z\.\u4e00-\u9fa5]+)"),
)
_ADD_PATTERN = re.compile(r"(?:增加|添加|加)(?:一颗|一个|一只)?\s*([0-9A-Za-z\.]*)\s*(电容|电阻|LED|按键|晶振|排针)?")

_CATEGORY_MAP = {
    "电容": "pcbagent:CAP",
    "电阻": "pcbagent:RES",
    "LED": "pcbagent:LED",
    "按键": "pcbagent:SW_PUSH",
    "晶振": "pcbagent:XTAL",
    "排针": "pcbagent:CONN_2",
}


def ops_from_text(text: str, ir: DesignIR) -> list[PatchOp]:
    """不调 LLM 时的最小可用解析：够应付「把电容换成 100nF」这类追问。"""
    text = (text or "").strip()
    ops: list[PatchOp] = []

    for pattern in _SET_PATTERNS:
        for ref, value in pattern.findall(text):
            if ir.part(ref) and not any(o.op == "set_value" and o.ref == ref for o in ops):
                ops.append(PatchOp("set_value", ref=ref, value=value))
    if ops:
        return ops

    m = _ADD_PATTERN.search(text)
    if m:
        value, kind = m.group(1), m.group(2)
        symbol = _CATEGORY_MAP.get(kind or "", "pcbagent:CAP")
        ops.append(PatchOp("add_part", symbol=symbol, value=value, note="来自自然语言追问"))
    return ops
