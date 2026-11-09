"""编排器：把四个节点串成一个「设计 - 校验 - 评审 - 返修」闭环。

    analyst -> designer -> [render + validate + kicad] -> critic -> patch -> ...
                                    ^                      |
                                    +----------------------+

这里刻意用纯 Python 写状态机（零依赖、可读、好调试），
``graph_langgraph.py`` 提供等价的 LangGraph 版本，用于对比与答辩演示。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .agents import AnalystAgent, CriticAgent, DesignerAgent
from .config import Settings
from .ir import ERROR, BoardSpec, DesignIR, Issue
from .kicad_gate import KiCadGate, diff_with_ir
from .kicad_live import KiCadLive
from .pcb import build_board as build_pcb_board
from .pcb import write_pcb
from .router import route_board
from .simcheck import check as check_sim
from .spice import format_value, parse_value
from .simulate import SimReport, Simulator
from .library import Library
from .llm import LLMClient, LLMError
from .patch import PatchOp, apply, ops_from_text
from .render import write_bom_csv, write_schematic
from .trace import Tracer
from .validate import counts, report_text, run_all


@dataclass
class RunResult:
    request: str
    spec: BoardSpec
    ir: DesignIR
    issues: list[Issue]
    rounds: int
    artifacts: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    mode: str = ""
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> int:
        return counts(self.issues)[0]

    @property
    def warnings(self) -> int:
        return counts(self.issues)[1]

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def describe(self) -> str:
        lines = [
            f"需求：{self.request}",
            f"板卡：{self.spec.title}",
            f"结果：{self.ir.summary_line()}",
            f"闭环：{self.rounds} 轮，{self.errors} 个错误 / {self.warnings} 个警告",
            f"模式：{self.mode}",
        ]
        if self.usage.get("calls"):
            lines.append(
                f"用量：{self.usage['calls']} 次调用 / "
                f"{self.usage['total_tokens']} tokens"
                f"（输入 {self.usage['prompt_tokens']}，输出 {self.usage['completion_tokens']}）"
            )
        for name, path in self.artifacts.items():
            lines.append(f"  {name}: {path}")
        if self.log:
            lines.append("过程：")
            lines.extend(f"  - {x}" for x in self.log)
        return "\n".join(lines)


class Orchestrator:
    def __init__(
        self,
        settings: Settings | None = None,
        out_dir: str | Path = "runs",
        lib: Library | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self.out_dir = Path(out_dir)
        self.lib = lib or Library()
        self.llm = LLMClient(self.settings)
        self.gate = KiCadGate(self.settings)
        self.live = KiCadLive(self.settings)
        self.sim = Simulator(self.settings, self.lib)
        self.last_sim: SimReport | None = None
        self.tracer: Tracer | None = None
        self.analyst = AnalystAgent(llm=self.llm, lib=self.lib)
        self.designer = DesignerAgent(llm=self.llm, lib=self.lib)
        self.critic = CriticAgent(llm=self.llm, lib=self.lib)

    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> str:
        return "LLM" if self.llm.available else "离线规则兜底"

    def _bind_tracer(self, out_dir: Path) -> None:
        self.tracer = Tracer(out_dir)
        self.analyst.tracer = self.tracer
        self.designer.tracer = self.tracer
        self.critic.tracer = self.tracer

    # ------------------------------------------------------------------ #
    def design(
        self,
        request: str,
        out_dir: str | Path | None = None,
        inject_fault: str = "",
    ) -> RunResult:
        target = Path(out_dir) if out_dir else self.out_dir
        target.mkdir(parents=True, exist_ok=True)
        self._bind_tracer(target)

        t0 = time.perf_counter()
        spec = self.analyst.analyze(request, round_no=1)
        ir = self.designer.design(spec, [], round_no=1, raw_request=request)

        log: list[str] = []
        if inject_fault:
            ir, fault_log = self.inject_fault(ir, inject_fault)
            log.extend(fault_log)
            if self.tracer:
                self.tracer.event("inject_fault", fault=inject_fault, log=fault_log)

        ir, issues, artifacts, rounds = self._repair(ir, target, log, start_round=1)

        final = self._write_final(ir, target, artifacts)
        self._publish_live(final, log)
        elapsed = round(time.perf_counter() - t0, 2)

        result = RunResult(
            request=request,
            spec=spec,
            ir=ir,
            issues=issues,
            rounds=rounds,
            artifacts=final,
            log=log,
            mode=self.mode,
            usage=dict(self.llm.usage),
        )
        self._record(result, elapsed, target)
        return result

    # ------------------------------------------------------------------ #
    def _repair(
        self,
        ir: DesignIR,
        target: Path,
        log: list[str],
        start_round: int = 1,
    ) -> tuple[DesignIR, list[Issue], dict[str, str], int]:
        """「评估 - 评审 - 应用补丁」闭环，不超过 max_rounds 轮。

        design 与 revise 共用，保证「追问引入的新错误」也会被修掉。
        """
        rounds = start_round - 1
        issues: list[Issue] = []
        artifacts: dict[str, str] = {}
        dirty = False

        while rounds - start_round + 1 < self.settings.max_rounds:
            rounds += 1
            issues, artifacts = self._evaluate(ir, rounds, target)
            dirty = False
            if not any(i.severity == ERROR for i in issues):
                break
            ops = self.critic.review(ir, issues, round_no=rounds)
            if not ops:
                log.append(f"第 {rounds} 轮评审未能给出可执行补丁，提前结束")
                break
            res = apply(ir, ops, self.lib)
            ir = res.ir
            log.extend(res.log)
            log.extend(self.critic.unfixable)
            dirty = True
            if res.applied == 0:
                log.append(f"第 {rounds} 轮补丁全部被拒（{res.rejected} 条），提前结束")
                break

        if dirty:
            # 最后一轮应用过补丁但还没评估，补一次以拿到真实状态
            rounds += 1
            issues, artifacts = self._evaluate(ir, rounds, target)
        return ir, issues, artifacts, rounds

    # ------------------------------------------------------------------ #
    def revise(
        self,
        ir: DesignIR,
        request: str,
        out_dir: str | Path | None = None,
    ) -> RunResult:
        """用户追问改需求：产出补丁 -> 应用 -> 重新校验，不重跑整条流水线。"""
        target = Path(out_dir) if out_dir else self.out_dir
        target.mkdir(parents=True, exist_ok=True)
        if self.tracer is None:
            self._bind_tracer(target)

        ops = self._ops_for_revise(ir, request)
        if not ops:
            issues, artifacts = self._evaluate(ir, 1, target)
            return RunResult(request, ir.spec, ir, issues, 0, artifacts,
                             ["未能从修改要求中解析出任何操作"], self.mode)

        patch_res = apply(ir, ops, self.lib)
        new_ir, patch_log = patch_res.ir, patch_res.log
        new_ir.meta["revisions"] = list(ir.meta.get("revisions") or []) + [request]
        if patch_res.applied == 0:
            patch_log.append(f"补丁全部被拒（{patch_res.rejected} 条），设计未改动")
        # 追问可能引入新错误（例如只加器件不接线），所以修订后同样要走返修闭环
        new_ir, issues, artifacts, rounds = self._repair(
            new_ir, target, patch_log, start_round=1
        )
        final = self._write_final(new_ir, target, artifacts)
        self._publish_live(final, patch_log)
        result = RunResult(
            request=request,
            spec=new_ir.spec,
            ir=new_ir,
            issues=issues,
            rounds=rounds,
            artifacts=final,
            log=patch_log,
            mode=self.mode,
            usage=dict(self.llm.usage),
        )
        if self.tracer:
            self.tracer.event("revise", request=request, ops=[o.to_dict() for o in ops])
        self._record(result, 0.0, target)
        return result

    def _ops_for_revise(self, ir: DesignIR, request: str) -> list[PatchOp]:
        if not self.offline_safe():
            try:
                bom_text = "\n".join(
                    f"- {i.ref} | {i.value} | {i.symbol}" for i in ir.bom
                )
                data = self.llm.complete_json(
                    prompts.REVISE_SYSTEM,
                    prompts.revise_user(request, ir.summary_line(), bom_text),
                )
                ops = [PatchOp.from_dict(x) for x in data.get("patches") or [] if isinstance(x, dict)]
                if ops:
                    return ops
            except (LLMError, ValueError, TypeError):
                pass
        return ops_from_text(request, ir)

    def offline_safe(self) -> bool:
        return self.llm.available

    # ------------------------------------------------------------------ #
    def _led_resistor(self, ir: DesignIR) -> str:
        """找 LED 限流电阻：值错误故障要挑一个仿真判得出来的器件。"""
        for item in ir.bom:
            if self.lib.category(item.symbol) != "indicator":
                continue
            for net in ir.nets:
                if f"{item.ref}.2" not in net.members:
                    continue
                for member in net.members:
                    ref = member.split(".")[0]
                    other = ir.part(ref)
                    if other and ref.startswith("R"):
                        return ref
        return ""

    def inject_fault(self, ir: DesignIR, kind: str) -> tuple[DesignIR, list[str]]:
        """故意破坏一个器件，用来演示「校验 - 评审 - 返修」闭环真的在起作用。

        这不是测试桩，而是答辩演示的刚需：一份天生正确的设计看不出 Agent 的能力。
        """
        kind = (kind or "").lower()
        target = ""
        if kind in {"decoupling", "cap"}:
            for net in ir.nets:
                if net.kind != "power":
                    continue
                for member in net.members:
                    ref = member.split(".")[0]
                    item = ir.part(ref)
                    if item and item.value == "100nF" and ref.startswith("C"):
                        target = ref
                        break
                if target:
                    break
        elif kind in {"i2c", "pullup"}:
            for net in ir.nets:
                if any(h in net.name.upper() for h in ("SDA", "SCL")):
                    for member in net.members:
                        ref = member.split(".")[0]
                        item = ir.part(ref)
                        if item and ref.startswith("R"):
                            target = ref
                            break
                    if target:
                        break
        elif kind in {"value", "spec"}:
            # 值错误：连接完全正确，只有电阻值离谱。ERC / DRC / 规则校验全绿，
            # 只有仿真能抓——这是证明「仿真闸门」有存在价值的对照组。
            target = self._led_resistor(ir) or next(
                (
                    i.ref
                    for i in ir.bom
                    if i.symbol.endswith(":RES") and i.ref.startswith("R")
                ),
                "",
            )
            item = ir.part(target) if target else None
            ohms = parse_value(item.value) if item else None
            if item and ohms:
                new_ir, log = apply(
                    ir,
                    [PatchOp("set_value", ref=target, value=format_value(ohms / 20))],
                    self.lib,
                )
                return new_ir, [f"注入故障：{target} 改小 20 倍（值错误，连接不变）"] + log
            return ir, ["未找到可改值的电阻"]
        elif kind == "reset":
            for net in ir.nets:
                if "RESET" in net.name.upper():
                    for member in net.members:
                        ref = member.split(".")[0]
                        if ref.startswith("R"):
                            target = ref
                            break
                    if target:
                        break

        if not target:
            return ir, [f"未找到可注入故障的器件（{kind}）"]
        new_ir, log = apply(ir, [PatchOp("remove_part", ref=target)], self.lib)
        return new_ir, [f"注入故障：删除 {target}"] + log

    # ------------------------------------------------------------------ #
    def _evaluate(
        self, ir: DesignIR, round_no: int, out_dir: Path
    ) -> tuple[list[Issue], dict[str, str]]:
        rdir = out_dir / f"round-{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)

        sch = write_schematic(ir, rdir / "board.kicad_sch", self.lib)
        bom = write_bom_csv(ir, rdir / "bom.csv")
        design = ir.to_json(rdir / "design.json")

        artifacts = {
            "原理图": str(sch),
            "BOM": str(bom),
            "设计 JSON": str(design),
        }

        issues = run_all(ir, self.lib)

        if self.gate.available:
            net = rdir / "board.net"
            res = self.gate.export_netlist(sch, net)
            if res.ok:
                artifacts["网表"] = str(net)
                issues.extend(diff_with_ir(ir, net))
                issues.sort(key=lambda i: 0 if i.severity == ERROR else 1)
            erc = self.gate.run_erc(sch, rdir / "erc.json")
            if erc.available:
                issues.extend(erc.issues)
                if erc.artifacts.get("report"):
                    artifacts["ERC 报告"] = erc.artifacts["report"]

        # 电气行为断言：结构校验抓不到的「值错误」只有仿真能抓
        if self.settings.sim_enabled and self.sim.available:
            report = self.sim.run(ir, rdir)
            self.last_sim = report
            if report.ok:
                artifacts["仿真结果"] = str(rdir / "sim.json")
            issues.extend(check_sim(ir, report, self.lib))
            if self.tracer:
                self.tracer.event(
                    "sim", round=round_no, ok=report.ok,
                    version=report.version, elapsed=report.elapsed,
                    unmodeled=len(report.unmodeled),
                )

        errors, warnings = counts(issues)
        if self.tracer:
            self.tracer.event("issues", round=round_no, errors=errors, warnings=warnings)
            self.tracer.event(
                "artifact", round=round_no, name="round-dir", path=str(rdir)
            )
        return issues, artifacts

    def _write_final(
        self, ir: DesignIR, out_dir: Path, artifacts: dict[str, str]
    ) -> dict[str, str]:
        final = {
            "原理图": write_schematic(ir, out_dir / "board.kicad_sch", self.lib),
            "BOM": write_bom_csv(ir, out_dir / "bom.csv"),
            "设计 JSON": ir.to_json(out_dir / "design.json"),
        }
        out = {k: str(v) for k, v in final.items()}
        for key, path in artifacts.items():
            out.setdefault(key, path)
        return out

    def export_pcb(
        self, ir: DesignIR, out_dir: str | Path, route: bool = True
    ) -> dict[str, str]:
        """生成 .kicad_pcb，可选自动布线与 DRC，返回产物与指标。"""
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        try:
            board = build_pcb_board(ir)
        except (RuntimeError, FileNotFoundError) as exc:
            return {"错误": f"无法生成 PCB：{exc}"}

        info: dict[str, str] = {}
        if route:
            stats = route_board(board)
            info["布通率"] = stats.describe()
        path = write_pcb(board, target / "board.kicad_pcb")
        info["PCB"] = str(path)
        info["板尺寸"] = f"{board.width:.1f} x {board.height:.1f} mm"

        if self.gate.available:
            res = self.gate.run_drc(path, target / "pcb_drc.json")
            errors = sum(1 for i in res.issues if i.severity == "error")
            warnings = len(res.issues) - errors
            info["PCB DRC"] = f"{errors} 个错误 / {warnings} 个警告"
            if res.artifacts.get("report"):
                info["DRC 报告"] = res.artifacts["report"]
        return info

    def _publish_live(self, paths: dict[str, str], log: list[str]) -> None:
        """live 后端：把最终原理图推进正在运行的 KiCad，并把 IPC 状态记进日志。

        原理图在 KiCad 10 + kipy 0.7 下没有对象模型，所以这里只做"推给 GUI"，
        校验仍由 kicad-cli 负责。失败也只是记一行日志，不影响交付物。
        """
        if self.settings.kicad_backend != "live":
            return
        sch = paths.get("原理图")
        if not sch:
            return
        ok, message = self.live.open_in_editor(sch)
        log.append(f"kicad_live：{message}")
        if self.tracer:
            self.tracer.event("live_publish", ok=ok, message=message)

    def _record(self, result: RunResult, elapsed: float, out_dir: Path) -> None:
        summary = {
            "request": result.request,
            "title": result.spec.title,
            "mode": result.mode,
            "rounds": result.rounds,
            "errors": result.errors,
            "warnings": result.warnings,
            "parts": len(result.ir.bom),
            "nets": len(result.ir.nets),
            "elapsed_s": elapsed,
            "kicad_cli": self.gate.available,
            "kicad_backend": self.settings.kicad_backend,
            "live_status": self.live.status().describe()
            if self.settings.kicad_backend == "live"
            else "",
            "llm_usage": dict(self.llm.usage),
            "issues": [i.to_dict() for i in result.issues],
            "log": result.log,
        }
        (out_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "report.txt").write_text(
            "\n".join([result.describe(), "", report_text(result.issues)]) + "\n",
            encoding="utf-8",
        )
