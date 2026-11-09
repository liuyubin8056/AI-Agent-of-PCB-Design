"""kicad_live 后端：通过 KiCad IPC API 把产物推进正在运行的 KiCad 实例。

和 kicad-cli 后端的差别
-----------------------
``kicad-cli`` 是**批处理**：给一个文件，返回一份报告，看不见 GUI。
``IPC API``（``kicad-python`` / kipy）是**实时通道**：连到运行中的实例，操作它当前打开的文档。
后者适合演示与交互，前者适合自动化与评测，所以本项目两个都留，按需切换。

能力边界（实测 kipy 0.7 + KiCad 10.0.7）
---------------------------------------
- 能：连接、查版本/API 版本、列出已打开的文档、把文件推给 GUI 打开、
      在 PCB 上读写封装/网络/选区（为将来的自动布局布线留口子）。
- 不能：**原理图没有对象模型**。``kipy`` 只暴露 ``Board``（PCB），没有 Schematic，
  也没有 ERC / 网表接口。所以 live 模式下 ERC 与网表仍走 ``kicad-cli`` ——
  这是 KiCad API 的现实边界，不是本项目的偷懒。

结论：live 后端负责「把设计推到人眼前 + PCB 侧可编程」，clu 后端负责「可复现的校验」。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings


@dataclass
class LiveStatus:
    available: bool = False
    connected: bool = False
    version: str = ""
    api_version: str = ""
    schematics: list[str] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    note: str = ""

    def describe(self) -> str:
        if not self.available:
            return f"kicad_live 不可用：{self.note or '未安装 kicad-python'}"
        if not self.connected:
            return f"kicad_live 未连接：{self.note or '请先启动 KiCad 并打开 IPC API'}"
        parts = [f"kicad_live 已连接 KiCad {self.version}（API {self.api_version}）"]
        parts.append(f"原理图 {len(self.schematics)} 个、PCB {len(self.boards)} 个已打开")
        return "，".join(parts)


class KiCadLive:
    """IPC 通道封装。任何失败都优雅降级，绝不让主流程崩。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self._kicad = None
        self._status: LiveStatus | None = None
        self._error = ""

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        """只判断依赖是否装了，不代表 KiCad 正在跑。"""
        try:
            import kipy  # noqa: F401
        except ImportError:
            return False
        return True

    def _connect(self):
        if self._kicad is not None:
            return self._kicad
        from kipy import KiCad

        self._kicad = KiCad()
        return self._kicad

    def status(self, refresh: bool = False) -> LiveStatus:
        if self._status is not None and not refresh:
            return self._status

        st = LiveStatus(available=self.available)
        if not st.available:
            st.note = "未安装 kicad-python（pip install kicad-python）"
            self._status = st
            return st
        try:
            kicad = self._connect()
            version = kicad.get_version()
            api = kicad.get_api_version()
            st.connected = True
            st.version = str(version).split(" (")[0]
            st.api_version = f"{api.major}.{api.minor}.{api.patch}"
            st.schematics = self._open_docs(kicad, "DOCTYPE_SCHEMATIC")
            st.boards = self._open_docs(kicad, "DOCTYPE_PCB")
        except Exception as exc:  # 连接失败 / 版本不匹配 / 未开 API
            self._error = f"{type(exc).__name__}: {str(exc)[:160]}"
            st.connected = False
            st.note = self._error
        self._status = st
        return st

    @staticmethod
    def _open_docs(kicad, doc_type_name: str) -> list[str]:
        """列出已打开文档。不同 kipy 版本枚举路径不同，失败就返回空。"""
        try:
            from kipy.proto.common.types import base_types_pb2 as bt

            doc_type = getattr(bt.DocumentType, doc_type_name)
            docs = kicad.get_open_documents(doc_type)
        except Exception:
            return []
        out: list[str] = []
        for doc in docs:
            path = getattr(doc, "path", "") or getattr(doc, "name", "")
            if path:
                out.append(Path(str(path)).name)
        return out

    # ------------------------------------------------------------------ #
    def open_in_editor(self, path: str | Path, wait: float = 12.0) -> tuple[bool, str]:
        """用系统默认方式打开原理图，再通过 IPC 确认它真的被加载了。"""
        path = Path(path).resolve()
        if not path.exists():
            return False, f"文件不存在：{path}"

        st = self.status()
        if st.connected and path.name in st.schematics:
            return True, f"{path.name} 已在 KiCad 中打开"

        try:
            if os.name == "nt":
                os.startfile(str(path))  # noqa: S606 - 交给系统关联程序
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            return False, f"无法拉起编辑器：{exc}"

        if not st.available:
            return True, f"已请求打开 {path.name}（未装 kicad-python，无法用 IPC 确认）"

        # 轮询确认文档被加载；IPC 未开启也不算失败，只是确认不了
        deadline = time.time() + wait
        while time.time() < deadline:
            current = self.status(refresh=True)
            if not current.connected:
                return True, f"已请求打开 {path.name}（IPC 未连接，无法确认）"
            if path.name in current.schematics:
                return True, f"{path.name} 已在 KiCad 中打开并确认"
            time.sleep(1.0)
        return True, f"已请求打开 {path.name}（等待 {wait:.0f}s 未在 IPC 文档列表中出现）"

    # ------------------------------------------------------------------ #
    def board_summary(self) -> dict[str, int] | None:
        """若当前打开了 PCB，返回封装与网络数量；否则 None。"""
        st = self.status()
        if not st.connected or not st.boards:
            return None
        try:
            board = self._connect().get_board()
            return {
                "footprints": len(board.get_footprints()),
                "tracks": len(board.get_tracks()) if hasattr(board, "get_tracks") else 0,
            }
        except Exception:
            return None

    def select_footprints(self, refs: list[str]) -> int:
        """在 PCB 上选中指定位号（高亮）。原理图侧没有对应 API。"""
        st = self.status()
        if not st.connected or not st.boards or not refs:
            return 0
        try:
            board = self._connect().get_board()
            board.clear_selection()
            wanted = {r.upper() for r in refs}
            hits = [f for f in board.get_footprints() if str(f.reference).upper() in wanted]
            for fp in hits:
                board.add_to_selection(fp)
            return len(hits)
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    def capability_text(self) -> str:
        return "\n".join(
            [
                self.status().describe(),
                "可做：连接实例、列出已打开文档、把原理图推给 GUI、PCB 侧读写封装/选区",
                "不可做：原理图对象模型（kipy 只暴露 Board），ERC 与网表仍走 kicad-cli",
            ]
        )
