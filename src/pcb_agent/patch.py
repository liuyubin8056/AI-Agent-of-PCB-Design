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
from dataclasses import dataclass
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


def _next_ref(ir: DesignIR, prefix: str) -> str:
    used = set(ir.refs())
    n = 0
    while True:
        n += 1
        candidate = f"{prefix}{n}"
        if candidate not in used:
            return candidate


def apply(
    ir: DesignIR, ops: list[PatchOp], lib: Library | None = None
) -> tuple[DesignIR, list[str]]:
    """返回打过补丁的新 IR（不改原对象）以及逐条执行日志。"""
    lib = lib or Library()
    new = copy.deepcopy(ir)
    log: list[str] = []

    for op in ops:
        try:
            if op.op == "set_value":
                item = new.part(op.ref)
                if not item:
                    log.append(f"跳过：找不到 {op.ref}")
                    continue
                old = item.value
                item.value = op.value
                log.append(f"{op.ref} 值 {old} -> {op.value}")

            elif op.op == "add_part":
                sid = lib.resolve(op.symbol or "pcbagent:CAP")
                ref = op.ref or _next_ref(new, lib.ref_prefix(sid))
                new.bom.append(
                    BomItem(ref, sid, op.value or lib.default_value(sid),
                            lib.footprint(sid), role="用户新增", note=op.note)
                )
                log.append(f"新增 {ref} ({sid} {op.value or lib.default_value(sid)})")

            elif op.op == "remove_part":
                target = new.part(op.ref)
                if not target:
                    log.append(f"跳过：找不到 {op.ref}")
                    continue
                new.bom = [x for x in new.bom if x.ref != op.ref]
                for net in new.nets:
                    net.members = [m for m in net.members if not m.startswith(op.ref + ".")]
                log.append(f"删除 {op.ref}")

            elif op.op == "connect":
                if not op.net or not op.member:
                    log.append("跳过：connect 缺少 net 或 member")
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
                    continue
                net.members = [m for m in net.members if m != op.member]
                log.append(f"{op.member} 从 {op.net} 摘除")

            elif op.op == "rename_net":
                net = new.net(op.net)
                if not net:
                    log.append(f"跳过：找不到网络 {op.net}")
                    continue
                net.name = op.new_name
                net.kind = Net(name=op.new_name).kind
                log.append(f"{op.net} 改名为 {op.new_name}")

            else:
                log.append(f"跳过未知操作 {op.op!r}")
        except KeyError as exc:
            log.append(f"跳过 {op.op}：{exc}")

    new.nets = [n for n in new.nets if n.members]
    return new, log


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
