"""官方符号库解析测试：装了 KiCad 才跑，没装自动跳过。"""

from __future__ import annotations

import pytest

from pcb_agent.official import OfficialLibrary, to_device

LIB = OfficialLibrary()
pytestmark = pytest.mark.skipif(
    not LIB.available, reason="未找到 KiCad 官方符号库目录"
)


def test_library_root_and_count():
    assert LIB.root is not None
    assert len(LIB.libraries()) > 50


def test_symbol_names_excludes_unit_children():
    """顶层符号不带 _0_1 / _1_1 这类单元后缀。"""
    names = LIB.symbol_names("Device")
    assert "R" in names and "C" in names
    assert not any(n.endswith("_1_1") for n in names)


def test_load_device_r_pins():
    dev = LIB.load("Device", "R")
    assert dev["ref_prefix"] == "R"
    assert [p["number"] for p in dev["pins"]] == ["1", "2"]
    assert all(p["type"] == "passive" for p in dev["pins"])


def test_load_mcu_has_full_pin_table():
    """28 脚 MCU 的引脚表必须完整，且包含封装信息。"""
    dev = LIB.load("MCU_Microchip_ATmega", "ATmega328P-P")
    assert len(dev["pins"]) == 28
    assert dev["ref_prefix"] == "U"
    assert dev["footprint"].startswith("Package_DIP")
    numbers = {p["number"] for p in dev["pins"]}
    assert "7" in numbers and "22" in numbers


def test_search_and_to_device_id():
    hits = LIB.search("ATmega328P", limit=5)
    assert hits
    device_id, dev = to_device(*hits[0].split(":", 1))
    assert device_id.startswith("pcbagent:")
    assert dev["pins"]
