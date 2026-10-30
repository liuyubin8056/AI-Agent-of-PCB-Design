"""LangGraph 适配层：与 ``orchestrator.py`` 等价的状态图实现。

用途有二：
1. 进阶挑战「多 Agent 协作」的框架级证据——答辩可以直接把这张图导出成 mermaid；
2. 做对照实验：同一套节点，手写状态机 vs 图编排，比可维护性和可观测性。

langgraph 是可选依赖：
    pip install -e ".[graph]"
"""

from __future__ import annotations

from typing import Any, TypedDict

from .ir import ERROR, BoardSpec, DesignIR, Issue
from .orchestrator import Orchestrator
from .patch import apply


class DesignState(TypedDict, total=False):
    request: str
    spec: BoardSpec
    ir: DesignIR
    issues: list[Issue]
    round: int
    cleaned: bool
    log: list[str]


def build_graph(orch: Orchestrator, out_dir: str = "runs/graph"):
    """构建并编译状态图。节点函数直接复用 Orchestrator 里的角色。"""
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - 可选依赖
        raise RuntimeError('未安装 langgraph，请执行 pip install -e ".[graph]"') from exc

    max_rounds = orch.settings.max_rounds
    orch._bind_tracer(orch.out_dir)

    def node_analyst(state: DesignState) -> DesignState:
        spec = orch.analyst.analyze(state["request"], round_no=1)
        return {"spec": spec, "log": state.get("log", []) + ["analyst 完成"]}

    def node_designer(state: DesignState) -> DesignState:
        ir = orch.designer.design(state["spec"], state.get("issues") or [], round_no=state.get("round", 1))
        return {"ir": ir}

    def node_check(state: DesignState) -> DesignState:
        rnd = state.get("round", 1) + 1
        issues, _ = orch._evaluate(state["ir"], rnd, orch.out_dir)
        cleaned = not any(i.severity == ERROR for i in issues)
        return {"issues": issues, "round": rnd, "cleaned": cleaned}

    def node_critic(state: DesignState) -> DesignState:
        ops = orch.critic.review(state["ir"], state["issues"], round_no=state.get("round", 1))
        log = list(state.get("log", []))
        if not ops:
            log.append("评审未能给出补丁，终止")
            return {"log": log, "cleaned": True}
        ir, patch_log = apply(state["ir"], ops, orch.lib)
        log.extend(patch_log)
        log.extend(orch.critic.unfixable)
        return {"ir": ir, "log": log}

    def route(state: DesignState) -> str:
        if state.get("cleaned") or state.get("round", 0) >= max_rounds:
            return END
        return "critic"

    graph = StateGraph(DesignState)
    graph.add_node("analyst", node_analyst)
    graph.add_node("designer", node_designer)
    graph.add_node("check", node_check)
    graph.add_node("critic", node_critic)

    graph.set_entry_point("analyst")
    graph.add_edge("analyst", "designer")
    graph.add_edge("designer", "check")
    graph.add_conditional_edges("check", route, {"critic": "critic", END: END})
    graph.add_edge("critic", "check")
    return graph.compile()


def run(orch: Orchestrator, request: str, out_dir: str = "runs/graph") -> dict[str, Any]:
    """跑一张图，返回最终状态。"""
    graph = build_graph(orch, out_dir)
    final: dict[str, Any] = graph.invoke({"request": request, "round": 0, "log": []})
    return final
