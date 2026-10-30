"""中间表示（IR）：LLM 与 KiCad 之间的唯一契约。

设计原则
--------
1. LLM 只允许产出本文件定义的结构（BoardSpec / BomItem / Net），
   绝不直接产出 KiCad 文件文本；
2. 所有字段都是可序列化的纯数据，方便落盘、diff 与单元测试；
3. 引脚一律用「位号.引脚号」表示，例如 ``U1.7``，引脚号而不是引脚名，
   因为引脚名可能含 ``~RESET/PC6`` 这类歧义字符。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ERROR = "error"
WARNING = "warning"

GROUND_NAMES = {"GND", "AGND", "DGND", "PGND", "EARTH"}
POWER_NAMES = {"VCC", "VDD", "VBUS", "VIN"}


# --------------------------------------------------------------------------- #
# 问题条目
# --------------------------------------------------------------------------- #
@dataclass
class Issue:
    """一条校验 / 审查发现的问题。"""

    code: str
    severity: str
    message: str
    where: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "where": self.where,
            "hint": self.hint,
        }

    def __str__(self) -> str:
        tag = "ERROR" if self.severity == ERROR else "WARN "
        loc = f" [{self.where}]" if self.where else ""
        return f"{tag} {self.code}{loc}: {self.message}"


# --------------------------------------------------------------------------- #
# 需求规格
# --------------------------------------------------------------------------- #
@dataclass
class BoardSpec:
    """需求分析 Agent 的产物：机器可读的板级规格。"""

    title: str
    summary: str = ""
    functions: list[str] = field(default_factory=list)
    supply_rails: list[str] = field(default_factory=list)
    interfaces: list[str] = field(default_factory=list)
    key_parts: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "functions": self.functions,
            "supply_rails": self.supply_rails,
            "interfaces": self.interfaces,
            "key_parts": self.key_parts,
            "constraints": self.constraints,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoardSpec":
        return cls(
            title=str(data.get("title") or "未命名板卡"),
            summary=str(data.get("summary") or ""),
            functions=[str(x) for x in data.get("functions") or []],
            supply_rails=[str(x) for x in data.get("supply_rails") or []],
            interfaces=[str(x) for x in data.get("interfaces") or []],
            key_parts=[str(x) for x in data.get("key_parts") or []],
            constraints=dict(data.get("constraints") or {}),
        )


# --------------------------------------------------------------------------- #
# BOM / 网络
# --------------------------------------------------------------------------- #
@dataclass
class BomItem:
    """一个器件实例。``symbol`` 必须是器件库里的 lib_id。"""

    ref: str
    symbol: str
    value: str
    footprint: str = ""
    role: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "symbol": self.symbol,
            "value": self.value,
            "footprint": self.footprint,
            "role": self.role,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BomItem":
        return cls(
            ref=str(data.get("ref", "")).strip(),
            symbol=str(data.get("symbol", "")).strip(),
            value=str(data.get("value", "")).strip(),
            footprint=str(data.get("footprint", "")).strip(),
            role=str(data.get("role", "")).strip(),
            note=str(data.get("note", "")).strip(),
        )


def net_kind(name: str) -> str:
    """按命名推断网络类型，只用于校验与渲染时的排版决策。"""
    upper = name.upper()
    if upper in GROUND_NAMES or upper.startswith("GND"):
        return "ground"
    if upper in POWER_NAMES or upper.startswith("+") or upper.startswith("V"):
        if upper.startswith("V") and len(upper) > 2 and upper[1:].isdigit():
            return "signal"
        return "power"
    return "signal"


@dataclass
class Net:
    """一条电气网络。``members`` 形如 ``["U1.7", "C3.1", "R1.1"]``。"""

    name: str
    members: list[str] = field(default_factory=list)
    kind: str = ""

    def __post_init__(self) -> None:
        if not self.kind:
            self.kind = net_kind(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "members": list(self.members)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Net":
        return cls(
            name=str(data.get("name", "")).strip(),
            members=[str(m).strip() for m in data.get("members") or []],
            kind=str(data.get("kind") or ""),
        )


# --------------------------------------------------------------------------- #
# 设计整体
# --------------------------------------------------------------------------- #
@dataclass
class DesignIR:
    """一份完整的设计：规格 + BOM + 网络表。"""

    spec: BoardSpec
    bom: list[BomItem] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------- 查询辅助 ----------------
    def part(self, ref: str) -> BomItem | None:
        for item in self.bom:
            if item.ref == ref:
                return item
        return None

    def net(self, name: str) -> Net | None:
        for net in self.nets:
            if net.name == name:
                return net
        return None

    def refs(self) -> list[str]:
        return [item.ref for item in self.bom]

    def nets_of_pin(self, pin_ref: str) -> list[Net]:
        return [net for net in self.nets if pin_ref in net.members]

    def pins_by_net(self) -> dict[str, list[str]]:
        return {net.name: list(net.members) for net in self.nets}

    def all_members(self) -> Iterable[str]:
        for net in self.nets:
            for member in net.members:
                yield member

    # ---------------- 结构自检（不依赖器件库） ----------------
    def structural_issues(self) -> list[Issue]:
        issues: list[Issue] = []
        seen_refs: set[str] = set()
        for item in self.bom:
            if not item.ref:
                issues.append(Issue("BOM_EMPTY_REF", ERROR, "BOM 中存在空位号"))
                continue
            if item.ref in seen_refs:
                issues.append(Issue("BOM_DUP_REF", ERROR, f"位号 {item.ref} 重复", item.ref))
            seen_refs.add(item.ref)
            if not item.symbol:
                issues.append(Issue("BOM_NO_SYMBOL", ERROR, f"{item.ref} 缺少 symbol", item.ref))

        seen_nets: set[str] = set()
        for net in self.nets:
            if not net.name:
                issues.append(Issue("NET_EMPTY_NAME", ERROR, "存在无名网络"))
                continue
            if net.name in seen_nets:
                issues.append(Issue("NET_DUP_NAME", ERROR, f"网络 {net.name} 重复定义", net.name))
            seen_nets.add(net.name)
            if len(net.members) < 2:
                issues.append(
                    Issue(
                        "NET_TOO_SMALL",
                        WARNING,
                        f"网络 {net.name} 只有 {len(net.members)} 个连接点",
                        net.name,
                    )
                )
            for member in net.members:
                if member.count(".") != 1 or not member.split(".")[1]:
                    issues.append(
                        Issue(
                            "NET_BAD_MEMBER",
                            ERROR,
                            f"连接点 {member!r} 格式非法，应为 位号.引脚号",
                            net.name,
                        )
                    )
        return issues

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.to_dict(),
            "bom": [item.to_dict() for item in self.bom],
            "nets": [net.to_dict() for net in self.nets],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DesignIR":
        return cls(
            spec=BoardSpec.from_dict(data.get("spec") or {}),
            bom=[BomItem.from_dict(x) for x in data.get("bom") or []],
            nets=[Net.from_dict(x) for x in data.get("nets") or []],
            meta=dict(data.get("meta") or {}),
        )

    def to_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load_json(cls, path: str | Path) -> "DesignIR":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---------------- 摘要 ----------------
    def summary_line(self) -> str:
        return (
            f"{self.spec.title}: {len(self.bom)} 个器件 / "
            f"{len(self.nets)} 条网络 / {sum(len(n.members) for n in self.nets)} 个连接点"
        )
