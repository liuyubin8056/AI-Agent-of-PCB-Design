"""kicad-cli 网关：把 KiCad 的命令行能力包成 Agent 可调用的工具。

没有安装 KiCad 时全部降级为「不可用」而不是崩溃——这样仓库克隆到任何机器都能跑通
离线 demo，装上 KiCad 后自动获得网表导出、ERC、DRC 与网表一致性比对能力。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .ir import ERROR, WARNING, DesignIR, Issue


# --------------------------------------------------------------------------- #
# 极简 s-expression 解析（用于读 .net 与自检 .kicad_sch）
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse_sexpr(text: str) -> list[Any]:
    """把 s-expression 文本解析成嵌套列表。"""
    tokens = _TOKEN.findall(text)
    stack: list[list[Any]] = [[]]
    for tok in tokens:
        if tok == "(":
            new: list[Any] = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("括号不匹配：多余的右括号")
            stack.pop()
        else:
            if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
                stack[-1].append(tok[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
            else:
                stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("括号不匹配：缺少右括号")
    return stack[0]


def _find_all(node: Any, key: str) -> list[list[Any]]:
    out: list[list[Any]] = []
    if isinstance(node, list):
        if node and node[0] == key:
            out.append(node)
        for child in node:
            out.extend(_find_all(child, key))
    return out


def nets_from_netlist(path: str | Path) -> dict[str, list[str]]:
    """从 KiCad .net 中抽出 {网络名: ["R1.2", "C1.1", ...]}。"""
    tree = parse_sexpr(Path(path).read_text(encoding="utf-8", errors="ignore"))
    nets: dict[str, list[str]] = {}
    for net in _find_all(tree, "net"):
        name = ""
        members: list[str] = []
        for item in net[1:]:
            if isinstance(item, list) and item:
                if item[0] == "name":
                    name = str(item[1])
                elif item[0] == "node":
                    ref = pin = ""
                    for field_ in item[1:]:
                        if isinstance(field_, list) and field_:
                            if field_[0] == "ref":
                                ref = str(field_[1])
                            elif field_[0] == "pin":
                                pin = str(field_[1])
                    if ref and pin:
                        members.append(f"{ref}.{pin}")
        if name:
            nets[name] = members
    return nets


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #
@dataclass
class GateResult:
    available: bool
    ok: bool
    issues: list[Issue] = field(default_factory=list)
    raw: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        if not self.available:
            return "kicad-cli 不可用，已跳过 KiCad 侧校验"
        errors = sum(1 for i in self.issues if i.severity == ERROR)
        return "通过" if self.ok and errors == 0 else f"发现 {errors} 个错误"


class KiCadGate:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        self.cli = self._locate()

    def _locate(self) -> str | None:
        if not self.settings.kicad_enabled or self.settings.kicad_backend == "off":
            return None
        if self.settings.kicad_cli:
            path = Path(self.settings.kicad_cli)
            return str(path) if path.exists() else None
        for name in ("kicad-cli", "kicad-cli.exe"):
            found = shutil.which(name)
            if found:
                return found
        found = self._from_registry()
        if found:
            return found
        # 常见自定义安装位置兜底（KiCad 允许装到任意盘符）
        for base in ("C:/Program Files/KiCad", "C:/Program Files (x86)/KiCad",
                     "D:/KiCad", "E:/KiCad", "F:/KiCad", "F:/KiCard"):
            for ver in ("10.0", "9.0", "8.0"):
                cand = Path(base) / ver / "bin" / "kicad-cli.exe"
                if cand.exists():
                    return str(cand)
            cand = Path(base) / "bin" / "kicad-cli.exe"
            if cand.exists():
                return str(cand)
        return None

    @staticmethod
    def _from_registry() -> str | None:
        """从 Windows 卸载注册表项里找 KiCad 的 InstallLocation。

        KiCad 安装器不写 PATH，但一定会写 DisplayName / InstallLocation，
        所以这是最可靠的自发现方式。
        """
        if os.name != "nt":
            return None
        try:
            import winreg
        except ImportError:  # pragma: no cover
            return None

        def value(key, name: str) -> str:
            try:
                return str(winreg.QueryValueEx(key, name)[0])
            except OSError:
                return ""

        roots = (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        )
        for hive, subpath in roots:
            try:
                with winreg.OpenKey(hive, subpath) as root:
                    count = winreg.QueryInfoKey(root)[0]
                    for idx in range(count):
                        try:
                            sub = winreg.EnumKey(root, idx)
                            with winreg.OpenKey(root, sub) as item:
                                if "kicad" not in value(item, "DisplayName").lower():
                                    continue
                                location = value(item, "InstallLocation")
                        except OSError:
                            continue
                        if not location:
                            continue
                        cand = Path(location) / "bin" / "kicad-cli.exe"
                        if cand.exists():
                            return str(cand)
            except OSError:
                continue
        return None

    @property
    def available(self) -> bool:
        return self.cli is not None

    def _run(self, args: list[str], timeout: int = 180) -> tuple[int, str]:
        assert self.cli
        try:
            proc = subprocess.run(
                [self.cli, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=timeout,
            )
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)

    def version(self) -> str:
        if not self.available:
            return "未安装 / 未找到 kicad-cli"
        code, out = self._run(["--version"], timeout=30)
        return out.strip() if code == 0 else f"无法获取版本：{out.strip()[:80]}"

    # ------------------------------------------------------------------ #
    def export_netlist(self, sch: str | Path, out_net: str | Path) -> GateResult:
        if not self.available:
            return GateResult(False, False, raw="kicad-cli 不可用")
        out_net = Path(out_net)
        out_net.parent.mkdir(parents=True, exist_ok=True)
        code, out = self._run(
            ["sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", str(out_net), str(sch)]
        )
        ok = code == 0 and out_net.exists()
        return GateResult(True, ok, raw=out, artifacts={"netlist": str(out_net)} if ok else {})

    def run_erc(self, sch: str | Path, out_json: str | Path) -> GateResult:
        return self._run_check(
            ["sch", "erc", "--format", "json", "--output", str(out_json), str(sch)],
            out_json,
        )

    def run_drc(self, pcb: str | Path, out_json: str | Path) -> GateResult:
        return self._run_check(
            ["pcb", "drc", "--format", "json", "--output", str(out_json), str(pcb)],
            out_json,
        )

    def _run_check(self, args: list[str], out_path: str | Path) -> GateResult:
        if not self.available:
            return GateResult(False, False, raw="kicad-cli 不可用")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        code, out = self._run(args)
        if not out_path.exists():
            return GateResult(True, False, raw=out, issues=[
                Issue("KICAD_CLI_FAILED", WARNING,
                      f"kicad-cli 未产出报告（退出码 {code}）：{out.strip()[:160]}", "")
            ])
        issues = parse_erc_json(out_path)
        errors = sum(1 for i in issues if i.severity == ERROR)
        return GateResult(True, errors == 0, issues=issues, raw=out,
                          artifacts={"report": str(out_path)})


# 无头 ERC 只读全局符号库表，读不到工程目录里的 sym-lib-table，于是对每个
# 自建符号都报一次「当前配置不包含符号库」。由于符号定义已随原理图内嵌在
# lib_symbols 里（能正常渲染、网表也正确），这条属于环境噪声，不计入问题清单。
_NOISE_TYPES = {"lib_symbol_issues"}


def parse_erc_json(path: str | Path) -> list[Issue]:
    """宽容解析 ERC/DRC 的 JSON 报告：不同小版本字段略有差异。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Issue("KICAD_REPORT_PARSE", WARNING, f"报告解析失败：{exc}", "")]

    issues: list[Issue] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "violations" in node and isinstance(node["violations"], list):
                for v in node["violations"]:
                    if not isinstance(v, dict):
                        continue
                    vtype = str(v.get("type", "CHECK"))
                    if vtype in _NOISE_TYPES:
                        continue
                    sev = str(v.get("severity", "warning")).lower()
                    desc = str(v.get("description") or v.get("message") or vtype)
                    issues.append(
                        Issue(
                            f"KICAD_{vtype.upper()[:24]}",
                            ERROR if sev == "error" else WARNING,
                            desc or "未提供描述",
                            str(node.get("path", "")),
                        )
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return issues


# --------------------------------------------------------------------------- #
# 交叉校验：渲染器生成的原理图 vs IR 网表
# --------------------------------------------------------------------------- #
def diff_with_ir(ir: DesignIR, netlist_path: str | Path) -> list[Issue]:
    """KiCad 从原理图导出的网表，必须与 IR 完全一致，否则说明渲染器有 bug。"""
    try:
        actual = nets_from_netlist(netlist_path)
    except (OSError, ValueError) as exc:
        return [Issue("NETLIST_PARSE", WARNING, f"网表解析失败：{exc}", "")]

    issues: list[Issue] = []
    expected = {net.name: set(net.members) for net in ir.nets}
    # KiCad 会给每个悬空引脚自动生成一条 unconnected-(...) 网络，不算真网络
    actual_sets = {
        name: set(members)
        for name, members in actual.items()
        if not name.startswith("unconnected-")
    }

    for name, members in expected.items():
        if name not in actual_sets:
            issues.append(Issue("NETLIST_NET_MISSING", ERROR,
                                f"原理图中找不到网络 {name}", name))
            continue
        missing = members - actual_sets[name]
        extra = actual_sets[name] - members
        if missing:
            issues.append(Issue("NETLIST_MEMBER_MISSING", ERROR,
                                f"网络 {name} 少了连接点：{', '.join(sorted(missing))}", name))
        if extra:
            issues.append(Issue("NETLIST_MEMBER_EXTRA", ERROR,
                                f"网络 {name} 多了连接点：{', '.join(sorted(extra))}", name))

    for name in actual_sets:
        if name not in expected:
            issues.append(Issue("NETLIST_NET_EXTRA", WARNING,
                                f"原理图多出未预期的网络 {name}", name))
    return issues
