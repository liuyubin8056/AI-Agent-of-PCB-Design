"""封装库读取：解析 KiCad 官方 ``.kicad_mod``，供 .kicad_pcb 生成与布线使用。

为什么要自己解析
----------------
``kicad-cli`` 没有"把封装加到板子上"的接口，``pcbnew`` 又只能在 KiCad 自带 Python 里跑。
所以这里直接读 ``share/kicad/footprints/*.pretty/*.kicad_mod``——和读符号库是同一套办法，
纯文件、离线、可测。

保留原始 token
--------------
``kicad_gate.parse_sexpr`` 会剥掉引号，重新输出时无法还原 ``(layer "F.Cu")`` 这种写法。
所以这里用**保留原文的 token** 建树，输出时原样拼接，只在我们需要的地方插入节点
（net、Reference/Value、坐标），保证除改动处外与官方文件逐字一致。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _build(tokens: list[str]) -> list:
    """把原始 token 序列解析成嵌套列表，叶子节点保留原样字符串。"""
    stack: list[list] = [[]]
    for tok in tokens:
        if tok == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(tok)
    return stack[0]


def _emit(node, indent: int = 0) -> str:
    if isinstance(node, str):
        return node
    inner = [_emit(child, indent + 1) for child in node]
    simple = all(isinstance(c, str) for c in node)
    text = "(" + " ".join(inner) + ")"
    if simple or len(text) < 70:
        return text
    pad = "\t" * (indent + 1)
    return "(" + "\n" + pad + ("\n" + pad).join(inner) + "\n" + "\t" * indent + ")"


def _unquote(tok: str) -> str:
    if isinstance(tok, str) and tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return str(tok)


def _child(node: list, key: str) -> list | None:
    for item in node[1:]:
        if isinstance(item, list) and item and _unquote(item[0]) == key:
            return item
    return None


def _children(node: list, key: str) -> list[list]:
    return [i for i in node[1:] if isinstance(i, list) and i and _unquote(i[0]) == key]


def _num(tok) -> float:
    try:
        return float(_unquote(tok))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
@dataclass
class Pad:
    number: str
    x: float  # 封装局部坐标（mm）
    y: float
    w: float
    h: float
    drill: float = 0.0
    smd: bool = True
    layers: list[str] = field(default_factory=list)
    node: list = field(default_factory=list)

    @property
    def abs_x_offset(self) -> float:
        return self.x


@dataclass
class Footprint:
    fp_id: str  # Lib:Name
    node: list  # 原始树（footprint 节点）
    pads: list[Pad]
    bbox: tuple[float, float, float, float]  # 相对原点的 minx,miny,maxx,maxy

    def pad(self, number: str) -> Pad | None:
        for p in self.pads:
            if p.number == str(number):
                return p
        return None


def _footprint_dirs(root: str | Path | None = None) -> list[Path]:
    cands: list[Path] = []
    if root:
        cands.append(Path(root))
    for base in ("F:/KiCard", "C:/Program Files/KiCad", "C:/Program Files (x86)/KiCad",
                 "D:/KiCad", "F:/KiCad", "/usr/share/kicad"):
        base_path = Path(base)
        if not base_path.exists():
            continue
        cands.append(base_path / "share" / "kicad" / "footprints")
        for ver in ("10.0", "9.0", "8.0"):
            cands.append(base_path / ver / "share" / "kicad" / "footprints")
    return cands


class FootprintLibrary:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root: Path | None = None
        for cand in _footprint_dirs(root):
            if cand.is_dir() and any(cand.glob("*.pretty")):
                self.root = cand
                break
        self._cache: dict[str, Footprint] = {}

    @property
    def available(self) -> bool:
        return self.root is not None

    def _path(self, fp_id: str) -> Path:
        if not self.available:
            raise FileNotFoundError("未找到 KiCad 官方封装库目录")
        lib, _, name = fp_id.partition(":")
        if not name:
            raise FileNotFoundError(f"封装 id 应为 Lib:Name，收到 {fp_id!r}")
        path = self.root / f"{lib}.pretty" / f"{name}.kicad_mod"
        if not path.is_file():
            raise FileNotFoundError(f"封装不存在：{path}")
        return path

    def load(self, fp_id: str) -> Footprint:
        if fp_id in self._cache:
            return self._cache[fp_id]
        tree = _build(_tokenize(self._path(fp_id).read_text(encoding="utf-8", errors="ignore")))
        node = next(
            (n for n in tree if isinstance(n, list) and n and _unquote(n[0]) == "footprint"),
            None,
        )
        if node is None:
            raise ValueError(f"{fp_id} 不是合法封装文件")

        pads: list[Pad] = []
        minx = miny = 1e9
        maxx = maxy = -1e9
        for pad_node in _children(node, "pad"):
            at = _child(pad_node, "at")
            size = _child(pad_node, "size")
            if at is None or size is None:
                continue
            x, y = _num(at[1]), _num(at[2])
            w, h = _num(size[1]), _num(size[2])
            drill_node = _child(pad_node, "drill")
            drill = 0.0
            if drill_node is not None:
                nums = [t for t in drill_node[1:] if isinstance(t, str) and t != "oval"]
                drill = _num(nums[-1]) if nums else 0.0
            layers_node = _child(pad_node, "layers")
            layers = [_unquote(t) for t in layers_node[1:]] if layers_node else []
            pads.append(
                Pad(
                    number=_unquote(pad_node[1]) if len(pad_node) > 1 else "",
                    x=x, y=y, w=w, h=h, drill=drill,
                    smd="smd" in [_unquote(t) for t in pad_node[1:3] if isinstance(t, str)],
                    layers=layers, node=pad_node,
                )
            )
            minx, maxx = min(minx, x - w / 2), max(maxx, x + w / 2)
            miny, maxy = min(miny, y - h / 2), max(maxy, y + h / 2)

        if minx > maxx:  # 没解析到 pad 时退化为一个点
            minx = miny = -1.0
            maxx = maxy = 1.0
        fp = Footprint(fp_id=fp_id, node=node, pads=pads, bbox=(minx, miny, maxx, maxy))
        self._cache[fp_id] = fp
        return fp
