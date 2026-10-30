"""渲染器：把 DesignIR 变成 KiCad 能打开的原理图（.kicad_sch）。

三条工程决策
------------
1. **自己生成 lib_symbols**，不去解析 KiCad 官方库。好处是引脚编号 100% 与网表
   一致（电气连接正确性优先），缺点是图形从简（矩形框）。同时导出配套的
   ``pcbagent.kicad_sym`` + ``sym-lib-table``，让工程自带符号库，
   避免 ERC 报「当前配置不包含符号库」。
2. **不做布线，用全局标签表达连接**。全局标签放在引脚端点上即视为电气相连，
   整张图不需要一条 wire，天然免疫器件重叠导致的连线错乱。
3. **悬空引脚打 no_connect，PWR_FLAG 只加在没有 power_out 的网络**。
   前者消除 ``pin_not_connected``，后者避免 ``pin_to_pin``（两个 power output 相连）。

坐标约定
--------
符号库坐标系 Y 轴向上、原理图坐标系 Y 轴向下，所以器件放到 (X, Y) 后，
引脚端点世界坐标 = (X + px, Y - py)。所有坐标吸附到 1.27 mm 连接栅格，
否则 ERC 会报 ``endpoint_off_grid``。
"""

from __future__ import annotations

import csv
import math
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .ir import DesignIR
from .library import Library

KICAD_VERSION = 20231120  # KiCad 8 的 .kicad_sch / .kicad_sym 文件格式版本
PIN_SPACING = 2.54
PIN_LENGTH = 2.54
BODY_HALF_W = 5.08
GRID = 1.27
LIB_NAME = "pcbagent"

# 尺寸都取 2.54 的整数倍，保证引脚端点落在 1.27 栅格上
CELL_W = 71.12
CELL_H = 58.42
MARGIN_X = 12.7
MARGIN_Y = 12.7


def _n(value: float) -> str:
    """数字格式化：去掉多余小数位，KiCad 对 2.54 / 2.540000 都接受，但 diff 更干净。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _u() -> str:
    return str(uuid.uuid4())


def _snap(value: float) -> float:
    """吸附到 1.27 mm 连接栅格。"""
    return round(round(value / GRID) * GRID, 4)


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
# 符号定义（.kicad_sch 的 lib_symbols 与 .kicad_sym 共用同一段文本）
# --------------------------------------------------------------------------- #
def emit_lib_symbol(
    lib: Library, symbol: str, name: str | None = None, indent: str = "    "
) -> str:
    dev = lib.get(symbol)
    geom = geometry(lib, symbol)
    short = (name or symbol).split(":")[-1]
    top = geom.body_top
    p = indent
    lines = [
        f'{p}(symbol "{name or symbol}"',
        f"{p}  (exclude_from_sim no) (in_bom yes) (on_board yes)",
        f'{p}  (property "Reference" "{dev.get("ref_prefix", "U")}" '
        f"(at 0 {_n(top + 2.54)} 0) (effects (font (size 1.27 1.27))))",
        f'{p}  (property "Value" "{dev.get("value", "")}" '
        f"(at 0 {_n(-(top + 2.54))} 0) (effects (font (size 1.27 1.27))))",
        f'{p}  (property "Footprint" "{dev.get("footprint", "")}" '
        "(at 0 0 0) (effects (font (size 1.27 1.27)) hide))",
        f'{p}  (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))',
        f'{p}  (symbol "{short}_0_1"',
        f"{p}    (rectangle (start {_n(-BODY_HALF_W)} {_n(top)}) "
        f"(end {_n(BODY_HALF_W)} {_n(-top)})",
        f"{p}      (stroke (width 0.254) (type default)) (fill (type background)))",
        f"{p}  )",
        f'{p}  (symbol "{short}_1_1"',
    ]
    for pin in geom.pins:
        angle = 0 if pin.side == "left" else 180
        lines.append(
            f"{p}    (pin {pin.ptype} line (at {_n(pin.px)} {_n(pin.py)} {angle}) "
            f"(length {_n(PIN_LENGTH)})"
        )
        lines.append(f'{p}      (name "{pin.name}" (effects (font (size 1.27 1.27))))')
        lines.append(f'{p}      (number "{pin.number}" (effects (font (size 1.27 1.27))))')
        lines.append(f"{p}    )")
    lines.append(f"{p}  )")
    lines.append(f"{p})")
    return "\n".join(lines)


def emit_power_flag_symbol(name: str | None = None, indent: str = "    ") -> str:
    name = name or f"{LIB_NAME}:PWR_FLAG"
    short = name.split(":")[-1]
    p = indent
    return "\n".join(
        [
            f'{p}(symbol "{name}"',
            f"{p}  (power) (pin_numbers hide) (pin_names (offset 0) hide)",
            f"{p}  (exclude_from_sim no) (in_bom yes) (on_board yes)",
            f'{p}  (property "Reference" "#FLG" (at 0 1.905 0) '
            "(effects (font (size 1.27 1.27)) hide))",
            f'{p}  (property "Value" "PWR_FLAG" (at 0 3.81 0) '
            "(effects (font (size 1.27 1.27))))",
            f'{p}  (property "Footprint" "" (at 0 0 0) '
            "(effects (font (size 1.27 1.27)) hide))",
            f'{p}  (property "Datasheet" "" (at 0 0 0) '
            "(effects (font (size 1.27 1.27)) hide))",
            f'{p}  (symbol "{short}_0_0"',
            f"{p}    (pin power_out line (at 0 0 90) (length 0)",
            f'{p}      (name "pwr" (effects (font (size 1.27 1.27))))',
            f'{p}      (number "1" (effects (font (size 1.27 1.27))))',
            f"{p}    )",
            f"{p}  )",
            f"{p})",
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


def _needs_power_flag(ir: DesignIR, lib: Library, net_name: str) -> bool:
    """只有「没有 power_out 驱动源」的电源网络才需要 PWR_FLAG。

    否则同一网络上有两个 power output（例如 LDO 的 VOUT 加一颗 PWR_FLAG），
    ERC 会报 pin_to_pin。
    """
    net = ir.net(net_name)
    if net is None:
        return False
    for member in net.members:
        ref, _, num = member.partition(".")
        item = ir.part(ref)
        if not item or not lib.has(item.symbol):
            continue
        pin = lib.pin(item.symbol, num)
        if pin and pin["type"] == "power_out":
            return False
    return True


def render_schematic(ir: DesignIR, lib: Library | None = None) -> str:
    lib = lib or Library()
    root_uuid = _u()
    project = "pcb_agent"

    rows, cols = _rows_cols(len(ir.bom))
    width = MARGIN_X * 2 + cols * CELL_W
    height = MARGIN_Y * 2 + rows * CELL_H + 12.7
    paper = "A4" if height <= 210 and width <= 297 else "A3"

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
    out.append('    (comment 1 "由 pcb_agent 自动生成，请勿手工编辑")')
    out.append(f'    (comment 2 "{_escape(ir.spec.summary[:90])}")')
    out.append("  )")

    out.append("  (lib_symbols")
    for symbol in symbols_used:
        out.append(emit_lib_symbol(lib, symbol))
    out.append(emit_power_flag_symbol())
    out.append("  )")

    # ---------------- 器件摆放 ----------------
    placements: list[tuple[object, float, float, SymbolGeom]] = []
    for idx, item in enumerate(ir.bom):
        row, col = divmod(idx, cols)
        x = _snap(MARGIN_X + col * CELL_W + CELL_W / 2)
        y = _snap(MARGIN_Y + row * CELL_H + CELL_H / 2)
        geom = geometry(lib, item.symbol)
        placements.append((item, x, y, geom))
        out.append(_emit_instance(item, x, y, geom, project, root_uuid))

    # ---------------- 连接：全局标签 / 悬空标记 ----------------
    for item, x, y, geom in placements:
        for pin in geom.pins:
            member = f"{item.ref}.{pin.number}"
            wx = _snap(x + pin.px)
            wy = _snap(y - pin.py)
            nets = [n for n in ir.nets if member in n.members]
            if not nets:
                # 未连接的引脚必须显式标注，否则 ERC 报 pin_not_connected
                out.append(f'  (no_connect (at {_n(wx)} {_n(wy)}) (uuid "{_u()}"))')
                continue
            net = nets[0]
            angle = 180 if pin.side == "left" else 0
            justify = "right" if pin.side == "left" else "left"
            out.append(
                f'  (global_label "{_escape(net.name)}" (shape input) '
                f"(at {_n(wx)} {_n(wy)} {angle}) (fields_autoplaced yes)"
            )
            out.append(f"    (effects (font (size 1.27 1.27)) (justify {justify}))")
            out.append(f'    (uuid "{_u()}")')
            out.append(
                f'    (property "Intersheetrefs" "${{INTERSHEET_REFS}}" '
                "(at 0 0 0) (effects (font (size 1.27 1.27)) hide))"
            )
            out.append("  )")

    # ---------------- 电源网络标记 ----------------
    power_nets = [
        n.name
        for n in ir.nets
        if n.kind in {"power", "ground"} and _needs_power_flag(ir, lib, n.name)
    ]
    flag_y = _snap(MARGIN_Y + rows * CELL_H + 7.62)
    for i, net_name in enumerate(power_nets):
        fx = _snap(MARGIN_X + i * 25.4)
        label = f"#FLG{i + 1}"
        out.append(
            f'  (symbol (lib_id "{LIB_NAME}:PWR_FLAG") (at {_n(fx)} {_n(flag_y)} 0) (unit 1)'
        )
        out.append("    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)")
        out.append(f'    (uuid "{_u()}")')
        out.append(
            f'    (property "Reference" "{label}" (at {_n(fx)} {_n(flag_y - 2.54)} 0) '
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
            f'(path "/{root_uuid}" (reference "{label}") (unit 1))))'
        )
        out.append("  )")
        out.append(
            f'  (global_label "{_escape(net_name)}" (shape input) '
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
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# --------------------------------------------------------------------------- #
# 配套符号库与库表（让工程自包含，消除 lib_symbol_issues 告警）
# --------------------------------------------------------------------------- #
def render_symbol_lib(ir: DesignIR, lib: Library | None = None) -> str:
    lib = lib or Library()
    used: list[str] = []
    for item in ir.bom:
        if item.symbol not in used:
            used.append(item.symbol)
    lines = [
        "(kicad_symbol_lib",
        f"  (version {KICAD_VERSION})",
        '  (generator "pcb_agent")',
        '  (generator_version "8.0")',
    ]
    for symbol in used:
        # .kicad_sym 里的符号名不带库前缀
        lines.append(
            emit_lib_symbol(lib, symbol, name=symbol.split(":")[-1], indent="  ")
        )
    lines.append(emit_power_flag_symbol(name="PWR_FLAG", indent="  "))
    lines.append(")")
    return "\n".join(lines) + "\n"


def render_sym_lib_table() -> str:
    return "\n".join(
        [
            "(sym_lib_table",
            "  (version 7)",
            f'  (lib (name "{LIB_NAME}")(type "KiCad")'
            f'(uri "${{KIPRJMOD}}/{LIB_NAME}.kicad_sym")(options "")'
            '(descr "pcb_agent 自动生成的符号库"))',
            ")",
            "",
        ]
    )


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def write_schematic(ir: DesignIR, path: str | Path, lib: Library | None = None) -> Path:
    """写出 .kicad_sch，同时生成配套的符号库与库表。"""
    lib = lib or Library()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_schematic(ir, lib), encoding="utf-8")
    (path.parent / f"{LIB_NAME}.kicad_sym").write_text(
        render_symbol_lib(ir, lib), encoding="utf-8"
    )
    (path.parent / "sym-lib-table").write_text(render_sym_lib_table(), encoding="utf-8")
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
