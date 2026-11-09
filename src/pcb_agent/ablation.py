"""消融实验跑批：8 条测试需求 × 4 种编排配置，产出可直接进报告的对照表。

四组配置（生成层完全相同，只在编排层做差异，保证可比）：

    A  单 Agent 一次生成          不做需求分析 LLM、不校验、不返修
    B  单 Agent + 规则校验器      校验只报不修
    C  多 Agent（评审无工具）     评审看不到故障清单，只能凭经验改
    D  多 Agent（评审带工具）     本方案，评审吃结构化问题清单

预期结论：B 优于 A；C 不一定优于 B（甚至会把好设计改坏）；D 最优。
这个负结果本身就是报告里要写的失败案例分析。

两种实验模式（--fault）：

    rotate（默认）  生成后按用例轮换注入一处故障（删去耦电容 / 删 I2C 上拉 / 删复位上拉），
                    再交给各级返修 —— 测的是「发现并修好错误」的能力
    none            不注入故障 —— 测的是生成质量（选型准确率、器件/网络数、token 成本）

为什么默认注入故障：本项目生成层走的是子电路模板，天生正确，不注入故障时
A/B/C/D 会全部零错误，实验没有区分度。故障注入把「返修能力」这一变量单独拉出来。

用法：

    python -m pcb_agent.ablation                       # 全量 8 条 × 4 组
    python -m pcb_agent.ablation --cases 1,4 --configs A,B
    python -m pcb_agent.ablation --offline             # 不走网络，冒烟用
    python -m pcb_agent.ablation --out runs/ab --max-rounds 2
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .ir import ERROR, Issue
from .orchestrator import Orchestrator
from .simcheck import check as check_sim
from .validate import counts, run_all
from .patch import apply


# --------------------------------------------------------------------------- #
# 测试需求
# --------------------------------------------------------------------------- #
@dataclass
class Case:
    id: str
    request: str
    expect: list[str]  # 期望的子电路集合，用于统计「选型准确率」

    def match(self, got: list[str]) -> bool:
        return set(got) == set(self.expect)


CASES: list[Case] = [
    Case(
        "C1",
        "帮我设计一块 Arduino 最小系统板，带电源指示灯和一路 I2C 传感器接口，7~12V 供电",
        ["mcu_min_system", "power_ldo5v", "led_indicator", "i2c_sensor"],
    ),
    Case(
        "C2",
        "做一块最简的 ATmega328P 最小系统板，5V 供电，不要指示灯、不要传感器接口",
        ["mcu_min_system", "power_ldo5v"],
    ),
    Case(
        "C3",
        "设计一块数据采集板：MCU 最小系统 + 一路 I2C 传感器接口，外部 9V 适配器供电",
        ["mcu_min_system", "power_ldo5v", "i2c_sensor"],
    ),
    Case(
        "C4",
        "做一个带复位按键和状态指示灯的 MCU 核心板，12V 输入稳压到 5V",
        ["mcu_min_system", "power_ldo5v", "led_indicator"],
    ),
    Case(
        "C5",
        "电源板部分要有 5V 稳压和电源指示灯，再带一颗 ATmega328P 做控制",
        ["mcu_min_system", "power_ldo5v", "led_indicator"],
    ),
    Case(
        "C6",
        "传感器节点板：MCU 最小系统 + I2C 传感器 + 心跳指示灯，7~12V 供电",
        ["mcu_min_system", "power_ldo5v", "led_indicator", "i2c_sensor"],
    ),
    Case(
        "C7",
        "教学用最小系统板，16MHz 晶振、复位按键、每个电源脚都要去耦，5V 供电",
        ["mcu_min_system", "power_ldo5v"],
    ),
    Case(
        "C8",
        "简化控制板：MCU + 一颗 LED 做运行指示 + 5V 稳压输入，不做传感器接口",
        ["mcu_min_system", "power_ldo5v", "led_indicator"],
    ),
]


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    code: str
    name: str
    use_analyst: bool  # 需求分析是否调 LLM（False = 规则兜底，即"单 Agent"）
    repair: str  # none / autofix / blind / tool，返修阶梯
    sim: bool = False  # 仿真结论是否进返修闭环（E 组唯一变量）
    max_rounds: int = 3


# 每一级只比上一级多一个变量，保证差异可归因
CONFIGS: dict[str, Config] = {
    "A": Config("A", "单 Agent 一次生成", use_analyst=False, repair="none"),
    "B": Config("B", "单 Agent + 规则校验器", use_analyst=False, repair="autofix"),
    "C": Config("C", "多 Agent（评审无工具）", use_analyst=True, repair="blind"),
    "D": Config("D", "多 Agent（评审带工具）", use_analyst=True, repair="tool"),
    # E 只比 D 多一个变量：把仿真实测值也当证据交给评审。
    # 结构校验抓不到的「值错误」只有它能修，这是 D 组之前没有优势的原因。
    "E": Config("E", "多 Agent + 仿真闸门", use_analyst=True, repair="tool", sim=True),
}

HALLUCINATION_CODES = {"PIN_NOT_EXIST", "NET_DANGLING_REF", "PIN_MULTIPLE_NETS"}
FAULTS = ("decoupling", "i2c", "reset", "value")


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #
@dataclass
class CaseResult:
    case_id: str
    config: str
    request: str
    title: str = ""
    parts: int = 0
    nets: int = 0
    errors: int = 0
    warnings: int = 0
    rounds: int = 0
    calls: int = 0
    retries: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0
    subcircuits: list[str] = field(default_factory=list)
    selection_ok: bool = False
    hallucinated: int = 0
    error_codes: list[str] = field(default_factory=list)
    fault: str = ""
    sim_errors: int = 0
    sim_codes: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def clean(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["clean"] = self.clean
        return data


def _hallucinations(issues: list[Issue]) -> int:
    return sum(1 for i in issues if i.code in HALLUCINATION_CODES)


# --------------------------------------------------------------------------- #
# 单次运行
# --------------------------------------------------------------------------- #
def _inject_first_applicable(
    orch: Orchestrator, ir, case: Case, fault: str
) -> tuple[object, str, list[str]]:
    """按用例序号轮换注入故障；当前用例不适用时顺延到下一个。"""
    if fault in {"", "none"}:
        return ir, "", []
    order = (
        list(FAULTS)
        if fault == "rotate"
        else [fault]
    )
    start = sum(ord(ch) for ch in case.id) % len(order) if fault == "rotate" else 0
    base_errors = counts(run_all(ir, orch.lib))[0]
    for offset in range(len(order)):
        kind = order[(start + offset) % len(order)]
        new_ir, log = orch.inject_fault(ir, kind)
        if new_ir is ir:
            continue
        # 故障必须真的能被「某一种」检查抓到，否则这个用例没有区分度。
        # 值错误不会被结构校验发现，只能靠仿真，所以两条判据都要看。
        if counts(run_all(new_ir, orch.lib))[0] > base_errors:
            return new_ir, kind, log
        if orch.sim.available:
            report = orch.sim.run(new_ir, Path("runs") / "_probe")
            if report.ok and any(
                i.severity == ERROR for i in check_sim(new_ir, report, orch.lib)
            ):
                return new_ir, kind, log
    return ir, "", ["没有可检测到的故障可注入"]


def run_case(
    orch: Orchestrator, case: Case, cfg: Config, out_dir: Path, fault: str = "rotate"
) -> CaseResult:
    target = out_dir / case.id / cfg.code
    target.mkdir(parents=True, exist_ok=True)
    orch._bind_tracer(target)
    orch.settings.sim_enabled = cfg.sim
    for key in orch.llm.usage:
        orch.llm.usage[key] = 0

    t0 = time.perf_counter()
    result = CaseResult(case_id=case.id, config=cfg.code, request=case.request)

    # 需求分析：单 Agent 配置用规则兜底，不进 LLM
    if cfg.use_analyst:
        spec = orch.analyst.analyze(case.request, round_no=1)
    else:
        spec = orch.analyst._heuristic(case.request)

    ir = orch.designer.design(spec, [], round_no=1, raw_request=case.request)

    log: list[str] = []
    ir, fault_kind, fault_log = _inject_first_applicable(orch, ir, case, fault)
    log.extend(fault_log)
    issues: list[Issue] = []
    rounds = 0
    dirty = False

    while rounds < cfg.max_rounds:
        rounds += 1
        issues, _artifacts = orch._evaluate(ir, rounds, target)
        dirty = False
        errors = [i for i in issues if i.severity == ERROR]

        if cfg.repair == "none":
            break
        # 带工具 / 规则修复都看得出"已经没问题了"，盲评审看不出来，只能继续问
        if cfg.repair in {"autofix", "tool"} and not errors:
            break

        if cfg.repair == "autofix":
            ops = orch.critic._autofix(ir, errors)
        elif cfg.repair == "blind":
            ops = orch.critic.review_blind(ir, round_no=rounds)
        else:
            ops = orch.critic.review(ir, issues, round_no=rounds)
        if not ops:
            break
        res = apply(ir, ops, orch.lib)
        ir = res.ir
        log.extend(res.log)
        dirty = True
        if res.applied == 0:
            log.append(f"第 {rounds} 轮补丁全部被拒")
            break

    if dirty:
        rounds += 1
        issues, _artifacts = orch._evaluate(ir, rounds, target)

    # 无论这组是否把仿真接进闭环，都用同一把尺子量一次，保证可比
    if orch.sim.available:
        report = orch.sim.run(ir, target / "sim")
        if report.ok:
            sim_issues = check_sim(ir, report, orch.lib)
            result.sim_errors = sum(1 for i in sim_issues if i.severity == ERROR)
            result.sim_codes = sorted({i.code for i in sim_issues if i.severity == ERROR})

    # 落盘最终产物，方便事后翻单个用例
    orch._write_final(ir, target, {})

    usage = dict(orch.llm.usage)
    result.title = ir.spec.title
    result.parts = len(ir.bom)
    result.nets = len(ir.nets)
    result.errors = sum(1 for i in issues if i.severity == ERROR)
    result.warnings = sum(1 for i in issues if i.severity != ERROR)
    result.rounds = rounds
    result.calls = usage.get("calls", 0)
    result.retries = usage.get("retries", 0)
    result.tokens = usage.get("total_tokens", 0)
    result.elapsed_s = round(time.perf_counter() - t0, 2)
    result.subcircuits = list(ir.meta.get("subcircuits") or [])
    result.selection_ok = case.match(result.subcircuits)
    result.hallucinated = _hallucinations(issues)
    result.error_codes = sorted({i.code for i in issues if i.severity == ERROR})
    result.fault = fault_kind
    result.note = "; ".join(log[-3:])
    return result


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def aggregate(results: list[CaseResult]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for code, cfg in CONFIGS.items():
        rows = [r for r in results if r.config == code]
        if not rows:
            continue
        n = len(rows)
        summary[code] = {
            "name": cfg.name,
            "cases": n,
            "zero_error_rate": round(sum(1 for r in rows if r.clean) / n, 3),
            "avg_errors": round(sum(r.errors for r in rows) / n, 2),
            "avg_rounds": round(sum(r.rounds for r in rows) / n, 2),
            "avg_calls": round(sum(r.calls for r in rows) / n, 2),
            "avg_tokens": int(sum(r.tokens for r in rows) / n),
            "avg_elapsed_s": round(sum(r.elapsed_s for r in rows) / n, 1),
            "selection_acc": round(sum(1 for r in rows if r.selection_ok) / n, 3),
            "hallucinated_total": sum(r.hallucinated for r in rows),
            "avg_parts": round(sum(r.parts for r in rows) / n, 1),
            "avg_sim_errors": round(sum(r.sim_errors for r in rows) / n, 2),
        }
    return summary


def to_markdown(results: list[CaseResult], summary: dict[str, dict]) -> str:
    lines = [
        "# 消融实验结果",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"用例数：{len({r.case_id for r in results})}，配置数：{len(summary)}",
        "",
        "## 汇总",
        "",
        "| 配置 | 说明 | 零错误率 | 平均错误 | 平均仿真错误 | 平均轮次 | 平均调用 | 平均 tokens | 平均耗时(s) | 选型准确率 | 引脚幻觉 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for code, s in summary.items():
        lines.append(
            f"| {code} | {s['name']} | {s['zero_error_rate']:.0%} | {s['avg_errors']} | "
            f"{s['avg_sim_errors']} | {s['avg_rounds']} | {s['avg_calls']} | {s['avg_tokens']} | "
            f"{s['avg_elapsed_s']} | {s['selection_acc']:.0%} | {s['hallucinated_total']} |"
        )

    lines += [
        "",
        "## 明细",
        "",
        "| 用例 | 配置 | 故障 | 器件 | 网络 | 错误 | 仿真错误 | 轮次 | 调用 | tokens | 耗时(s) | 选型 | 错误码 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.case_id} | {r.config} | {r.fault or '—'} | {r.parts} | {r.nets} | {r.errors} | "
            f"{r.sim_errors} | {r.rounds} | "
            f"{r.calls} | {r.tokens} | {r.elapsed_s} | "
            f"{'✓' if r.selection_ok else '✗'} | {', '.join(r.error_codes) or '—'} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(results: list[CaseResult], summary: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "ablation.md"
    md.write_text(to_markdown(results, summary), encoding="utf-8")

    js = out_dir / "ablation.json"
    js.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "summary": summary,
                "results": [r.to_dict() for r in results],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    csv_path = out_dir / "ablation.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0].to_dict().keys()))
        writer.writeheader()
        for r in results:
            row = r.to_dict()
            row["subcircuits"] = " ".join(row["subcircuits"])
            row["error_codes"] = " ".join(row["error_codes"])
            writer.writerow(row)
    return {"markdown": md, "json": js, "csv": csv_path}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_list(raw: str, total: int) -> list[int]:
    if not raw or raw.lower() == "all":
        return list(range(1, total + 1))
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return [i for i in out if 1 <= i <= total]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-agent-ablation", description="消融实验跑批：8 需求 × 4 配置"
    )
    parser.add_argument("--out", default="", help="输出目录，默认 runs/ablation-<时间戳>")
    parser.add_argument("--cases", default="all", help="用例序号，如 1,3,5 或 1-4 或 all")
    parser.add_argument("--configs", default="A,B,C,D", help="配置组合，如 A,B")
    parser.add_argument("--max-rounds", type=int, default=3, help="单次最多返修轮数")
    parser.add_argument("--offline", action="store_true", help="强制离线（冒烟用）")
    parser.add_argument("--no-kicad", action="store_true", help="关闭 kicad-cli 校验")
    parser.add_argument(
        "--fault",
        default="rotate",
        choices=["rotate", "none", "decoupling", "i2c", "reset", "value"],
        help="故障注入方式，默认 rotate（按用例轮换）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = Settings.load()
    if args.offline:
        settings.offline = True
    if args.no_kicad:
        settings.kicad_enabled = False
    if settings.offline:
        print("警告：离线模式（规则兜底），C/D 两组的差异会被抹平，仅用于冒烟")

    case_idx = _parse_list(args.cases, len(CASES))
    cases = [CASES[i - 1] for i in case_idx]
    codes = [c.strip().upper() for c in args.configs.split(",") if c.strip()]
    unknown = [c for c in codes if c not in CONFIGS]
    if unknown:
        print(f"未知配置：{unknown}", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else Path("runs") / time.strftime("ablation-%Y%m%d-%H%M%S")
    orch = Orchestrator(settings=settings, out_dir=out_dir)
    print(f"LLM：{settings.describe()}")
    print(f"kicad-cli：{'可用' if orch.gate.available else '未安装（跳过 ERC）'}")
    print(f"用例 {len(cases)} × 配置 {len(codes)} = {len(cases) * len(codes)} 次运行 → {out_dir.resolve()}")

    results: list[CaseResult] = []
    total = len(cases) * len(codes)
    n = 0
    for case in cases:
        for code in codes:
            cfg = CONFIGS[code]
            cfg.max_rounds = args.max_rounds
            n += 1
            print(f"[{n}/{total}] {case.id} / {code} {cfg.name} ...", end=" ", flush=True)
            try:
                r = run_case(orch, case, cfg, out_dir, fault=args.fault)
                results.append(r)
                print(
                    f"错误 {r.errors} / 轮次 {r.rounds} / {r.calls} 次调用 / "
                    f"{r.tokens} tokens / {r.elapsed_s}s"
                )
            except Exception as exc:  # 单次失败不能拖垮整批
                print(f"失败：{exc}")
                results.append(
                    CaseResult(
                        case_id=case.id,
                        config=code,
                        request=case.request,
                        note=f"运行失败：{type(exc).__name__}: {exc}",
                    )
                )

    summary = aggregate(results)
    paths = write_outputs(results, summary, out_dir)

    print()
    print(to_markdown(results, summary))
    print("产物：")
    for name, path in paths.items():
        print(f"  {name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
