"""两层栅格 A* 布线器：给定已摆放的板子，把每条网络连通。

设计取舍（为什么不直接调 Freerouting）
-------------------------------------
``kicad-cli`` 没有 Specctra DSN 导出（实测 ``pcb export`` 子命令里没有 dsn），
用 Freerouting 就只能靠 GUI 或自己写 DSN——两条都不比写个路由器省事。
自己写还有个好处：**结果确定**，同样的板子每次布出同样的线，能写进报告做复现。

算法
----
1. 建 1.27mm 栅格，两层（F.Cu / B.Cu），pad 按"外扩间距"占位当障碍；
2. 网络按引脚数降序处理，每个网络内部先连最近的一对，再逐点接到已成树上；
3. 单次连接用 A*，代价：直行 1、换层（过孔）12；
4. 路径合并共线点，输出 segment / via。
布不通就跳过并记录，最终以 DRC 的 unconnected 数量为准——不谎报布通率。
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .pcb import Board, _r, _uid

GRID = 1.27
TRACK_W = 0.25
CLEARANCE = 0.25
VIA_COST = 12
VIA_SIZE = 0.8
VIA_DRILL = 0.4

DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@dataclass
class NetRoute:
    net: str
    ok: bool
    pins: int = 0
    length: float = 0.0
    vias: int = 0


@dataclass
class RouteStats:
    routes: list[NetRoute] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.routes)

    @property
    def routed(self) -> int:
        return sum(1 for r in self.routes if r.ok)

    @property
    def ratio(self) -> float:
        return self.routed / self.total if self.total else 0.0

    @property
    def total_length(self) -> float:
        return sum(r.length for r in self.routes)

    @property
    def total_vias(self) -> int:
        return sum(r.vias for r in self.routes)

    def describe(self) -> str:
        return (
            f"布通 {self.routed}/{self.total}（{self.ratio:.0%}），"
            f"线长 {self.total_length:.1f} mm，过孔 {self.total_vias} 个"
        )


class _Grid:
    def __init__(self, width: float, height: float, grid: float = GRID) -> None:
        self.grid = grid
        self.nx = max(int(width / grid) + 1, 2)
        self.ny = max(int(height / grid) + 1, 2)
        # 0 空闲 / 1 障碍 / >1 已被某网络占用（存网络序号+2）
        self.cells = [bytearray(self.nx * self.ny) for _ in range(2)]
        self.pad_owner: list[dict[int, str]] = [{}, {}]
        self.pad_drill: dict[int, float] = {}  # 该格上焊盘的钻孔直径（判断能否打过孔）

    def idx(self, x: int, y: int) -> int:
        return y * self.nx + x

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.nx and 0 <= y < self.ny

    def cell_of(self, x_mm: float, y_mm: float) -> tuple[int, int]:
        return (
            min(max(int(round(x_mm / self.grid)), 0), self.nx - 1),
            min(max(int(round(y_mm / self.grid)), 0), self.ny - 1),
        )

    def mm(self, x: int, y: int) -> tuple[float, float]:
        return _r(x * self.grid), _r(y * self.grid)

    def block_pad(
        self, x_mm: float, y_mm: float, w: float, h: float, net: str, drill: float = 0.0
    ) -> None:
        """按「格心到 pad 矩形的真实距离」占位，而不是框一个矩形。

        用外接矩形会把 DIP 相邻 pad 之间的 0.47mm 走廊一起堵死，引脚就被围成孤岛，
        布线器几乎必然失败——这是第一版 2/11 布通率的真正原因。
        """
        thr = CLEARANCE + TRACK_W / 2
        hw, hh = w / 2, h / 2
        reach = int(math.ceil((max(hw, hh) + thr) / self.grid)) + 1
        cx, cy = self.cell_of(x_mm, y_mm)
        for layer in (0, 1):
            for dy in range(-reach, reach + 1):
                for dx in range(-reach, reach + 1):
                    gx, gy = cx + dx, cy + dy
                    if not self.inside(gx, gy):
                        continue
                    ddx = max(abs(gx * self.grid - x_mm) - hw, 0.0)
                    ddy = max(abs(gy * self.grid - y_mm) - hh, 0.0)
                    if math.hypot(ddx, ddy) >= thr:
                        continue
                    i = self.idx(gx, gy)
                    self.cells[layer][i] = 1
                    self.pad_owner[layer][i] = net
                    if drill:
                        self.pad_drill[i] = drill

    def pad_within(self, x: int, y: int, net: str, radius: int = 1) -> bool:
        """过孔有 0.8mm 实体，落在 pad 旁边一样会间距/孔距违规，所以要查邻域。"""
        for layer in (0, 1):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    gx, gy = x + dx, y + dy
                    if not self.inside(gx, gy):
                        continue
                    i = self.idx(gx, gy)
                    if self.cells[layer][i] != 1:
                        continue
                    # 别的网络的焊盘绝对不许打孔。本网络焊盘允许换层——插针焊盘
                    # 本来就贯穿两层，同网络叠孔在电气上无害（DRC 会给 holes_co_located
                    # 警告，属于已知取舍，见 README「自动布局布线」的实测结果）。
                    if self.pad_owner[layer].get(i, "") != net:
                        return True
        return False

    def free(self, x: int, y: int, layer: int, net_no: int, net: str) -> bool:
        if not self.inside(x, y):
            return False
        i = self.idx(x, y)
        cell = self.cells[layer][i]
        if cell == 0:
            return True
        if cell == 1:
            owner = self.pad_owner[layer].get(i, "")
            return bool(owner) and owner == net
        return cell == net_no


def _astar(
    g: _Grid,
    start: tuple[int, int],
    goals: set[tuple[int, int]],
    net_no: int,
    net: str,
    max_expand: int = 400_000,
) -> list[tuple[int, int, int]] | None:
    if start in goals:
        return [(start[0], start[1], 0)]

    def h(x: int, y: int) -> float:
        return min(abs(x - gx) + abs(y - gy) for gx, gy in goals)

    open_heap = [(h(*start), 0.0, start[0], start[1], 0)]
    best: dict[tuple[int, int, int], float] = {(start[0], start[1], 0): 0.0}
    parent: dict[tuple[int, int, int], tuple[int, int, int] | None] = {
        (start[0], start[1], 0): None
    }
    expanded = 0

    while open_heap:
        _, cost, x, y, layer = heapq.heappop(open_heap)
        if (x, y) in goals:
            path = []
            node: tuple[int, int, int] | None = (x, y, layer)
            while node is not None:
                path.append(node)
                node = parent[node]
            return list(reversed(path))

        expanded += 1
        if expanded > max_expand:
            return None
        if cost > best.get((x, y, layer), float("inf")) + 1e-9:
            continue

        for dx, dy in DIRS:
            nx, ny = x + dx, y + dy
            if not g.free(nx, ny, layer, net_no, net):
                continue
            key = (nx, ny, layer)
            new_cost = cost + 1
            if new_cost < best.get(key, float("inf")):
                best[key] = new_cost
                parent[key] = (x, y, layer)
                heapq.heappush(open_heap, (new_cost + h(nx, ny), new_cost, nx, ny, layer))

        other = 1 - layer
        key = (x, y, other)
        new_cost = cost + VIA_COST
        on_pad = g.pad_within(x, y, net)
        if (
            not on_pad  # 过孔不能打在焊盘上：会造成孔重叠，甚至短路
            and new_cost < best.get(key, float("inf"))
            and g.free(x, y, other, net_no, net)
        ):
            best[key] = new_cost
            parent[key] = (x, y, layer)
            heapq.heappush(open_heap, (new_cost, new_cost, x, y, other))
    return None


def _segments(path: list[tuple[int, int, int]], g: _Grid):
    """把折线压成直线段，并生成 KiCad 的 segment / via 文本。"""
    if len(path) < 2:
        return [], [], 0.0, 0
    net_line = []
    via_points: list[tuple[float, float]] = []
    points = [(g.mm(x, y), layer) for x, y, layer in path]

    runs: list[tuple[tuple[float, float], tuple[float, float], int]] = []
    start = points[0][0]
    start_layer = points[0][1]
    prev = points[0][0]
    prev_layer = start_layer
    direction = None
    length = 0.0
    vias = 0

    for (cur, layer) in points[1:]:
        if layer != prev_layer:
            if prev != start:
                runs.append((start, prev, prev_layer))
            vias += 1
            via_points.append(cur)
            start = cur
            prev = cur
            prev_layer = layer
            direction = None
            continue
        step = (round(cur[0] - prev[0], 3), round(cur[1] - prev[1], 3))
        if direction is not None and step != direction:
            runs.append((start, prev, prev_layer))
            start = prev
        direction = step
        length += abs(step[0]) + abs(step[1])
        prev = cur

    if prev != start:
        runs.append((start, prev, prev_layer))

    for a, b, layer in runs:
        net_line.append((a, b, layer))
    return net_line, via_points, length, vias


def route_board(board: Board, max_pins: int = 40) -> RouteStats:
    """对已摆放的板子布线，把结果写进 board.segments / board.vias。"""
    g = _Grid(board.width, board.height)

    # 1) pad 占位
    drill_of = {
        f"{pl.ref}.{pad.number}": pad.drill for pl in board.placements for pad in pl.fp.pads
    }
    for ref, pad_no, x, y, w, h in board.pad_flat():
        g.block_pad(x, y, w, h, board.net_of(ref, pad_no), drill_of.get(f"{ref}.{pad_no}", 0.0))

    # 2) 网络按引脚数降序
    pad_drill = {
        f"{pl.ref}.{pad.number}": pad.drill
        for pl in board.placements
        for pad in pl.fp.pads
    }
    nets = board.net_pads()
    ordered = sorted(nets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    stats = RouteStats()

    for net, pins in ordered:
        if len(pins) > max_pins:
            stats.routes.append(NetRoute(net, False, len(pins)))
            continue
        net_no = board.nets.get(net, 0) + 2
        cells = [g.cell_of(x, y) for _, x, y in pins]
        # 只有贴片焊盘需要补「焊盘中心 -> 栅格点」的短线：插针焊盘贯穿两层，
        # 走线落在它的栅格点上就已经电气连接，多补一段反而制造间距违规。
        stubs = []
        for idx, (label, px, py) in enumerate(pins):
            if pad_drill.get(label, 0) > 0:
                continue
            cx, cy = cells[idx]
            mx, my = g.mm(cx, cy)
            if abs(mx - px) > 1e-6 or abs(my - py) > 1e-6:
                stubs.append(((px, py), (mx, my), 0))

        tree: set[tuple[int, int]] = {cells[0]}
        connected = [cells[0]]
        ok = True
        net_runs: list[tuple[tuple[float, float], tuple[float, float], int]] = []
        net_vias: list[tuple[float, float]] = []
        length = 0.0
        vias = 0

        # g.cells 标记：先把自己引脚位置标成本网络，避免被自己的 pad 挡住
        for (cx, cy) in cells:
            for layer in (0, 1):
                i = g.idx(cx, cy)
                if g.cells[layer][i] == 0:
                    g.cells[layer][i] = net_no

        remaining = cells[1:]
        while remaining:
            # 选距离已有树最近的引脚先连
            best = None
            for cell in remaining:
                d = min(abs(cell[0] - t[0]) + abs(cell[1] - t[1]) for t in tree)
                if best is None or d < best[0]:
                    best = (d, cell)
            _, target = best
            path = _astar(g, target, tree, net_no, net)
            if path is None:
                ok = False
                remaining = [c for c in remaining if c != target]
                continue
            for x, y, layer in path:
                i = g.idx(x, y)
                if g.cells[layer][i] in (0, net_no):
                    g.cells[layer][i] = net_no
            runs, via_pts, seg_len, via_cnt = _segments(path, g)
            net_runs.extend(runs)
            net_vias.extend(via_pts)
            length += seg_len
            vias += via_cnt
            for x, y, _layer in path:
                tree.add((x, y))
            remaining = [c for c in remaining if c != target]

        net_runs.extend(stubs)
        for a, b, layer in net_runs:
            layer_name = "F.Cu" if layer == 0 else "B.Cu"
            board.segments.append(
                f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) '
                f"(width {TRACK_W}) (layer \"{layer_name}\") (net {board.nets[net]}) "
                f'(uuid "{_uid()}"))'
            )
        for vx, vy in net_vias:
            board.vias.append(
                f'  (via (at {vx} {vy}) (size {VIA_SIZE}) (drill {VIA_DRILL}) '
                f'(layers "F.Cu" "B.Cu") (net {board.nets[net]}) (uuid "{_uid()}"))'
            )
        stats.routes.append(NetRoute(net, ok, len(pins), round(length, 2), vias))

    return stats
