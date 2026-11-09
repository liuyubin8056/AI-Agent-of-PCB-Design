"""评审 Agent：读问题清单，产出最小补丁。

为什么它必须是「带工具的审查者」
--------------------------------
如果评审 Agent 只是「另一个模型再看一遍设计」，它大概率会附和，多 Agent 毫无收益。
所以它的输入里一定是**确定性校验器 / ERC 的结构化输出**，输出是**可执行补丁**，
而不是一句「看起来不错」。这也是消融实验要验证的假设。
"""

from __future__ import annotations

import re

from .. import prompts
from ..ir import ERROR, DesignIR, Issue
from ..llm import LLMError
from ..patch import OPS, PatchOp
from .base import Agent

MAX_PATCHES = 6


def _measured_current(message: str) -> float | None:
    """从断言文案里取回实测电流（A）。文案格式由 simcheck 保证，有单测锁定。"""
    found = re.search(r"电流\s*([\d.]+)\s*mA", message or "")
    return float(found.group(1)) / 1000 if found else None


class CriticAgent(Agent):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "critic")
        kwargs.setdefault("role", "评审")
        super().__init__(**kwargs)
        self.unfixable: list[str] = []

    def review(
        self, ir: DesignIR, issues: list[Issue], round_no: int = 1
    ) -> list[PatchOp]:
        self.unfixable = []
        errors = [i for i in issues if i.severity == ERROR]
        if not errors:
            self.log(round_no, action="review", patches=0, note="无错误，跳过")
            return []

        ops: list[PatchOp] = []
        if not self.offline:
            try:
                data = self.llm.complete_json(
                    prompts.CRITIC_SYSTEM,
                    prompts.critic_user(
                        ir.summary_line(),
                        "\n".join(f"- {i}" for i in issues),
                        self.lib,
                        "\n".join(
                            f"- {i.ref} | {i.value} | {i.symbol}" for i in ir.bom
                        ),
                    ),
                )
                ops = self._sanitize(data.get("patches") or [])
            except (LLMError, ValueError, TypeError) as exc:
                self.notes.append(f"评审 LLM 失败，转规则自动修复：{exc}")
                ops = []

        if not ops:
            ops = self._autofix(ir, errors)

        self.log(
            round_no,
            action="review",
            patches=len(ops),
            ops=[o.op for o in ops],
            unfixable=len(self.unfixable),
        )
        return ops[:MAX_PATCHES]

    def review_blind(self, ir: DesignIR, round_no: int = 1) -> list[PatchOp]:
        """消融实验 C 组用：不给校验结果，只让模型「凭经验再看一眼」。

        这是多数人想象中多 Agent 的样子，也是本方案要证伪的对照组。
        离线模式不伪造盲评审，直接返回空（否则 A/B/C 无法区分）。
        """
        self.unfixable = []
        if self.offline:
            self.log(round_no, action="review_blind", patches=0, note="离线不支持盲评审")
            return []

        ops: list[PatchOp] = []
        try:
            data = self.llm.complete_json(
                prompts.CRITIC_BLIND_SYSTEM,
                prompts.critic_blind_user(
                    ir.summary_line(),
                    self.lib,
                    "\n".join(f"- {i.ref} | {i.value} | {i.symbol}" for i in ir.bom),
                ),
            )
            ops = self._sanitize(data.get("patches") or [])
        except (LLMError, ValueError, TypeError) as exc:
            self.notes.append(f"盲评审 LLM 失败：{exc}")
            ops = []

        self.log(
            round_no,
            action="review_blind",
            patches=len(ops),
            ops=[o.op for o in ops],
        )
        return ops[:MAX_PATCHES]

    # ------------------------------------------------------------------ #
    def _sanitize(self, raw: list) -> list[PatchOp]:
        ops: list[PatchOp] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("op")) not in OPS:
                if item.get("op") == "note":
                    self.unfixable.append(str(item.get("note", "")))
                continue
            op = PatchOp.from_dict(item)
            if op.op in {"set_value", "remove_part"} and not op.ref:
                continue
            if op.op in {"connect", "disconnect"} and not (op.net and op.member):
                continue
            ops.append(op)
        return ops

    def _led_resistor(self, ir: DesignIR, led_ref: str) -> str:
        """找 LED 阳极那条网上的限流电阻。"""
        for net in ir.nets:
            if f"{led_ref}.2" not in net.members:
                continue
            for member in net.members:
                ref = member.split(".")[0]
                item = ir.part(ref)
                if item and ref.startswith("R"):
                    return ref
        return ""

    def _autofix(self, ir: DesignIR, issues: list[Issue]) -> list[PatchOp]:
        """规则自动修复：只处理「补一颗器件」或「改一个值」就能好的问题。

        仿真结论也在这里兜底：LED 电流超标就调大限流电阻。没有这条，
        E 组（多 Agent + 仿真闸门）即使发现了问题也改不动，闭环断在半路。
        """
        from ..spice import format_value, parse_value

        used = set(ir.refs())
        counter: dict[str, int] = {}

        def nxt(prefix: str) -> str:
            n = counter.get(prefix, 0)
            while True:
                n += 1
                ref = f"{prefix}{n}"
                if ref not in used:
                    counter[prefix] = n
                    used.add(ref)
                    return ref

        rail = next((n.name for n in ir.nets if n.kind == "power"), "+5V")
        gnd = next((n.name for n in ir.nets if n.kind == "ground"), "GND")

        ops: list[PatchOp] = []
        for issue in issues:
            if len(ops) >= MAX_PATCHES:
                break
            net = issue.where
            if issue.code in {"SIM_LED_OVERDRIVE", "SIM_LED_OFF"} and issue.where:
                ref = self._led_resistor(ir, issue.where)
                item = ir.part(ref) if ref else None
                ohms = parse_value(item.value) if item else None
                if item and ohms:
                    # 按实测电流反推目标阻值：I ∝ 1/R，直接一步到位，
                    # 别用"乘 10 试试"这种拍脑袋的做法（会停在 165Ω / 15mA 这种尴尬值）
                    amps = _measured_current(issue.message)
                    if amps:
                        target_i = 0.005  # 指示灯工作点取 5mA
                        ohms = min(max(ohms * amps / target_i, 100.0), 100_000.0)
                    else:
                        ohms *= 10 if issue.code == "SIM_LED_OVERDRIVE" else 0.1
                    ops.append(PatchOp("set_value", ref=ref, value=format_value(ohms)))
                    continue
            if issue.code == "POWER_RAIL_NO_DECOUPLING" and net:
                c = nxt("C")
                ops += [
                    PatchOp("add_part", ref=c, symbol="pcbagent:CAP", value="100nF",
                            note="补去耦电容"),
                    PatchOp("connect", net=net, member=f"{c}.1"),
                    PatchOp("connect", net=gnd, member=f"{c}.2"),
                ]
            elif issue.code == "RESET_NO_PULLUP" and net:
                r = nxt("R")
                ops += [
                    PatchOp("add_part", ref=r, symbol="pcbagent:RES", value="10k",
                            note="补复位上拉"),
                    PatchOp("connect", net=net, member=f"{r}.1"),
                    PatchOp("connect", net=rail, member=f"{r}.2"),
                ]
            elif issue.code == "I2C_NO_PULLUP" and net:
                r = nxt("R")
                ops += [
                    PatchOp("add_part", ref=r, symbol="pcbagent:RES", value="4.7k",
                            note="补 I2C 上拉"),
                    PatchOp("connect", net=net, member=f"{r}.1"),
                    PatchOp("connect", net=rail, member=f"{r}.2"),
                ]
            elif issue.code == "XTAL_NO_LOAD_CAP" and net:
                c = nxt("C")
                ops += [
                    PatchOp("add_part", ref=c, symbol="pcbagent:CAP", value="22pF",
                            note="补晶振负载电容"),
                    PatchOp("connect", net=net, member=f"{c}.1"),
                    PatchOp("connect", net=gnd, member=f"{c}.2"),
                ]
            else:
                self.unfixable.append(f"{issue.code}: {issue.message}")
        return ops
