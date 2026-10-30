"""渲染器：把 DesignIR 变成 KiCad 8 能打开的原理图（.kicad_sch）。

两条工程决策
------------
1. **自己生成 lib_symbols**，不去解析 KiCad 官方库。好处是引脚编号 100% 与网表
   一致（电气连接正确性优先），缺点是图形从简（矩形框）。后续可替换为官方库符号。
2. **不做布线，用全局标签表达连接**。KiCad 里「全局标签放在引脚端点上」即视为
   电气相连，因此整个原理图不需要一条 wire，天然免疫器件重叠导致的连线错乱。
   对「自动生成原理图」这个目标，这是最稳的 v0.1 策略。

坐标约定
--------
符号库坐标系 Y 轴向上、原理图坐标系 Y 轴向下，所以器件放到 (X, Y) 后，
引脚端点世界坐标 = (X + px, Y - py)。这是手写 .kicad_sch 最容易错的地方。
"""

from __future__ import annotations

import csv
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .ir import DesignIR
from .library import Library

KICAD_VERSION = 20231120  # KiCad 8 的 .kicad_sch 文件格式版本
PIN_SPACING = 2.54
PIN_LENGTH = 2.54
BODY_HALF_W = 5.08
CELL_W = 72.0
CELL_H = 58.0
MARGIN_X = 12.0
MARGIN_Y = 14.0


def _n(value: float) -> str:
    """数字格式化：去掉多余小数位，KiCad 对 2.54 / 2.540000 都接受，但 diff 更干净。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _u() -> str:
    return str(uuid.uuid4())


@dataclass
class PinGeom:
    number: str
    name: str
    ptype: str
    px: float
    py: float
    side: str  # "left" | "right"


@dataclass
class SymbolGeom:
    """一个器件符号的几何信息（符号坐标系）。"""

    pins: list[PinGeom] = field(default_factory=list)
    body_top: float = PIN_SPACING

    def pin(self, number: str) -> PinGeom | None:
        for p in self.pins:
            if p.number == number:
                return p
        return None


def geometry(lib: Library, symbol: str) -> SymbolGeom:
    """按引脚数生成左右分列的符号几何。"""
    pins = lib.pins(symbol)
    half = math.ceil(len(pins) / 2)
    left, right = pins[:half], pins[half:]
    rows = max(len(left), len(right), 1)
    top = (rows - 1) * PIN_SPACING / 2

    geom = SymbolGeom(body_top=top + PIN_SPACING)
    for idx, pin in enumerate(left):
        geom.pins.append(
            PinGeom(pin["number"], pin["name"], pin["type"],
                    -(BODY_HALF_W + PIN_LENGTH), top - idx * PIN_SPACING, "left")
        )
    for idx, pin in enumerate(right):
        geom.pins.append(
            PinGeom(pin["number"], pin["name"], pin["type"],
                    BODY_HALF_W + PIN_LENGTH, top - idx * PIN_SPACING, "right")
        )
    return geom


# --------------------------------------------------------------------------- #
# lib_symbols 生成
# --------------------------------------------------------------------------- #
def emit_lib_symbol(lib: Library, symbol: str) -> str:
    dev = lib.get(symbol)
    geom = geometry(lib, symbol)
    short = symbol.split(":")[-1]
    top = geom.body_top
    lines = [
        f'    (symbol "{symbol}"',
        "      (exclude_from_sim no) (in_bom yes) (on_board yes)",
        f'      (property "Reference" "{dev.get("ref_prefix", "U")}" '
        f"(at 0 {_n(top + 2.54)} 0) (effects (font (size 1.27 1.27))))",
        f'      (property "Value" "{dev.get("value", "")}" '
        f"(at 0 {_n(-(top + 2.54))} 0) (effects (font (size 1.27 1.27))))",
        f'      (property "Footprint" "{dev.get("footprint", "")}" '
        "(at 0 0 0) (effects (font (size 1.27 1.27)) hide))",
        f'      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
        f'      (symbol "{short}_0_1"',
        f"        (rectangle (start {_n(-BODY_HALF_W)} {_n(top)}) "
        f"(end {_n(BODY_HALF_W)} {_n(-top)})",
        "          (stroke (width 0.254) (type default)) (fill (type background)))",
        "      )",
        f'      (symbol "{short}_1_1"',
    ]
    for pin in geom.pins:
        angle = 0 if pin.side == "left" else 180
        lines.append(
            f"        (pin {pin.ptype} line (at {_n(pin.px)} {_n(pin.py)} {angle}) "
            f"(length {_n(PIN_LENGTH)})"
        )
        lines.append(
            f'          (name "{pin.name}" (effects (font (size 1.27 1.27))))'
        )
        lines.append(
            f'          (number "{pin.number}" (effects (font (size 1.27 1.27))))'
        )
        lines.append("        )")
    lines.append("      )")
    lines.append("    )")
    return "\n".join(lines)


def emit_power_flag_symbol() -> str:
    return "\n".join(
        [
            '    (symbol "pcbagent:PWR_FLAG"',
            "      (power) (pin_numbers hide) (pin_names (offset 0) hide)",
            "      (exclude_from_sim no) (in_bom yes) (on_board yes)",
            '      (property "Reference" "#FLG" (at 0 1.905 0) '
            "(effects (font (size 1.27 1.27)) hide))",
            '      (property "Value" "PWR_FLAG" (at 0 3.81 0) '
            "(effects (font (size 1.27 1.27))))",
            '      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
            '      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
            '      (symbol "PWR_FLAG_0_0"',
            "        (pin power_out line (at 0 0 90) (length 0)",
            '          (name "pwr" (effects (font (size 1.27 1.27))))',
            '          (number "1" (effects (font (size 1.27 1.27))))',
            "        )",
            "      )",
            "    )",
        ]
    )


# --------------------------------------------------------------------------- #
# 主体渲染
# --------------------------------------------------------------------------- #
def _rows_cols(count: int) -> tuple[int, int]:
    if count <= 3:
        return 1, max(count, 1)
    cols = min(6, max(3, math.ceil(count / 4)))
    rows = math.ceil(count / cols)
    return rows, cols


def render_schematic(ir: DesignIR, lib: Library | None = None) -> str:
    lib = lib or Library()
    root_uuid = _u()
    project = "pcb_agent"

    rows, cols = _rows_cols(len(ir.bom))
    height = MARGIN_Y * 2 + rows * CELL_H + 12
    paper = "A3" if height > 210 or (MARGIN_X * 2 + cols * CELL_W) > 297 else "A4"

    symbols_used: list[str] = []
    for item in ir.bom:
        if item.symbol not in symbols_used:
            symbols_used.append(item.symbol)

    out: list[str] = []
    out.append("(kicad_sch")
    out.append(f"  (version {KICAD_VERSION})")
    out.append('  (generator "pcb_agent")')
    out.append('  (generator_version "8.0")')
    out.append(f'  (uuid "{root_uuid}")')
    out.append(f'  (paper "{paper}")')
    out.append("  (title_block")
    out.append(f'    (title "{_escape(ir.spec.title)}")')
    out.append('    (rev "0.1")')
    out.append(f'    (comment 1 "由 pcb_agent 自动生成，请勿手工编辑")')
    out.append(f'    (comment 2 "{_escape(ir.spec.summary[:90])}")')
    out.append("  )")

    out.append("  (lib_symbols")
    for symbol in symbols_used:
        out.append(emit_lib_symbol(lib, symbol))
    out.append(emit_power_flag_symbol())
    out.append("  )")

    placements: list[tuple[object, float, float, SymbolGeom]] = []
    for idx, item in enumerate(ir.bom):
        row, col = divmod(idx, cols)
        x = MARGIN_X + col * CELL_W + CELL_W / 2
        y = MARGIN_Y + row * CELL_H + CELL_H / 2
        geom = geometry(lib, item.symbol)
        placements.append((item, x, y, geom))
        out.append(_emit_instance(item, x, y, geom, project, root_uuid))

    # 引脚端点 + 全局标签：整个原理图不需要一条 wire
    for item, x, y, geom in placements:
        for pin in geom.pins:
            member = f"{item.ref}.{pin.number}"
            nets = [n for n in ir.nets if member in n.members]
            if not nets:
                continue
            net = nets[0]
            wx = x + pin.px
            wy = y - pin.py
            angle = 180 if pin.side == "left" else 0
            justify = "right" if pin.side == "left" else "left"
            out.append(
                f'  (global_label "{_escape(net.name)}" (shape input) '
                f"(at {_n(wx)} {_n(wy)} {angle}) (fields_autoplaced yes)"
            )
            out.append(
                f"    (effects (font (size 1.27 1.27)) (justify {justify}))"
            )
            out.append(f'    (uuid "{_u()}")')
            out.append(
                f'    (property "Intersheetrefs" "${{INTERSHEET_REFS}}" '
                "(at 0 0 0) (effects (font (size 1.27 1.27)) hide))"
            )
            out.append("  )")

    # 电源网络标记：让 ERC 知道这些电源网络已被外部驱动
    power_nets = [n for n in ir.nets if n.kind in {"power", "ground"}]
    flag_y = MARGIN_Y + rows * CELL_H + 8
    for i, net in enumerate(power_nets):
        fx = MARGIN_X + 8 + i * 26
        out.append(
            f'  (symbol (lib_id "pcbagent:PWR_FLAG") (at {_n(fx)} {_n(flag_y)} 0) (unit 1)'
        )
        out.append("    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)")
        out.append(f'    (uuid "{_u()}")')
        out.append(
            f'    (property "Reference" "#FLG{i + 1}" (at {_n(fx)} {_n(flag_y - 2.54)} 0) '
            "(effects (font (size 1.27 1.27)) hide))"
        )
        out.append(
            f'    (property "Value" "PWR_FLAG" (at {_n(fx)} {_n(flag_y - 5.08)} 0) '
            "(effects (font (size 1.27 1.27))))"
        )
        out.append(
            f'    (property "Footprint" "" (at {_n(fx)} {_n(flag_y)} 0) '
            "(effects (font (size 1.27 1.27)) hide))"
        )
        out.append(
            f'    (property "Datasheet" "" (at {_n(fx)} {_n(flag_y)} 0) '
            "(effects (font (size 1.27 1.27)) hide))"
        )
        out.append(f'    (pin "1" (uuid "{_u()}"))')
        out.append(
            f'    (instances (project "{project}" '
            f'(path "/{root_uuid}" (reference "#FLG{i + 1}") (unit 1))))'
        )
        out.append("  )")
        out.append(
            f'  (global_label "{_escape(net.name)}" (shape input) '
            f"(at {_n(fx)} {_n(flag_y)} 0) (fields_autoplaced yes)"
        )
        out.append("    (effects (font (size 1.27 1.27)) (justify left))")
        out.append(f'    (uuid "{_u()}")')
        out.append(
            f'    (property "Intersheetrefs" "${{INTERSHEET_REFS}}" (at 0 0 0) '
            "(effects (font (size 1.27 1.27)) hide))"
        )
        out.append("  )")

    out.append('  (sheet_instances (path "/" (page "1")))')
    out.append(")")
    return "\n".join(out) + "\n"


def _emit_instance(item, x: float, y: float, geom: SymbolGeom, project: str, root: str) -> str:
    top = geom.body_top
    lines = [
        f'  (symbol (lib_id "{item.symbol}") (at {_n(x)} {_n(y)} 0) (unit 1)',
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)",
        f'    (uuid "{_u()}")',
        f'    (property "Reference" "{item.ref}" (at {_n(x + 7.62)} {_n(y - top - 1.27)} 0) '
        "(effects (font (size 1.27 1.27)) (justify left)))",
        f'    (property "Value" "{_escape(item.value)}" '
        f"(at {_n(x + 7.62)} {_n(y - top + 1.27)} 0) "
        "(effects (font (size 1.27 1.27)) (justify left)))",
        f'    (property "Footprint" "{item.footprint}" (at {_n(x)} {_n(y)} 0) '
        "(effects (font (size 1.27 1.27)) hide))",
        f'    (property "Datasheet" "" (at {_n(x)} {_n(y)} 0) '
        "(effects (font (size 1.27 1.27)) hide))",
    ]
    for pin in geom.pins:
        lines.append(f'    (pin "{pin.number}" (uuid "{_u()}"))')
    lines.append(
        f'    (instances (project "{project}" '
        f'(path "/{root}" (reference "{item.ref}") (unit 1))))'
    )
    lines.append("  )")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
    )


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def write_schematic(ir: DesignIR, path: str | Path, lib: Library | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_schematic(ir, lib)
    path.write_text(text, encoding="utf-8")
    return path


def write_bom_csv(ir: DesignIR, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["位号", "值", "器件符号", "封装", "作用", "备注"])
        for item in ir.bom:
            writer.writerow(
                [item.ref, item.value, item.symbol, item.footprint, item.role, item.note]
            )
    return path


def balance_check(text: str) -> tuple[bool, str]:
    """括号配平 + 引号配平自检。没有 KiCad 的机器上也能验一层。"""
    depth = 0
    in_str = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False, "出现多余的右括号"
    if in_str:
        return False, "存在未闭合的字符串"
    if depth != 0:
        return False, f"括号未配平，剩余深度 {depth}"
    return True, "ok"
