"""消融实验跑批的测试：结构正确 + A/B 阶梯有区分度 + 产物可生成。

全部离线、关 kicad，秒级完成。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcb_agent.ablation import (
    CASES,
    CONFIGS,
    CaseResult,
    _parse_list,
    aggregate,
    run_case,
    to_markdown,
    write_outputs,
)
from pcb_agent.config import Settings
from pcb_agent.orchestrator import Orchestrator


def make_orch(tmp_path: Path) -> Orchestrator:
    settings = Settings(
        offline=True, api_key="", model="", max_rounds=3,
        kicad_enabled=False, sim_enabled=False,
    )
    return Orchestrator(settings=settings, out_dir=tmp_path)


def test_case_matrix_shape():
    assert len(CASES) == 8
    assert len({c.id for c in CASES}) == 8
    for case in CASES:
        assert case.expect, "每个用例都要有期望子电路，否则算不了选型准确率"


def test_configs_form_a_ladder():
    """四组配置必须是逐级加一个变量，否则差异无法归因。"""
    assert [CONFIGS[c].repair for c in "ABCD"] == ["none", "autofix", "blind", "tool"]
    assert CONFIGS["A"].use_analyst is False
    assert CONFIGS["D"].use_analyst is True
    # E 只比 D 多一个变量，否则实验没法归因
    assert CONFIGS["E"].sim is True and CONFIGS["D"].sim is False
    assert CONFIGS["E"].repair == CONFIGS["D"].repair


def test_parse_list():
    assert _parse_list("all", 8) == list(range(1, 9))
    assert _parse_list("1,3,5", 8) == [1, 3, 5]
    assert _parse_list("2-4", 8) == [2, 3, 4]
    assert _parse_list("1,9", 8) == [1]  # 越界忽略


def test_a_keeps_fault_b_fixes_it(tmp_path):
    """A 不返修应留下错误，B 用规则修复应清零——这是实验成立的底线。"""
    orch = make_orch(tmp_path)
    case = CASES[0]  # 完整最小系统，三种故障都适用
    a = run_case(orch, case, CONFIGS["A"], tmp_path / "A", fault="rotate")
    b = run_case(orch, case, CONFIGS["B"], tmp_path / "B", fault="rotate")

    assert a.fault, "默认模式必须注入一个能被检出的故障"
    assert a.errors >= 1, "A 组不返修，应该带着故障交付"
    assert b.errors == 0, "B 组规则修复后应为 0 错误"
    assert b.rounds > a.rounds


def test_fault_none_measures_generation(tmp_path):
    orch = make_orch(tmp_path)
    r = run_case(orch, CASES[1], CONFIGS["A"], tmp_path / "n", fault="none")
    assert r.fault == ""
    assert r.parts > 0 and r.nets > 0
    assert r.selection_ok


def test_outputs_written(tmp_path):
    orch = make_orch(tmp_path)
    results = [
        run_case(orch, CASES[0], CONFIGS["A"], tmp_path / "r1", fault="rotate"),
        run_case(orch, CASES[0], CONFIGS["B"], tmp_path / "r2", fault="rotate"),
    ]
    summary = aggregate(results)
    assert summary["A"]["zero_error_rate"] == 0.0
    assert summary["B"]["zero_error_rate"] == 1.0

    paths = write_outputs(results, summary, tmp_path / "out")
    for path in paths.values():
        assert Path(path).exists()
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert len(data["results"]) == 2
    assert "汇总" in Path(paths["markdown"]).read_text(encoding="utf-8")


def test_failed_run_does_not_break_aggregate():
    rows = [
        CaseResult(case_id="C1", config="A", request="x", errors=2),
        CaseResult(case_id="C1", config="A", request="x", note="运行失败"),
    ]
    summary = aggregate(rows)
    assert summary["A"]["cases"] == 2
    assert "消融实验结果" in to_markdown(rows, summary)


@pytest.mark.parametrize("code", ["A", "B", "C", "D"])
def test_every_config_runs_offline(tmp_path, code):
    """离线时 C 的盲评审会退化为空补丁，但不能崩。"""
    orch = make_orch(tmp_path / code)
    r = run_case(orch, CASES[0], CONFIGS[code], tmp_path / code, fault="rotate")
    assert r.rounds >= 1
    assert r.parts > 0
