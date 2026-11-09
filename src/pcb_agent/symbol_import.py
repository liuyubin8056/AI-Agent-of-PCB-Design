"""官方符号库导入 CLI：把 KiCad 自带器件灌进我们自己的器件库。

    python -m pcb_agent.symbol_import --libraries
    python -m pcb_agent.symbol_import --search ATmega328
    python -m pcb_agent.symbol_import --import Device:R --import Device:C
    python -m pcb_agent.symbol_import --import MCU_Microchip_ATmega:ATmega328P-P

导入后可以像内置器件一样被子电路模板和渲染器使用（id 形如 ``pcbagent:Device_R``）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DATA_DIR
from .official import OfficialLibrary, to_device


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcb-agent-symbol-import", description="从 KiCad 官方符号库导入器件"
    )
    parser.add_argument("--libraries", action="store_true", help="列出全部官方符号库")
    parser.add_argument("--symbols", default="", help="列出某个库里的符号，如 Device")
    parser.add_argument("--search", default="", help="按符号名搜索，如 ATmega328")
    parser.add_argument("--import", dest="imports", action="append", default=[],
                        help="导入符号，可重复：--import Device:R")
    parser.add_argument("--symbols-file", default="", help="器件库路径，默认 data/symbols.json")
    parser.add_argument("--dry-run", action="store_true", help="只打印解析结果，不写入")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lib = OfficialLibrary()
    if not lib.available:
        print("未找到 KiCad 官方符号库目录，可用 KICAD_SYMBOL_DIR 指定", file=sys.stderr)
        return 2

    if args.libraries:
        libs = lib.libraries()
        print(f"官方符号库 {len(libs)} 个（{lib.root}）：")
        print("  " + " ".join(libs))
        return 0

    if args.symbols:
        names = lib.symbol_names(args.symbols)
        print(f"{args.symbols}: {len(names)} 个符号")
        print("  " + " ".join(names[:80]) + (" ..." if len(names) > 80 else ""))
        return 0

    if args.search:
        hits = lib.search(args.search)
        print(f"命中 {len(hits)} 个：" if hits else "未命中")
        for hit in hits:
            print(f"  {hit}")
        return 0

    if not args.imports:
        build_parser().print_help()
        return 2

    target = Path(args.symbols_file) if args.symbols_file else DATA_DIR / "symbols.json"
    data = _load(target)
    devices = data.setdefault("devices", {})
    aliases = data.setdefault("aliases", {})

    imported = 0
    for spec in args.imports:
        if ":" not in spec:
            print(f"格式应为 库:符号，收到 {spec!r}", file=sys.stderr)
            continue
        library, symbol = spec.split(":", 1)
        try:
            device_id, dev = to_device(library, symbol)
        except (FileNotFoundError, KeyError) as exc:
            print(f"跳过 {spec}：{exc}", file=sys.stderr)
            continue

        pins = dev["pins"]
        print(
            f"{spec} -> {device_id}  引脚 {len(pins)} 个  "
            f"ref={dev['ref_prefix']}  value={dev['value']}"
            + (f"  footprint={dev['footprint']}" if dev["footprint"] else "")
        )
        if args.dry_run:
            continue
        devices[device_id] = dev
        aliases.setdefault(symbol.lower(), device_id)
        imported += 1

    if args.dry_run:
        print("dry-run：未写入")
        return 0
    if imported:
        _save(target, data)
        print(f"已写入 {target}（器件总数 {len(devices)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
