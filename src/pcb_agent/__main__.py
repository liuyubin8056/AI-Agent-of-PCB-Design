"""命令行入口。

    python -m pcb_agent --demo                    # 零配置跑通全流程
    python -m pcb_agent "帮我设计一块 Arduino 最小系统板"
    python -m pcb_agent --demo --revise "把C3换成100nF"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Settings
from .orchestrator import Orchestrator, RunResult
from .validate import report_text

DEMO_REQUEST = "帮我设计一块 Arduino 最小系统板，带电源指示灯和一路 I2C 传感器接口，7~12V 供电"


def _default_out() -> Path:
    return Path("runs") / time.strftime("%Y%m%d-%H%M%S")


def _print_result(result: RunResult, show_issues: bool = True) -> None:
    print()
    print("=" * 68)
    print(result.describe())
    if show_issues:
        print()
        print(report_text(result.issues))
    print("=" * 68)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-agent",
        description="自然语言 -> BOM + 网表 + KiCad 8 原理图",
    )
    parser.add_argument("request", nargs="?", help="一句话设计需求")
    parser.add_argument("--demo", action="store_true", help="使用内置示例需求")
    parser.add_argument("--out", default="", help="输出目录，默认 runs/<时间戳>")
    parser.add_argument("--revise", default="", help="对生成结果追加一次修改，如「把C3换成100nF」")
    parser.add_argument("--from-json", default="", help="从已有的 design.json 继续修改")
    parser.add_argument(
        "--inject-fault",
        default="",
        choices=["", "decoupling", "i2c", "reset", "value"],
        help="演示用：故意破坏设计（value=只改值不改连接，只有仿真能抓）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 形式输出摘要")
    parser.add_argument("--quiet", action="store_true", help="只输出摘要，不打印问题清单")
    parser.add_argument(
        "--sim",
        action="store_true",
        help="打印工作点仿真明细（仿真本身默认开启，PCB_SIM=off 可关）",
    )
    parser.add_argument(
        "--pcb",
        action="store_true",
        help="顺带生成 .kicad_pcb 并自动布线（含 DRC）",
    )
    parser.add_argument(
        "--tui",
        choices=["auto", "on", "off"],
        default="auto",
        help="终端界面：auto=有 TTY 才开（默认），on=强制，off=纯文本",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="用 KiCad IPC 后端：把原理图推进正在运行的 KiCad（需开启 IPC API）",
    )
    return parser


def _run_once(args, orch: Orchestrator, out_dir: Path) -> RunResult:
    """真正跑一次设计/修订。界面与纯文本两条路径共用。"""
    if args.from_json:
        from .ir import DesignIR

        ir = DesignIR.load_json(args.from_json)
        request = args.revise or "载入已有设计"
        return orch.revise(ir, request, out_dir)

    request = args.request or (DEMO_REQUEST if args.demo else "")
    if not request:
        raise SystemExit("请给出一句话需求，或使用 --demo")
    result = orch.design(request, out_dir, inject_fault=args.inject_fault)
    if args.revise:
        result = orch.revise(result.ir, args.revise, out_dir / "revision")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    if args.live:
        settings.kicad_backend = "live"
    out_dir = Path(args.out) if args.out else _default_out()

    orch = Orchestrator(settings=settings, out_dir=out_dir)

    from .console import ensure_utf8
    from .ui.tui import Tui
    from .ui.tui import supported as tui_supported

    ensure_utf8()

    use_tui = False
    if args.tui == "on":
        # 没有真终端时交给 alt-screen 只会污染管道输出，宁可降级
        if sys.stdout.isatty():
            use_tui = True
        else:
            print("提示：当前不是交互终端，--tui on 已降级为文本输出", file=sys.stderr)
    elif args.tui == "auto":
        use_tui = tui_supported()
    if args.json:  # JSON 输出必须保持可解析，不能被界面接管
        use_tui = False

    subtitle = (
        f"{settings.describe()}  ·  kicad {settings.kicad_backend}"
        f"（{'可用' if orch.gate.available else '未装'}）"
        f"  ·  仿真 {'可用' if orch.sim.available else '未装'}"
        f"  ·  {out_dir}"
    )
    tui = None
    if use_tui:
        out_dir.mkdir(parents=True, exist_ok=True)
        tui = Tui(
            title="PCB-Agent 电路板设计",
            subtitle=subtitle,
            max_rounds=settings.max_rounds,
            snapshot_path=str(out_dir / "ui-frame.txt"),
        )
        orch.on_event(tui.feed)
        tui.start()
    else:
        print(f"LLM 后端：{settings.describe()}")
        print(f"kicad-cli：{'可用' if orch.gate.available else '未安装（跳过网表/ERC 校验）'}")
        print(f"KiCad 后端：{settings.kicad_backend}")
        if settings.kicad_backend == "live":
            print(f"  {orch.live.status().describe()}")
        print(f"输出目录：{out_dir.resolve()}")

    try:
        result = _run_once(args, orch, out_dir)
    finally:
        if tui is not None:
            tui.stop()


    if args.sim and orch.last_sim is not None:
        from .simulate import summarize

        print()
        print("工作点仿真")
        print(summarize(orch.last_sim))

    pcb_info: dict[str, str] = {}
    if args.pcb:
        print()
        print("生成 PCB 并自动布线…")
        pcb_info = orch.export_pcb(result.ir, out_dir)
        for key, value in pcb_info.items():
            print(f"  {key}：{value}")

    if args.json:
        payload = {
            "title": result.spec.title,
            "parts": len(result.ir.bom),
            "nets": len(result.ir.nets),
            "rounds": result.rounds,
            "errors": result.errors,
            "warnings": result.warnings,
            "artifacts": {k: str(v) for k, v in result.artifacts.items()},
            "pcb": pcb_info,
            "sim": orch.last_sim.to_dict() if orch.last_sim else {},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_result(result, show_issues=not args.quiet)

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
