"""控制台编码修正测试。

不在当前进程里直接调用 ``ensure_utf8()``：它会 reconfigure pytest 捕获的流，
影响后续用例的输出。统一丢进子进程验证。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def run_py(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )


def test_ensure_utf8_switches_stdout_and_is_idempotent():
    result = run_py(
        "import sys\n"
        "from pcb_agent.console import ensure_utf8\n"
        "ensure_utf8()\n"
        "first = sys.stdout.encoding.lower().replace('-', '')\n"
        "print(first, ensure_utf8())\n"
    )
    assert result.returncode == 0, result.stderr
    encoding, second_call = result.stdout.split()
    assert encoding == "utf8"
    assert second_call == "False", "重复调用应返回 False（幂等）"


def test_ensure_utf8_never_raises_without_console():
    """没有真实控制台（重定向）时也不能抛异常。"""
    result = run_py(
        "from pcb_agent.console import ensure_utf8\n"
        "ensure_utf8()\n"
        "print('alive')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "alive" in result.stdout


def test_box_drawing_chars_survive_utf8_output():
    """框线字符必须先能写进 UTF-8 流，否则界面第一帧就崩在编码上。"""
    result = run_py(
        "from pcb_agent.console import ensure_utf8\n"
        "ensure_utf8()\n"
        "print('┌─┬┐│✓▶!✗')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "┌─┬┐" in result.stdout


def test_enable_vt_handles_missing_console():
    """没有控制台句柄（重定向 / 服务）时 _enable_vt 必须返回 False 而不是抛异常。"""
    result = run_py(
        "from pcb_agent.console import _enable_vt\n"
        "import ctypes\n"
        "print('result:', _enable_vt(ctypes.windll.kernel32))\n"
    )
    assert result.returncode == 0, result.stderr
    # 管道里 stdout 不是控制台，只能拿到 False；关键是没崩
    assert "result: False" in result.stdout


def test_vt_flag_is_turned_on_in_real_console():
    """有真实控制台时必须把 ENABLE_VIRTUAL_TERMINAL_PROCESSING 打开。

    没有控制台就跳过——CI 里跑不出这个条件。
    """
    result = run_py(
        "import ctypes, sys\n"
        "from pcb_agent.console import ensure_utf8\n"
        "if not sys.stdout.isatty():\n"
        "    print('SKIP')\n"
        "    raise SystemExit\n"
        "ensure_utf8()\n"
        "h = ctypes.windll.kernel32.GetStdHandle(-11)\n"
        "m = ctypes.c_uint32()\n"
        "ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(m))\n"
        "print('VT:', bool(m.value & 0x0004))\n"
    )
    assert result.returncode == 0, result.stderr
    if "SKIP" in result.stdout:
        pytest.skip("当前没有真实控制台，跳过 VT 开关验证")
    assert "VT: True" in result.stdout
