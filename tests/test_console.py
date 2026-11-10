"""控制台编码修正测试。

不在当前进程里直接调用 ``ensure_utf8()``：它会 reconfigure pytest 捕获的流，
影响后续用例的输出。统一丢进子进程验证。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
