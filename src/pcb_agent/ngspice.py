"""ngspice 薄封装：用 ctypes 直连 KiCad 自带的 libngspice。

为什么不用 ``ngspice.exe``
--------------------------
KiCad 只装 ``ngspice.dll``，不装命令行程序（本机实测确认）。而这个 DLL 与
KiCad 内置仿真器同版本，结果能和 GUI 对得上，比另下一个二进制更可信。

为什么一定要放子进程
--------------------
libngspice 是外来 C 代码，出错时是 access violation 而不是 Python 异常，
会直接杀掉宿主进程。仿真是"不可信第三方"，必须做进程隔离：
``simulate()`` 默认起 ``python -m pcb_agent.ngspice`` 子进程，崩了就崩那个。

踩过的两个坑（都写进代码注释了）
------------------------------
1. ``ngSpice_Circ`` 之前必须调 ``ngSpice_Init``，否则写 0x24 崩溃；
2. ``ngSpice_Init`` 的回调必须全程持有引用。ctypes 的 CFUNCTYPE 对象被 GC
   回收后，ngspice 手里就是野指针，下一次 printf 立即 access violation。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings

DLL_NAMES = ("ngspice.dll", "libngspice-0.dll", "libngspice.so", "libngspice.dylib")


# --------------------------------------------------------------------------- #
# 定位 DLL
# --------------------------------------------------------------------------- #
def find_library(settings: Settings | None = None) -> str | None:
    """按「显式配置 → kicad-cli 同级目录 → PATH → 常见安装位置」找 libngspice。"""
    settings = settings or Settings.load()
    explicit = getattr(settings, "ngspice_dll", "") or ""
    if explicit:
        path = Path(explicit)
        return str(path) if path.exists() else None

    # 与 kicad-cli 同目录：KiCad 的 bin 下就是它
    try:
        from .kicad_gate import KiCadGate

        cli = KiCadGate(settings).cli
        if cli:
            cand = Path(cli).parent / "ngspice.dll"
            if cand.exists():
                return str(cand)
    except Exception:  # noqa: BLE001 - 探测失败不影响后续兜底
        pass

    for name in DLL_NAMES:
        found = shutil.which(name)
        if found:
            return found

    bases = (
        "F:/KiCard", "F:/KiCad", "D:/KiCad", "E:/KiCad",
        "C:/Program Files/KiCad", "C:/Program Files (x86)/KiCad",
    )
    for base in bases:
        for ver in ("10.0", "9.0", "8.0"):
            for rel in (Path(ver) / "bin", Path("bin")):
                cand = Path(base) / rel / "ngspice.dll"
                if cand.exists():
                    return str(cand)
    return None


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class RawResult:
    """一次仿真的原始产出。"""

    ok: bool = False
    values: dict[str, float] = field(default_factory=dict)
    measures: dict[str, float] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    error: str = ""
    version: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "values": self.values,
            "measures": self.measures,
            "log": self.log[-40:],
            "error": self.error,
            "version": self.version,
        }


# --------------------------------------------------------------------------- #
# 进程内会话
# --------------------------------------------------------------------------- #
_SC = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p)
_CE = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p
)
_SD = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
_BG = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool, ctypes.c_int, ctypes.c_void_p)


class Session:
    """一个进程内 ngspice 会话。非线程安全，一次只跑一个电路。"""

    def __init__(self, dll_path: str) -> None:
        self.path = dll_path
        self.log: list[str] = []
        if os.name == "nt":
            try:
                os.add_dll_directory(str(Path(dll_path).parent))
            except (AttributeError, OSError):
                pass
        self.lib = ctypes.WinDLL(dll_path) if os.name == "nt" else ctypes.CDLL(dll_path)
        self._bind()
        # 关键：回调对象必须挂在实例上，否则被 GC 后 ngspice 拿到野指针
        self._callbacks = [
            _SC(self._on_out),
            _SC(lambda *a: 0),
            _CE(lambda *a: 0),
            _SD(lambda *a: 0),
            _SD(lambda *a: 0),
            _BG(lambda *a: 0),
        ]
        self.lib.ngSpice_Init(*self._callbacks, None)

    def _bind(self) -> None:
        self.lib.ngSpice_Init.argtypes = [_SC, _SC, _CE, _SD, _SD, _BG, ctypes.c_void_p]
        self.lib.ngSpice_Init.restype = ctypes.c_int
        self.lib.ngSpice_Command.argtypes = [ctypes.c_char_p]
        self.lib.ngSpice_Command.restype = ctypes.c_int
        self.lib.ngSpice_Circ.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
        self.lib.ngSpice_Circ.restype = ctypes.c_int

    def _on_out(self, msg, ident, _user) -> int:
        if msg:
            self.log.append(msg.decode("utf-8", "ignore").strip())
        return 0

    # -- 基本操作 -------------------------------------------------------- #
    def command(self, text: str) -> int:
        return int(self.lib.ngSpice_Command(text.encode("utf-8")))

    def version(self) -> str:
        """只要 ``ngspice-46`` 这一个短串：完整 banner 有几十行，日志里没法看。"""
        self.log.clear()
        self.command("version")
        for line in self.log:
            found = re.search(r"ngspice-\d+", line)
            if found:
                return found.group(0)
        return "ngspice"

    def load(self, lines: list[str]) -> int:
        """装电路。数组必须以 NULL 结尾，否则 ngspice 越界读。"""
        payload = list(lines) + [None]
        arr = (ctypes.c_char_p * len(payload))(
            *[s.encode("utf-8") if s is not None else None for s in payload]
        )
        return int(self.lib.ngSpice_Circ(arr))

    def run(self) -> int:
        return self.command("run")

    def to_file(self, path: Path, vectors: list[str]) -> int:
        if not vectors:
            return 0  # 只跑 .meas 的 deck 没有向量要写
        if path.exists():
            path.unlink()
        return self.command(f"wrdata {path} {' '.join(vectors)}")

    def reset(self) -> None:
        self.command("destroy all")


# --------------------------------------------------------------------------- #
# wrdata 解析
# --------------------------------------------------------------------------- #
# 注意：TRIG/TARG 形式的测量会把 targ=/trig= 追加在同一行，不能锚定行尾
_MEAS_RE = re.compile(
    r"^\s*(?:stdout|stderr)?\s*([A-Za-z_]\w*)\s*=\s*([-+]?[\d.]+(?:[eE][-+]?\d+)?)"
)


def parse_measures(log: list[str]) -> dict[str, float]:
    """从日志里取 ``.meas`` 结果（ngspice 把测量值打印成 ``name = value``）。"""
    out: dict[str, float] = {}
    for line in log:
        found = _MEAS_RE.match(line)
        if found:
            try:
                out[found.group(1).lower()] = float(found.group(2))
            except ValueError:
                continue
    return out


def parse_wrdata(path: Path, vectors: list[str]) -> dict[str, float]:
    """``wrdata`` 每条向量输出「scale + value」两列，取奇数号列。"""
    if not path.exists():
        return {}
    rows = [ln.split() for ln in path.read_text(errors="ignore").splitlines() if ln.strip()]
    if not rows:
        return {}
    last = rows[-1]
    out: dict[str, float] = {}
    for idx, name in enumerate(vectors):
        col = 2 * idx + 1
        if col < len(last):
            try:
                out[name] = float(last[col])
            except ValueError:
                pass
    return out


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def simulate(
    deck: str,
    vectors: list[str],
    workdir: str | Path,
    settings: Settings | None = None,
    timeout: int = 90,
    inproc: bool = False,
) -> RawResult:
    """跑一份 deck，返回全部向量的终值。

    默认起子进程（崩溃隔离）；``inproc=True`` 用于调试，崩溃会带走调用方。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    deck_path = workdir / "deck.cir"
    deck_path.write_text(deck, encoding="utf-8")

    settings = settings or Settings.load()
    dll = find_library(settings)
    if not dll:
        return RawResult(ok=False, error="未找到 ngspice 动态库（装 KiCad 即可）")

    if inproc:
        return _run_here(dll, deck, vectors, workdir)

    src = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, "-m", "pcb_agent.ngspice",
        "--deck", str(deck_path),
        "--out", str(workdir),
        "--vectors", ",".join(vectors),
        "--dll", dll,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, encoding="utf-8",
            errors="ignore",
        )
    except subprocess.TimeoutExpired:
        return RawResult(ok=False, error=f"仿真超时（{timeout}s）")

    payload = (proc.stdout or "").strip().splitlines()
    for line in reversed(payload):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            return RawResult(
                ok=bool(data.get("ok")),
                values={k: float(v) for k, v in (data.get("values") or {}).items()},
                measures={k: float(v) for k, v in (data.get("measures") or {}).items()},
                log=list(data.get("log") or []),
                error=str(data.get("error") or ""),
                version=str(data.get("version") or ""),
            )
    tail = (proc.stderr or "").strip().splitlines()[-3:]
    return RawResult(
        ok=False,
        error=f"仿真子进程异常退出（code={proc.returncode}）：{' / '.join(tail)}",
    )


def _run_here(dll: str, deck: str, vectors: list[str], workdir: Path) -> RawResult:
    session = Session(dll)
    result = RawResult(version=session.version())
    lines = [ln for ln in deck.splitlines() if ln.strip()]
    if session.load(lines) != 0:
        result.error = "电路装载失败"
        result.log = session.log[-20:]
        return result
    if session.run() != 0:
        result.error = "仿真运行失败"
        result.log = session.log[-20:]
        return result
    out = workdir / "vectors.txt"
    session.to_file(out, vectors)
    result.values = parse_wrdata(out, vectors)
    result.measures = parse_measures(session.log)
    result.log = session.log[-20:]
    result.ok = bool(result.values or result.measures)
    if not result.ok:
        result.error = "未取到任何结果，检查 deck 是否包含分析语句"
    session.reset()
    return result


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ngspice 子进程 worker（一般不直接调用）")
    parser.add_argument("--deck", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--vectors", default="")
    parser.add_argument("--dll", default="")
    args = parser.parse_args()

    workdir = Path(args.out)
    deck = Path(args.deck).read_text(encoding="utf-8")
    vectors = [v for v in args.vectors.split(",") if v]
    dll = args.dll or find_library()
    if not dll:
        print(json.dumps({"ok": False, "error": "未找到 ngspice 动态库"}))
        return 0
    res = _run_here(dll, deck, vectors, workdir)
    print(json.dumps(res.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
