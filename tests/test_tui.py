"""终端界面测试：不依赖真实终端，只测折叠逻辑与渲染几何。"""

from __future__ import annotations

import pytest

from pcb_agent.steps import GLYPHS, StepRecorder
from pcb_agent.ui import tui


def begin(phase: str, **payload) -> dict:
    return {"t": 0.0, "kind": "step", "phase": phase, "status": "begin", **payload}


def end(phase: str, **payload) -> dict:
    return {"t": 0.0, "kind": "step", "phase": phase, "status": "end", **payload}


# --------------------------------------------------------------------------- #
# 折叠逻辑
# --------------------------------------------------------------------------- #
def test_begin_end_pair():
    rec = StepRecorder()
    rec.consume(begin("analyze", input={"需求": "做个最小系统板"}))
    rec.consume(end("analyze", elapsed=0.02, output={"板卡": "Arduino 最小系统板"}))
    assert len(rec.steps) == 1
    step = rec.steps[0]
    assert step.status == "ok"
    assert step.inputs["需求"].startswith("做个")
    assert step.summary() == "Arduino 最小系统板"


def test_warn_and_fail_status():
    rec = StepRecorder()
    rec.consume(begin("evaluate"))
    rec.consume(end("evaluate", warn=True, output={"错误": 0, "警告": 1}))
    rec.consume(begin("patch"))
    rec.consume(end("patch", ok=False, output={"应用": 0, "拒绝": 2}))
    assert [s.status for s in rec.steps] == ["warn", "fail"]


def test_running_step_visible():
    rec = StepRecorder()
    rec.consume(begin("design"))
    assert rec.running() is not None
    assert rec.running().summary() == "执行中…"
    rec.consume(end("design", output={"器件": 18, "网络": 11}))
    assert rec.running() is None
    assert rec.steps[0].summary() == "18 器件 / 11 网络"


def test_issues_event_updates_counters():
    rec = StepRecorder()
    rec.consume(begin("evaluate"))
    rec.consume({"kind": "issues", "round": 2, "errors": 3, "warnings": 1})
    assert (rec.errors, rec.warnings, rec.round_no) == (3, 1, 2)
    assert any("第 2 轮" in n for n in rec.steps[0].notes)


def test_unknown_event_is_ignored():
    rec = StepRecorder()
    assert rec.consume({"kind": "whatever", "t": 0}) is False


# --------------------------------------------------------------------------- #
# 渲染几何：每一行的显示宽度必须等于终端宽度
# --------------------------------------------------------------------------- #
def sample() -> StepRecorder:
    rec = StepRecorder(max_rounds=3)
    rec.consume(begin("analyze", input={"需求": "Arduino 最小系统板"}))
    rec.consume(end("analyze", elapsed=0.01, output={"板卡": "Arduino 最小系统板"}))
    rec.consume(begin("design"))
    rec.consume(end("design", elapsed=0.02, output={"器件": 18, "网络": 11}))
    rec.consume(begin("evaluate"))
    rec.consume(end("evaluate", elapsed=0.42, warn=True, output={"错误": 0, "警告": 1}))
    rec.consume(begin("finalize"))
    rec.consume(end("finalize", output={"产物": 6}))
    return rec


@pytest.mark.parametrize("width", [48, 60, 72, 80, 100, 140])
def test_render_width_is_exact(width):
    frame = tui.render(sample(), selected=2, width=width, height=24, subtitle="测试")
    widths = {tui.disp_width(line) for line in frame}
    assert widths == {width}, widths


@pytest.mark.parametrize("view", ["输入", "输出", "问题"])
def test_render_all_views(view):
    frame = tui.render(sample(), selected=1, view=view, width=100, height=24)
    text = "\n".join(tui.visible(x) for x in frame)
    assert "电路设计" in text


def test_narrow_layout_keeps_detail():
    frame = tui.render(sample(), selected=2, width=64, height=26)
    text = "\n".join(tui.visible(x) for x in frame)
    assert "渲染 + 校验" in text


def test_help_overlay_adds_box():
    plain = tui.render(sample(), width=100, height=24)
    with_help = tui.render(sample(), width=100, height=24, help_open=True)
    assert len(with_help) > len(plain)
    assert any("快捷键" in tui.visible(line) for line in with_help)


def test_clip_counts_east_asian_width():
    assert tui.disp_width("中文") == 4
    assert tui.disp_width("\x1b[32mOK\x1b[0m") == 2
    assert tui.clip("abcdef", 4).endswith("…")
    assert tui.disp_width(tui.clip("中文中文", 5)) <= 5


def test_glyphs_cover_all_statuses():
    for status in ("pending", "running", "ok", "warn", "fail", "skip"):
        assert status in GLYPHS


# --------------------------------------------------------------------------- #
# 驱动层
# --------------------------------------------------------------------------- #
def test_snapshot_writes_file(tmp_path):
    tui_obj = tui.Tui(snapshot_path=str(tmp_path / "frame.txt"))
    for event in [
        begin("analyze"),
        end("analyze", output={"板卡": "演示板"}),
    ]:
        tui_obj.feed(event)
    path = tui_obj.snapshot()
    text = (tmp_path / "frame.txt").read_text(encoding="utf-8")
    assert "PCB-Agent" in text and "演示板" in text
    assert "\x1b[" not in text, "快照必须是纯文本"


def test_supported_is_false_without_tty():
    # pytest 环境下 stdout 被捕获，不是 TTY
    assert tui.supported() is False
