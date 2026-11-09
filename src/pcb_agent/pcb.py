"""板文件生成：DesignIR -> .kicad_pcb（含分区摆放），并可选把布线结果写进去。

这一步补的是「进阶任务：自动布局布线」的另一半——原理图之后还得有板子。
做法与原理图一致：自己算坐标、自己拼 s-expression，不依赖 KiCad 进程；
封装来自官方 ``.kicad_mod``，所以 pad 尺寸、间距、钻孔都是真的，DRC 才有意义。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .footprints import Footprint, FootprintLibrary
from .ir import DesignIR

MARGIN = 8.0
GRID = 2.54


def _uid() -> str:
    return str(uuid.uuid4())


def _r(v: float) -> float:
    return round(v, 3)


@dataclass
class Placement:
    ref: str
    fp: Footprint
    x: float
    y: float
    width: float
    height: float

    def pads_abs(self) -> list[tuple[str, float, float]]:
        return [(p.number, _r(self.x + p.x), _r(self.y + p.y)) for p in self.fp.pads]

    def rect(self) -> tuple[float, float, float, float]:
        hw, hh = self.width / 2, self.height / 2
        return (self.x - hw, self.y - hh, self.x + hw, self.y + hh)


@dataclass
class Board:
    ir: DesignIR
    placements: list[Placement] = field(default_factory=list)
    width: float = 80.0
    height: float = 70.0
    nets: dict[str, int] = field(default_factory=dict)
    segments: list[str] = field(default_factory=list)
    vias: list[str] = field(default_factory=list)

    def net_of(self, ref: str, pad: str) -> str:
        member = f"{ref}.{pad}"
        for net in self.ir.nets:
            if member in net.members:
                return net.name
        return ""

    def pad_flat(self) -> list[tuple[str, str, float, float, float, float]]:
        """(ref, pad, x, y, w, h) —— 布线器要用的扁平清单。"""
        out = []
        for pl in self.placements:
            for pad in pl.fp.pads:
                out.append((pl.ref, pad.number, _r(pl.x + pad.x), _r(pl.y + pad.y), pad.w, pad.h))
        return out

    def net_pads(self) -> dict[str, list[tuple[str, float, float]]]:
        """网络 -> [(ref.pad, x, y)]，跳过单点网络。"""
        table: dict[str, list[tuple[str, float, float]]] = {}
        for pl in self.placements:
            for pad in pl.fp.pads:
                net = self.net_of(pl.ref, pad.number)
                if not net:
                    continue
                table.setdefault(net, []).append(
                    (f"{pl.ref}.{pad.number}", _r(pl.x + pad.x), _r(pl.y + pad.y))
                )
        return {k: v for k, v in table.items() if len(v) > 1}


# --------------------------------------------------------------------------- #
# 摆放
# --------------------------------------------------------------------------- #
def _overlaps(a: tuple[float, float, float, float], b, gap: float = 2.0) -> bool:
    return not (
        a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1]
    )


def place_components(
    ir: DesignIR, fplib: FootprintLibrary, step: float = 2.54
) -> tuple[list[Placement], float, float]:
    """连接性驱动的贪心摆放：主控居中，其余器件依次放到它连接最多的器件旁边。"""
    loaded: dict[str, Footprint] = {}
    for item in ir.bom:
        if not item.footprint:
            continue
        try:
            loaded[item.ref] = fplib.load(item.footprint)
        except (FileNotFoundError, ValueError):
            continue
    if not loaded:
        raise RuntimeError("没有可用的封装，无法生成 PCB")

    placed: list[Placement] = []

    def add(ref: str, x: float, y: float) -> None:
        fp = loaded[ref]
        minx, miny, maxx, maxy = fp.bbox
        w, h = maxx - minx, maxy - miny
        # 让封装几何中心落在 (x, y)：抵消 bbox 偏移
        cx = (minx + maxx) / 2
        cy = (miny + maxy) / 2
        # 把封装原点吸到 1.27 栅格：DIP 这类 2.54 阵列的 pad 就正好落在栅格上，
        # 布线器的占位判断才有意义（否则 pad 中心落在半格，会算错间距）
        ox = round((x - cx) / 1.27) * 1.27
        oy = round((y - cy) / 1.27) * 1.27
        placed.append(Placement(ref, fp, _r(ox), _r(oy), w, h))

    # 主控居中
    mcu_ref = next(
        (r for r in loaded if loaded[r].fp_id.startswith(("Package_DIP", "Package_QFP", "Package_TO_SOT"))),
        next(iter(loaded)),
    )
    add(mcu_ref, 40.0, 40.0)

    # 剩余器件按「与已放置器件的共享网络数」降序，逐个找最近空位
    pending = [r for r in loaded if r != mcu_ref]
    net_members: dict[str, set[str]] = {}
    for net in ir.nets:
        for member in net.members:
            net_members.setdefault(net.name, set()).add(member.split(".")[0])

    while pending:
        def affinity(ref: str) -> int:
            score = 0
            for net, members in net_members.items():
                if ref in members and any(p.ref in members for p in placed):
                    score += 1
            return score

        pending.sort(key=lambda r: (-affinity(r), r))
        ref = pending.pop(0)
        fp = loaded[ref]
        w = max(fp.bbox[2] - fp.bbox[0], 2.0)
        h = max(fp.bbox[3] - fp.bbox[1], 2.0)

        # 目标点：与之相连的已放置器件的平均位置
        partners = [
            p for p in placed
            if any(
                ref in members and p.ref in members for members in net_members.values()
            )
        ]
        tx = sum(p.x for p in partners) / len(partners) if partners else 40.0
        ty = sum(p.y for p in partners) / len(partners) if partners else 40.0

        best = None
        for ring in range(0, 12):
            for ang in range(0, 360, 30):
                rad = math.radians(ang)
                x = tx + math.cos(rad) * (ring * 2.0)
                y = ty + math.sin(rad) * (ring * 2.0)
                x = round(x / step) * step
                y = round(y / step) * step
                rect = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                if rect[0] < MARGIN or rect[1] < MARGIN:
                    continue
                if any(_overlaps(rect, p.rect(), gap=5.0) for p in placed):
                    continue
                best = (x, y)
                break
            if best:
                break
        add(ref, best[0] if best else 40.0 + 8 * len(placed), best[1] if best else 70.0)

    xs = [p.rect() for p in placed]
    width = max(r[2] for r in xs) + MARGIN
    height = max(r[3] for r in xs) + MARGIN
    return placed, max(width, 60.0), max(height, 50.0)


# --------------------------------------------------------------------------- #
# 输出 .kicad_pcb
# --------------------------------------------------------------------------- #
_LAYERS = """  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )"""


def build_board(
    ir: DesignIR,
    fplib: FootprintLibrary | None = None,
    with_edge: bool = True,
) -> Board:
    fplib = fplib or FootprintLibrary()
    placements, width, height = place_components(ir, fplib)
    board = Board(ir=ir, placements=placements, width=width, height=height)
    for idx, net in enumerate(sorted({n.name for n in ir.nets})): 
        board.nets[net] = idx + 1
    board._with_edge = with_edge  # type: ignore[attr-defined]
    return board


def _pad_node(pad_node: list, net_name: str, net_no: int) -> str:
    """把 pad 节点的 net 补上，其余原样保留。"""
    from .footprints import _emit, _unquote

    cleaned = [
        item
        for item in pad_node
        if not (isinstance(item, list) and item and _unquote(item[0]) == "net")
    ]
    if net_name and net_no:
        cleaned.append(["net", str(net_no), f'"{net_name}"'])
    return _emit(cleaned, 2)


def render_pcb(board: Board) -> str:
    from .footprints import _emit, _unquote

    out: list[str] = []
    out.append("(kicad_pcb")
    out.append("  (version 20241229)")
    out.append('  (generator "pcb_agent")')
    out.append('  (generator_version "10.0")')
    out.append('  (general (thickness 1.6) (legacy_teardrops no))')
    out.append('  (paper "A4")')
    out.append(_LAYERS)
    out.append("  (setup (pad_to_mask_clearance 0))")
    out.append('  (net 0 "")')
    for name, idx in sorted(board.nets.items(), key=lambda kv: kv[1]):
        out.append(f'  (net {idx} "{name}")')

    # 封装：保留官方几何，只改名字/位号/坐标/pad 网络
    for idx, pl in enumerate(board.placements):
        node = pl.fp.node
        parts: list[str] = []
        name = pl.fp.fp_id
        parts.append(f'  (footprint "{name}" (layer "F.Cu")')
        parts.append(f'    (uuid "{_uid()}")')
        parts.append(f"    (at {_r(pl.x)} {_r(pl.y)})")
        for item in node[1:]:
            if not isinstance(item, list) or not item:
                continue
            key = _unquote(item[0])
            if key in {"version", "generator", "layer", "uuid", "at", "tstamp"}:
                continue
            if key == "property":
                prop = _unquote(item[1]) if len(item) > 1 else ""
                if prop in {"Reference", "Value"}:
                    value = pl.ref if prop == "Reference" else board.ir.part(pl.ref).value
                    new = [item[0], item[1], f'"{value}"'] + item[3:]
                    if len(item) > 2 and isinstance(item[2], str):
                        parts.append("    " + _emit(new, 2))
                    continue
                parts.append("    " + _emit(item, 2))
            elif key == "pad":
                pad_no = _unquote(item[1]) if len(item) > 1 else ""
                net_name = board.net_of(pl.ref, pad_no)
                parts.append("    " + _pad_node(item, net_name, board.nets.get(net_name, 0)))
            else:
                parts.append("    " + _emit(item, 2))
        parts.append("  )")
        out.extend(parts)

    if getattr(board, "_with_edge", True):
        w, h = _r(board.width), _r(board.height)
        for start, end in (
            ((0, 0), (w, 0)), ((w, 0), (w, h)),
            ((w, h), (0, h)), ((0, h), (0, 0)),
        ):
            out.append(
                f'  (gr_line (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
                f'(stroke (width 0.1) (type default)) (layer "Edge.Cuts") (uuid "{_uid()}"))'
            )

    out.extend(board.segments)
    out.extend(board.vias)
    out.append(")")
    return "\n".join(out) + "\n"


def write_pcb(board: Board, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_pcb(board), encoding="utf-8")
    return path
