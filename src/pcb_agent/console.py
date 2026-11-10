"""Windows 控制台编码修正。

Python 在中文 Windows 上默认用 GBK 编码 stdout，后果有两层：

1. **写不进去**：界面里的 ``─`` ``│`` ``✓`` 这些框线字符不在 GBK 里，
   ``print`` 会直接抛 ``UnicodeEncodeError``，界面第一个字节就崩；
2. **写进去也显示错**：即使 Python 用 UTF-8 输出，控制台仍按 GBK 解释，
   中文变成乱码，边框错位。

所以两件事都要做：把 Python 侧的流换成 UTF-8，再把控制台代码页切成 65001。
"""

from __future__ import annotations

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
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            changed = True
        except Exception:  # noqa: BLE001 - 没有控制台（如 pythonw）时正常失败
            pass

    _APPLIED = True
    return changed
