"""Windows 控制台编码修正。

Python 在中文 Windows 上默认用 GBK 编码 stdout，后果有两层：

1. **写不进去**：界面里的 ``─`` ``│`` ``✓`` 这些框线字符不在 GBK 里，
   ``print`` 会直接抛 ``UnicodeEncodeError``，界面第一个字节就崩；
2. **写进去也显示错**：即使 Python 用 UTF-8 输出，控制台仍按 GBK 解释，
   中文变成乱码，边框错位。

所以两件事都要做：把 Python 侧的流换成 UTF-8，再把控制台代码页切成 65001。
"""

from __future__ import annotations

import ctypes
import os
import sys

_APPLIED = False


def ensure_utf8() -> bool:
    """把 stdout/stderr 切成 UTF-8，并把 Windows 控制台代码页设为 65001。

    幂等，重复调用无副作用。返回是否真的做了调整。
    拿不到真实终端（重定向到文件、pytest 捕获）时也安全：
    ``reconfigure`` 失败会被吞掉，不影响流程。
    """
    global _APPLIED
    if _APPLIED:
        return False

    changed = False
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            # errors="replace"：万一还有边角字符写不出去，宁可显示成问号，
            # 也不能让整个界面崩在编码上
            reconfigure(encoding="utf-8", errors="replace")
            changed = True
        except (ValueError, OSError):
            pass

    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            _enable_vt(kernel32)
            changed = True
        except Exception:  # noqa: BLE001 - 没有控制台（如 pythonw）时正常失败
            pass

    _APPLIED = True
    return changed


# Windows 控制台的 ANSI 转义默认是关的（老 conhost）。不打开的话，
# 界面里的 \x1b[36m 会原样显示成 "←[36m"，比乱码更难读。
_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _enable_vt(kernel32) -> bool:
    """打开 stdout 的 VT 处理。已经是开的就返回 False。

    显式声明参数/返回类型：句柄在 64 位下是 8 字节，按 ctypes 默认的
    ``c_int`` 取会被截断，SetConsoleMode 就会作用到错误的句柄上。
    """
    kernel32.GetStdHandle.argtypes = [ctypes.c_uint32]
    kernel32.GetStdHandle.restype = ctypes.c_void_p
    kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetConsoleMode.restype = ctypes.c_int
    kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetConsoleMode.restype = ctypes.c_int

    handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return False
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    if mode.value & _ENABLE_VIRTUAL_TERMINAL_PROCESSING:
        return False
    return bool(
        kernel32.SetConsoleMode(
            handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING
        )
    )
