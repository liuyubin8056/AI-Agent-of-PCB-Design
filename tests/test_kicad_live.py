"""kicad_live 后端测试：不依赖 KiCad 是否在运行，只验证降级行为。"""

from __future__ import annotations

from pcb_agent.config import Settings
from pcb_agent.kicad_gate import KiCadGate
from pcb_agent.kicad_live import KiCadLive, LiveStatus


def make_settings(**kw) -> Settings:
    base = dict(offline=True, api_key="", model="", kicad_enabled=True)
    base.update(kw)
    return Settings(**base)


def test_status_never_raises():
    live = KiCadLive(make_settings())
    st = live.status()
    assert isinstance(st, LiveStatus)
    assert isinstance(st.available, bool)
    assert st.describe()  # 无论连不连上都有人话


def test_capability_text_mentions_boundary():
    text = KiCadLive(make_settings()).capability_text()
    assert "kicad-cli" in text
    assert "原理图" in text


def test_open_missing_file_returns_false(tmp_path):
    live = KiCadLive(make_settings())
    ok, msg = live.open_in_editor(tmp_path / "nope.kicad_sch")
    assert ok is False and "不存在" in msg


def test_backend_off_disables_gate():
    """PCB_KICAD_BACKEND=off 时，kicad-cli 网关必须彻底关闭。"""
    gate = KiCadGate(make_settings(kicad_backend="off"))
    assert gate.available is False


def test_backend_default_is_cli():
    assert make_settings().kicad_backend == "cli"
