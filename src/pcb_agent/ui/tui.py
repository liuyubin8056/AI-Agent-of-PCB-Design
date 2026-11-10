"""终端可视化界面：纯标准库 ANSI，不引入 rich / textual。

为什么自己写：项目从第一天起就是"零必需依赖"，装一个 TUI 库会让
`pip install -e .` 多拉一堆东西，助教按 README 跑不起来就失去意义。

三条设计约束：
1. **界面只读事件**，不改流水线：`Tui.feed(record)` 订阅 Tracer 的事件流。
2. **非 TTY 自动降级**：`--tui auto` 在管道 / CI / 窄终端下不接管屏幕。
3. **渲染是纯函数**：`render()` 输入状态输出字符串列表，可以脱离终端单测。
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from typing import Any

from ..console import ensure_utf8
from ..steps import Step, StepRecorder

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
REV = "\x1b[7m"
FG_GREEN = "\x1b[32m"
FG_YELLOW = "\x1b[33m"
FG_RED = "\x1b[31m"
FG_CYAN = "\x1b[36m"
FG_GRAY = "\x1b[90m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ALT_ON = "\x1b[?1049h"
ALT_OFF = "\x1b[?1049l"
HOME = "\x1b[H"
CLEAR = "\x1b[2J"

STATUS_COLOR = {
    "ok": FG_GREEN,
    "warn": FG_YELLOW,
    "fail": FG_RED,
    "running": FG_CYAN,
    "pending": FG_GRAY,
    "skip": FG_GRAY,
}
VIEWS = ("输入", "输出", "问题")


# --------------------------------------------------------------------------- #
# 文本宽度 / 截断（中文按两列算，否则边框会错位）
# --------------------------------------------------------------------------- #
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def visible(text: str) -> str:
    """去掉 ANSI 转义后的可见文本。宽度计算必须基于它，否则边框永远对不齐。"""
    return ANSI_RE.sub("", text)


def disp_width(text: str) -> int:
    width = 0
    for ch in visible(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return width


def clip(text: str, width: int) -> str:
    """按显示宽度截断。一旦需要截断就丢掉颜色：半截转义序列会污染整屏。"""
    if width <= 0:
        return ""
    if disp_width(text) <= width:
        return text
    out, used = [], 0
    for ch in visible(text):
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: str, width: int) -> str:
    text = clip(text, width)
    return text + " " * max(0, width - disp_width(text))


def _kv_lines(data: dict[str, Any], width: int, limit: int = 8) -> list[str]:
    """把输入/输出字典摊成「键  值」行，值过长就截断。"""
    lines: list[str] = []
    for key, value in list(data.items())[:limit]:
        if isinstance(value, (list, tuple)):
            text = "、".join(str(v) for v in value[:6])
            if len(value) > 6:
                text += f" …共{len(value)}项"
        elif isinstance(value, dict):
            text = "、".join(f"{k}={v}" for k, v in list(value.items())[:4])
        else:
            text = str(value)
        lines.append(f"{pad(str(key), 8)}{clip(text, width - 10)}")
    if len(data) > limit:
        lines.append(f"{DIM}… 另有 {len(data) - limit} 项{DIM}{RESET}")
    return lines or [f"{DIM}（无）{RESET}"]


# --------------------------------------------------------------------------- #
# 布局
# --------------------------------------------------------------------------- #
def _frame_row(left: str, right: str, left_w: int, width: int) -> str:
    """左右两列拼一行。宽度严格 = width，多出来的内容一律截断。"""
    right_w = width - left_w - 3
    return (
        f"{FG_CYAN}│{RESET}{pad(left, left_w)}"
        f"{FG_CYAN}│{RESET}{pad(right, right_w)}{FG_CYAN}│{RESET}"
    )


def _full_row(text: str, width: int) -> str:
    return f"{FG_CYAN}│{RESET}{pad(' ' + text if text else '', width - 2)}{FG_CYAN}│{RESET}"


def header_lines(rec: StepRecorder, title: str, subtitle: str, width: int) -> list[str]:
    inner = width - 2
    left = f"{BOLD}{title}{RESET}"
    right = f"轮次 {max(rec.round_no, 1)}/{rec.max_rounds} · {rec.elapsed:5.1f}s"
    gap = max(1, inner - disp_width(title) - disp_width(right) - 2)
    lines = [f"{FG_CYAN}┌{'─' * inner}┐{RESET}"]
    lines.append(
        f"{FG_CYAN}│{RESET} {left}{' ' * gap}{right} {FG_CYAN}│{RESET}"
    )
    if subtitle:
        lines.append(_full_row(f"{DIM}{subtitle}{RESET}", width))
    return lines


def step_lines(rec: StepRecorder, selected: int, width: int) -> list[str]:
    out: list[str] = []
    for idx, step in enumerate(rec.steps):
        color = STATUS_COLOR.get(step.status, "")
        marker = "▸" if idx == selected else " "
        head = f"{marker}{color}{step.glyph()}{RESET} {idx + 1:02d} {step.title}"
        right = step.duration_text()
        room = width - disp_width(right) - 1
        head = clip(head, room) if disp_width(head) > room else head
        out.append(f"{head}{' ' * max(1, width - disp_width(head) - disp_width(right))}{DIM}{right}{RESET}")
        summary = step.summary()
        if summary:
            out.append(f"   {DIM}{clip(summary, width - 3)}{RESET}")
    if not out:
        out.append(f"{DIM}等待第一步…{RESET}")
    return out


def detail_lines(step: Step | None, view: str, width: int, height: int) -> list[str]:
    if step is None:
        return [f"{DIM}（用 ↑↓ 选择左侧步骤）{RESET}"]
    head = (
        f"{BOLD}{step.title}{RESET}  "
        f"{STATUS_COLOR.get(step.status, '')}{step.glyph()} {step.status_text()}{RESET}  "
        f"{DIM}{step.duration_text()}{RESET}"
    )
    lines = [head, ""]
    if view == "输入":
        lines.append(f"{FG_CYAN}输入{RESET}")
        lines += _kv_lines(step.inputs, width)
    elif view == "输出":
        lines.append(f"{FG_CYAN}输出{RESET}")
        lines += _kv_lines(step.outputs, width)
    else:
        lines.append(f"{FG_CYAN}问题 / 备注{RESET}")
        if step.notes:
            lines += [clip(n, width) for n in step.notes[-6:]]
        elif step.outputs.get("问题码"):
            codes = step.outputs["问题码"]
            lines += [clip(str(c), width) for c in (codes if isinstance(codes, list) else [codes])]
        else:
            lines.append(f"{DIM}（无）{RESET}")
    return lines[: max(3, height)]


def footer_lines(width: int, message: str = "") -> list[str]:
    inner = width - 2
    keys = "↑↓ 选择  Tab 切换视图  f 跟随  s 快照  ? 帮助  q 退出"
    if width < 78:
        keys = "↑↓  Tab  f  s  ?  q"
    return [
        f"{FG_CYAN}├{'─' * inner}┤{RESET}",
        _full_row(f"{DIM}{keys}{RESET}", width),
        _full_row(f"{message or ''}", width),
        f"{FG_CYAN}└{'─' * inner}┘{RESET}",
    ]


def render(
    rec: StepRecorder,
    selected: int = -1,
    view: str = "输出",
    width: int = 100,
    height: int = 30,
    title: str = "PCB-Agent",
    subtitle: str = "",
    message: str = "",
    help_open: bool = False,
) -> list[str]:
    """把状态画成若干行文本。纯函数，不碰终端；每行显示宽度都等于 width。"""
    width = max(48, width)
    lines = header_lines(rec, title, subtitle, width)
    body_h = max(6, height - len(lines) - 4)

    step = rec.steps[selected] if 0 <= selected < len(rec.steps) else rec.running()
    if width < 78:  # 窄屏：步骤在上、详情在下
        upper = max(3, body_h // 2)
        left = step_lines(rec, selected, width - 2)[:upper]
        lower = detail_lines(step, view, width - 2, body_h - upper)
        lines.append(f"{FG_CYAN}├{'─' * (width - 2)}┤{RESET}")
        for i in range(upper):
            lines.append(_full_row(left[i] if i < len(left) else "", width))
        lines.append(f"{FG_CYAN}├{'─' * (width - 2)}┤{RESET}")
        for i in range(body_h - upper):
            lines.append(_full_row(lower[i] if i < len(lower) else "", width))
    else:
        left_w = min(36, max(28, width // 3))
        right_w = width - left_w - 3
        left = step_lines(rec, selected, left_w)[:body_h]
        right = detail_lines(step, view, right_w, body_h)
        lines.append(
            f"{FG_CYAN}├{'─' * left_w}┬{'─' * right_w}┤{RESET}"
        )
        for i in range(body_h):
            lines.append(
                _frame_row(
                    left[i] if i < len(left) else "",
                    right[i] if i < len(right) else "",
                    left_w,
                    width,
                )
            )
    lines.append(f"{FG_CYAN}├{'─' * (width - 2)}┤{RESET}")
    lines += footer_lines(width, message)[1:]
    if help_open:
        lines = overlay_help(lines, width, height)
    return lines


def overlay_help(lines: list[str], width: int, height: int) -> list[str]:
    keys = [
        "↑ / ↓    选择步骤（查看它的输入与输出）",
        "Tab      切换详情视图：输入 / 输出 / 问题",
        "f        跟随最新步骤（默认开启，手动选择后关闭）",
        "s        把当前画面存到 <输出目录>/ui-frame.txt",
        "q        请求退出（当前步骤结束后停止）",
        "空格     暂停/继续刷新（排查某一帧时用）",
    ]
    box_w = min(width - 6, 62)
    top = max(1, (height - len(keys) - 4) // 2)
    out = list(lines)
    out.insert(top, f"{FG_CYAN}╭{'─' * (box_w - 2)}╮{RESET}")
    out.insert(top + 1, f"{FG_CYAN}│{RESET}{BOLD}{pad(' 快捷键', box_w - 3)}{RESET}{FG_CYAN}│{RESET}")
    for i, key in enumerate(keys):
        out.insert(top + 2 + i, f"{FG_CYAN}│{RESET} {pad(key, box_w - 3)}{FG_CYAN}│{RESET}")
    out.insert(top + 2 + len(keys), f"{FG_CYAN}╰{'─' * (box_w - 2)}╯{RESET}")
    return out


# --------------------------------------------------------------------------- #
# 终端驱动
# --------------------------------------------------------------------------- #
class Tui:
    """接管终端、订阅事件、按需重画。

    用法::

        tui = Tui(title="PCB-Agent", subtitle="...")
        orch.on_event(tui.feed)
        tui.start()
        try:
            ...
        finally:
            tui.stop()
    """

    def __init__(
        self,
        title: str = "PCB-Agent",
        subtitle: str = "",
        max_rounds: int = 3,
        snapshot_path: str = "",
        fps: float = 8.0,
    ) -> None:
        self.rec = StepRecorder(max_rounds=max_rounds)
        self.title = title
        self.subtitle = subtitle
        self.snapshot_path = snapshot_path
        self.interval = 1.0 / max(1.0, fps)
        self.selected = -1
        self.view = "输出"
        self.follow = True
        self.help_open = False
        self.paused = False
        self.message = ""
        self._dirty = True
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._keys: threading.Thread | None = None
        self._active = False
        atexit.register(self._restore)

    # -- 事件 ------------------------------------------------------------ #
    def feed(self, record: dict[str, Any]) -> None:
        if self.rec.consume(record):
            if self.follow:
                self.selected = len(self.rec.steps) - 1
            self._dirty = True

    # -- 生命周期 -------------------------------------------------------- #
    def start(self) -> None:
        self.rec.started_at = time.perf_counter()
        # 必须先修编码再进 alt-screen：框线字符写不出去的话，第一帧就崩
        ensure_utf8()
        self._enter()
        self._active = True
        self._thread = threading.Thread(target=self._tick, daemon=True)
        self._thread.start()
        # 没有真终端就别起键盘线程：msvcrt.kbhit() 会抛异常污染输出
        if sys.stdin.isatty():
            self._keys = threading.Thread(target=self._read_keys, daemon=True)
            self._keys.start()

    def stop(self, final_note: str = "") -> None:
        self._stop.set()
        time.sleep(self.interval * 1.5)
        if final_note:
            self.message = final_note
            self._dirty = True
        try:
            self._draw()
        except OSError:
            pass
        self._restore()
        self._active = False

    def _enter(self) -> None:
        if os.name == "nt":
            os.system("")  # 打开 Windows 终端的 VT 转义处理
        sys.stdout.write(ALT_ON + HIDE_CURSOR + CLEAR)
        sys.stdout.flush()

    def _restore(self) -> None:
        if not self._active:
            return
        sys.stdout.write(SHOW_CURSOR + ALT_OFF)
        sys.stdout.flush()

    # -- 渲染循环 -------------------------------------------------------- #
    def _size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((100, 30))
        return size.columns, size.lines

    def _tick(self) -> None:
        while not self._stop.is_set():
            if self.rec.running() is not None or self._dirty:
                if not self.paused:
                    try:
                        self._draw()
                    except OSError:
                        return
                    self._dirty = False
            time.sleep(self.interval)

    def _draw(self) -> None:
        width, height = self._size()
        frame = render(
            self.rec, self.selected, self.view, width, height,
            self.title, self.subtitle, self.message, self.help_open,
        )
        sys.stdout.write(HOME + "\n".join(line.rstrip() for line in frame))
        sys.stdout.flush()

    def snapshot(self) -> str:
        width, height = self._size()
        frame = render(
            self.rec, self.selected, self.view, width, height,
            self.title, self.subtitle, "已保存快照", self.help_open,
        )
        target = self.snapshot_path or "ui-frame.txt"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_strip_ansi(line) for line in frame) + "\n")
        return target

    # -- 键盘 ------------------------------------------------------------ #
    def _read_keys(self) -> None:
        try:
            if os.name == "nt":
                self._read_keys_windows()
            else:
                self._read_keys_posix()
        except Exception:  # noqa: BLE001 - 键盘读不到不影响主流程（只是不能用快捷键）
            return

    def _read_keys_windows(self) -> None:  # pragma: no cover - 依赖真实终端
        import msvcrt

        while not self._stop.is_set():
            if not msvcrt.kbhit():
                time.sleep(0.05)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                arrow = msvcrt.getwch()
                self._on_key("up" if arrow == "H" else "down" if arrow == "P" else "?")
            else:
                self._on_key(ch)

    def _read_keys_posix(self) -> None:  # pragma: no cover - 依赖真实终端
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                if not select.select([sys.stdin], [], [], 0.05)[0]:
                    continue
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    self._on_key("up" if seq == "[A" else "down" if seq == "[B" else "?")
                else:
                    self._on_key(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _on_key(self, key: str) -> None:
        if key in ("q", "Q"):
            self.message = "已请求退出，当前步骤结束后停止…"
            self._dirty = True
            self._stop.set()
            return
        if key in ("up", "k"):
            self.follow = False
            self.selected = max(0, self.selected - 1)
        elif key in ("down", "j"):
            self.follow = False
            self.selected = min(len(self.rec.steps) - 1, self.selected + 1)
        elif key in ("\t", "tab"):
            self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        elif key in ("f", "F"):
            self.follow = True
            self.selected = len(self.rec.steps) - 1
            self.message = "跟随最新步骤"
        elif key in ("s", "S"):
            self.message = f"快照已保存：{self.snapshot()}"
        elif key in ("?", "h", "H"):
            self.help_open = not self.help_open
        elif key == " ":
            self.paused = not self.paused
            self.message = "已暂停刷新" if self.paused else "继续刷新"
        self._dirty = True


def _strip_ansi(text: str) -> str:
    return visible(text)


def supported(width: int | None = None) -> bool:
    """能不能开 TUI：必须是 TTY，且终端够宽。"""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_TUI") or os.environ.get("TERM") == "dumb":
        return False
    cols = width if width is not None else shutil.get_terminal_size((100, 30)).columns
    return cols >= 60
