"""配置：环境变量 + .env 文件（零依赖，不引 python-dotenv）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 仓库根目录：src/pcb_agent/config.py -> parents[2]
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"


def _load_dotenv(path: Path) -> None:
    """把 .env 中的键值读进 os.environ，已存在的环境变量优先。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    offline: bool = True
    max_rounds: int = 3
    kicad_cli: str = ""
    kicad_enabled: bool = True
    temperature: float = 0.0  # 默认 0：实验要可复现，不要采样抖动
    timeout: int = 120

    @classmethod
    def load(cls, dotenv: bool = True) -> "Settings":
        if dotenv:
            _load_dotenv(ROOT / ".env")
        api_key = os.environ.get("PCB_LLM_API_KEY", "").strip()
        offline_env = os.environ.get("PCB_OFFLINE")
        # 没配密钥就自动退回离线模式，保证「克隆即可运行」
        offline = _as_bool(offline_env, default=not bool(api_key))
        return cls(
            base_url=os.environ.get("PCB_LLM_BASE_URL", "https://api.deepseek.com/v1").strip(),
            api_key=api_key,
            model=os.environ.get("PCB_LLM_MODEL", "deepseek-chat").strip(),
            offline=offline or not api_key,
            max_rounds=int(os.environ.get("PCB_MAX_ROUNDS", "3") or "3"),
            kicad_cli=os.environ.get("KICAD_CLI", "").strip(),
            kicad_enabled=_as_bool(os.environ.get("PCB_KICAD"), default=True),
            temperature=float(os.environ.get("PCB_LLM_TEMPERATURE", "0") or "0"),
            timeout=int(os.environ.get("PCB_LLM_TIMEOUT", "120") or "120"),
        )

    def describe(self) -> str:
        if self.offline:
            return "离线 Mock 后端（规则引擎，不联网）"
        return f"LLM 后端 {self.model} @ {self.base_url}"
