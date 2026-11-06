"""LLM 客户端：只用标准库，任何 OpenAI 兼容接口都能接。

选型理由：作业里换模型的概率极高（DeepSeek / 通义 / 智谱 / 学校平台 / 本地 Ollama），
所以不绑任何 SDK。``complete_json`` 在解析失败时会把错误回灌给模型重试一次，
这是抑制「格式幻觉」最便宜的一招。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings.load()
        # 累计 token 用量，报告里的实验数据直接从这里取
        self.usage: dict[str, int] = {
            "calls": 0,
            "retries": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        # 累计 token 用量，报告里的实验数据直接从这里取
        self.usage: dict[str, int] = {
            "calls": 0,
            "retries": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    @property
    def available(self) -> bool:
        return not self.settings.offline and bool(self.settings.api_key)

    # ------------------------------------------------------------------ #
    def complete(self, system: str, user: str, temperature: float | None = None) -> str:
        if not self.available:
            raise LLMError("当前为离线模式，没有可用的 LLM 后端")
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.temperature if temperature is None else temperature,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise LLMError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"调用失败：{exc}") from exc

        usage = body.get("usage") or {}
        self.usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                self.usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"返回结构异常：{str(body)[:200]}") from exc

    # ------------------------------------------------------------------ #
    def complete_json(self, system: str, user: str, retries: int = 1) -> dict[str, Any]:
        prompt = user
        last_error = ""
        for attempt in range(retries + 1):
            text = self.complete(system, prompt)
            try:
                return extract_json(text)
            except ValueError as exc:
                last_error = str(exc)
                self.usage["retries"] += 1
                prompt = (
                    f"{user}\n\n你上一次的输出无法解析为 JSON（{last_error}）。"
                    "请只输出一个合法的 JSON 对象，不要任何解释文字和代码块标记。"
                )
        raise LLMError(f"连续 {retries + 1} 次无法得到合法 JSON：{last_error}")


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """从可能带代码块或前后废话的回复里抠出 JSON 对象。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("空回复")
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        raise ValueError("顶层不是对象")
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("找不到 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    return data
