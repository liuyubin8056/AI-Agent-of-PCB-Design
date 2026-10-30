"""器件库：引脚白名单的唯一来源。

两个关键用途
------------
1. ``pin_table_text()``：把某器件的真实引脚表注入 prompt，
   让模型「从枚举里选」而不是「凭记忆编」，这是抑制引脚幻觉的第一道防线；
2. ``pins()``：校验器用它判定「这个引脚到底存不存在」。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import DATA_DIR


class Library:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DATA_DIR / "symbols.json"
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.devices: dict[str, dict[str, Any]] = data.get("devices", {})
        self.aliases: dict[str, str] = {
            k.lower(): v for k, v in (data.get("aliases") or {}).items()
        }
        self._pin_index: dict[str, dict[str, dict[str, Any]]] = {}
        for sid, dev in self.devices.items():
            self._pin_index[sid] = {p["number"]: p for p in dev.get("pins", [])}

    # ------------------------------------------------------------------ #
    def resolve(self, sid: str) -> str:
        """把别名 / 短名解析成规范的 lib_id。"""
        key = sid.strip()
        if key in self.devices:
            return key
        low = key.lower()
        if low in self.aliases:
            return self.aliases[low]
        for cand in self.devices:
            if cand.split(":")[-1].lower() == low:
                return cand
        raise KeyError(f"器件库中找不到 {sid!r}")

    def has(self, sid: str) -> bool:
        try:
            self.resolve(sid)
            return True
        except KeyError:
            return False

    def get(self, sid: str) -> dict[str, Any]:
        return self.devices[self.resolve(sid)]

    def pins(self, sid: str) -> list[dict[str, Any]]:
        return list(self.get(sid).get("pins", []))

    def pin(self, sid: str, number: str) -> dict[str, Any] | None:
        return self._pin_index[self.resolve(sid)].get(str(number))

    def pin_exists(self, sid: str, number: str) -> bool:
        return self.pin(sid, number) is not None

    def pin_names(self, sid: str) -> list[str]:
        return [p["name"] for p in self.pins(sid)]

    def find_pins_by_name(self, sid: str, needle: str) -> list[dict[str, Any]]:
        """按引脚名做子串匹配，用于「找出 RESET / VCC / SDA 这类引脚」。"""
        needle = needle.upper()
        return [p for p in self.pins(sid) if needle in p["name"].upper()]

    def ref_prefix(self, sid: str) -> str:
        return self.get(sid).get("ref_prefix", "U")

    def default_value(self, sid: str) -> str:
        return self.get(sid).get("value", "")

    def footprint(self, sid: str) -> str:
        return self.get(sid).get("footprint", "")

    def category(self, sid: str) -> str:
        return self.get(sid).get("category", "")

    def desc(self, sid: str) -> str:
        return self.get(sid).get("desc", "")

    # ------------------------------------------------------------------ #
    def search(self, keyword: str) -> list[str]:
        """极简检索：命中断词或关键词即返回。生产版可换成向量检索。"""
        keyword = (keyword or "").lower()
        hits: list[str] = []
        for sid, dev in self.devices.items():
            blob = " ".join(
                [sid.lower(), dev.get("desc", ""), " ".join(dev.get("keywords", []))]
            )
            if keyword and keyword in blob:
                hits.append(sid)
        return hits

    def by_category(self, category: str) -> list[str]:
        return [sid for sid, dev in self.devices.items() if dev.get("category") == category]

    # ------------------------------------------------------------------ #
    def catalog_text(self) -> str:
        """给 prompt 用的器件目录（只列 id / 值 / 一句话说明）。"""
        lines = []
        for sid, dev in self.devices.items():
            if dev.get("category") == "virtual":
                continue
            lines.append(
                f"- {sid} | {dev.get('value', '')} | {dev.get('category', '')} | {dev.get('desc', '')}"
            )
        return "\n".join(lines)

    def pin_table_text(self, sid: str, limit: int = 64) -> str:
        """给 prompt 用的引脚表。这是「引脚白名单」。"""
        dev = self.get(sid)
        rows = [f"{p['number']}={p['name']}({p['type']})" for p in dev.get("pins", [])[:limit]]
        return f"{sid} ({dev.get('value', '')}): " + ", ".join(rows)

    def pin_tables_text(self, sids: list[str]) -> str:
        return "\n".join(self.pin_table_text(self.resolve(s)) for s in sids)
