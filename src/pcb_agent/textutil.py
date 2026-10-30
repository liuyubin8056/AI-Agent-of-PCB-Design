"""轻量文本工具：只做「否定词检测」这一件事。

规则兜底模式没有 LLM 的语义理解，最容易翻车的场景是：
用户说「不需要传感器」，关键词匹配却把传感器加了进去。
所以在匹配关键词前先看一眼前面有没有否定词。
"""

from __future__ import annotations

NEG_PREFIXES = ("不需要", "不用", "不要", "无需", "不含", "不带", "去掉", "去掉", "取消", "省掉")
_WINDOW = 12


def is_negated(text: str, word: str) -> bool:
    """判断 ``word`` 在 ``text`` 里是否被否定词修饰。

    只做邻近距离判断（否定词后 12 个字符内出现目标词），够用且不误伤。
    """
    if not text or not word:
        return False
    low = text.lower()
    target = word.lower()
    for prefix in NEG_PREFIXES:
        start = 0
        while True:
            idx = low.find(prefix, start)
            if idx == -1:
                break
            window = low[idx + len(prefix) : idx + len(prefix) + _WINDOW]
            if target in window:
                return True
            start = idx + len(prefix)
    return False
