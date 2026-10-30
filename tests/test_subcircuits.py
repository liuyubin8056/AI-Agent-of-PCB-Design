"""子电路模板测试：位号唯一、引脚存在、关键网络齐全。"""

from __future__ import annotations

import pytest

from pcb_agent.library import Library
from pcb_agent.subcircuits import build_board

LIB = Library()
FULL = [
    {"id": "mcu_min_system", "params": {}},
    {"id": "power_ldo5v", "params": {}},
    {"id": "led_indicator", "params": {}},
    {"id": "i2c_sensor", "params": {}},
]


@pytest.fixture(scope="module")
def board():
    return build_board(FULL, LIB)


def test_no_duplicate_refs(board):
    parts, _nets, _log = board
    refs = [p.ref for p in parts]
    assert len(refs) == len(set(refs))


def test_all_pins_exist_in_library(board):
    parts, nets, _log = board
    by_ref = {p.ref: p for p in parts}
    for net in nets:
        for member in net.members:
            ref, num = member.split(".")
            item = by_ref[ref]
            assert LIB.pin_exists(item.symbol, num), f"{member} 在 {item.symbol} 中不存在"


def test_key_nets_present(board):
    _parts, nets, _log = board
    names = {n.name for n in nets}
    assert {"+5V", "GND", "RESET", "XTAL1", "XTAL2", "SDA", "SCL"} <= names


def test_power_and_ground_first(board):
    _parts, nets, _log = board
    kinds = [n.kind for n in nets]
    assert kinds.index("power") < kinds.index("ground") < kinds.index("signal")


def test_crystal_load_caps_attached(board):
    _parts, nets, _log = board
    by_name = {n.name: n.members for n in nets}
    for net in ("XTAL1", "XTAL2"):
        caps = [m for m in by_name[net] if m.startswith("C")]
        assert caps, f"{net} 缺少负载电容"


def test_unknown_subcircuit_is_skipped():
    parts, nets, log = build_board([{"id": "not_exist"}], LIB)
    assert parts == [] and nets == []
    assert any("跳过未知子电路" in x for x in log)


def test_led_drive_pin_follows_mcu_ref():
    """位号变化时，LED 的驱动引脚必须跟着 MCU 走，不能硬编码 U1。"""
    parts, nets, _log = build_board(
        [{"id": "mcu_min_system"}, {"id": "led_indicator"}], LIB
    )
    mcu = next(p for p in parts if p.symbol.endswith("MCU_ATmega328P"))
    ctrl = next(n for n in nets if n.name == "LED_CTRL")
    assert any(m.startswith(mcu.ref + ".") for m in ctrl.members)
