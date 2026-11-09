"""官方符号库接入：从 KiCad 自带的 .kicad_sym 里读真实器件与引脚表。

解决什么问题
------------
``data/symbols.json`` 里的 10 个器件是手写的，引脚号靠人记。这是答辩最容易被问住的
地方（"能不能设计一块带 WiFi 的板"）。KiCad 装了 224 个符号库、上万个器件，
直接读它们才是正解——引脚表是权威来源，也是渲染器生成 lib_symbols 的依据。

解析复用 ``kicad_gate.parse_sexpr``，不引新依赖；也不依赖 KiCad 进程，
纯文件读取，离线可用、可测。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .kicad_gate import parse_sexpr

# 顶层符号必须缩进 1 级：KiCad 9/10 用 Tab，旧版本用 2 空格；
# 嵌套的单元子符号（NAME_0_1 / NAME_1_1）缩进更深，要排除
_SYMBOL_LINE = re.compile(r'^([ \t]+)\(symbol\s+"([^"]+)"', re.MULTILINE)


def _depth(indent: str) -> int:
    return indent.count("\t") + (len(indent) - indent.count("\t")) // 2

_PIN_TYPES = {
    "input", "output", "bidirectional", "tri_state", "passive", "free",
    "unspecified", "power_in", "power_out", "open_collector", "open_emitter",
    "no_connect",
}


def _candidates(root: str | Path | None = None) -> list[Path]:
    """按优先级给出符号库目录候选。"""
    out: list[Path] = []
    if root:
        out.append(Path(root))
    env = os.environ.get("KICAD_SYMBOL_DIR", "").strip()
    if env:
        out.append(Path(env))
    for base in (
        "F:/KiCard",
        "C:/Program Files/KiCad",
        "C:/Program Files (x86)/KiCad",
        "D:/KiCad",
        "F:/KiCad",
        "/usr/share/kicad",
        "/Applications/KiCad",
    ):
        base_path = Path(base)
        if not base_path.exists():
            continue
        out.append(base_path / "share" / "kicad" / "symbols")
        # Windows 安装形如 <root>/10.0/share/kicad/symbols
        for ver in ("10.0", "9.0", "8.0"):
            out.append(base_path / ver / "share" / "kicad" / "symbols")
    return out


class OfficialLibrary:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = None
        for cand in _candidates(root):
            if cand.is_dir() and any(cand.glob("*.kicad_sym")):
                self.root = cand
                break

    @property
    def available(self) -> bool:
        return self.root is not None

    def libraries(self) -> list[str]:
        if not self.available:
            return []
        return sorted(p.stem for p in self.root.glob("*.kicad_sym"))

    def _file(self, lib: str) -> Path:
        if not self.available:
            raise FileNotFoundError("未找到 KiCad 官方符号库目录")
        path = self.root / f"{lib}.kicad_sym"
        if not path.is_file():
            raise FileNotFoundError(f"符号库 {lib}.kicad_sym 不存在")
        return path

    # ------------------------------------------------------------------ #
    def symbol_names(self, lib: str) -> list[str]:
        text = self._file(lib).read_text(encoding="utf-8", errors="ignore")
        return [
            name for indent, name in _SYMBOL_LINE.findall(text) if _depth(indent) == 1
        ]

    def search(self, keyword: str, limit: int = 40) -> list[str]:
        """在全部库里按符号名做子串搜索，返回 ``库:符号``。"""
        keyword = keyword.lower()
        hits: list[str] = []
        for lib in self.libraries():
            try:
                names = self.symbol_names(lib)
            except OSError:
                continue
            for name in names:
                if keyword in name.lower():
                    hits.append(f"{lib}:{name}")
                    if len(hits) >= limit:
                        return hits
        return hits

    # ------------------------------------------------------------------ #
    def load(self, lib: str, symbol: str) -> dict[str, Any]:
        """解析一个符号，返回与我们器件库同构的 dict。"""
        tree = parse_sexpr(self._file(lib).read_text(encoding="utf-8", errors="ignore"))
        root = tree[0]
        nodes = {
            node[1]: node
            for node in root[1:]
            if isinstance(node, list) and len(node) > 1 and node[0] == "symbol"
        }
        node = nodes.get(symbol)
        if node is None:
            raise KeyError(f"{lib} 中没有符号 {symbol}")

        # 派生符号（extends）继承基类引脚
        base_pins: list[dict[str, Any]] = []
        for child in node[1:]:
            if isinstance(child, list) and child and child[0] == "extends":
                base = str(child[1])
                base_pins = self.load(lib, base)["pins"]
                break

        props = {}
        for child in node[1:]:
            if isinstance(child, list) and child and child[0] == "property":
                props[str(child[1])] = str(child[2]) if len(child) > 2 else ""

        pins = _collect_pins(node) or base_pins
        return {
            "desc": f"{lib}:{symbol}（KiCad 官方库）",
            "ref_prefix": props.get("Reference", "U").rstrip("?").rstrip("0123456789") or "U",
            "value": props.get("Value", symbol),
            "footprint": props.get("Footprint", ""),
            "category": lib.lower(),
            "keywords": [lib.lower(), symbol.lower()],
            "pins": pins,
        }


def _collect_pins(node: list) -> list[dict[str, Any]]:
    """递归收集 pin 节点。单元子符号里才放引脚，所以必须往下走。"""
    pins: list[dict[str, Any]] = []

    def walk(item: Any) -> None:
        if not isinstance(item, list) or not item:
            return
        if item[0] == "pin":
            ptype, name, number = "passive", "~", ""
            for child in item[1:]:
                if isinstance(child, str) and child in _PIN_TYPES:
                    ptype = child
                elif isinstance(child, list) and child:
                    if child[0] == "name" and len(child) > 1:
                        name = str(child[1])
                    elif child[0] == "number" and len(child) > 1:
                        number = str(child[1])
            if number:
                pins.append({"number": number, "name": name, "type": ptype})
            return
        for child in item:
            walk(child)

    for child in node[1:]:
        walk(child)
    return pins


def to_device(lib: str, symbol: str, root: str | Path | None = None) -> tuple[str, dict]:
    """把官方符号转成可直接写进 data/symbols.json 的 ``(id, device)``。"""
    library = OfficialLibrary(root)
    dev = library.load(lib, symbol)
    device_id = f"pcbagent:{lib}_{symbol}"
    return device_id, dev
