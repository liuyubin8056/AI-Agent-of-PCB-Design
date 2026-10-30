"""Streamlit 演示界面（可选依赖）。

    pip install -e ".[ui]"
    streamlit run src/pcb_agent/ui/streamlit_app.py

录屏演示时用它：左边输入需求，右边实时看到每一步产物与问题清单。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    sys.stderr.write('未安装 streamlit，请执行 pip install -e ".[ui]"\n')
    raise

# 允许直接 `streamlit run` 时不装包也能跑
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pcb_agent.config import Settings  # noqa: E402
from pcb_agent.orchestrator import Orchestrator  # noqa: E402
from pcb_agent.validate import report_text  # noqa: E402

DEMO = "帮我设计一块 Arduino 最小系统板，带电源指示灯和一路 I2C 传感器接口，7~12V 供电"

st.set_page_config(page_title="AI Agent 电路板设计", layout="wide")
st.title("AI Agent 电路板设计")
st.caption("一句话需求 -> BOM + 引脚级网表 + KiCad 8 原理图")

with st.sidebar:
    st.subheader("运行设置")
    request = st.text_area("设计需求", DEMO, height=120)
    fault = st.selectbox("故障注入（演示闭环用）", ["无", "decoupling", "i2c", "reset"])
    revise = st.text_input("生成后追问修改", "把C3换成100nF")
    out_dir = st.text_input("输出目录", "runs/ui")
    run_it = st.button("开始设计", type="primary")

    settings = Settings.load()
    st.divider()
    st.caption(f"LLM：{settings.describe()}")
    st.caption("kicad-cli：可用" if Orchestrator(settings).gate.available else "kicad-cli：未安装")

if run_it:
    orch = Orchestrator(settings=settings, out_dir=out_dir)
    with st.spinner("设计中…"):
        result = orch.design(
            request, out_dir, inject_fault="" if fault == "无" else fault
        )
        if revise.strip():
            result = orch.revise(result.ir, revise, Path(out_dir) / "revision")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("器件数", len(result.ir.bom))
    col2.metric("网络数", len(result.ir.nets))
    col3.metric("错误", result.errors)
    col4.metric("闭环轮次", result.rounds)

    st.subheader("板级规格")
    st.json(result.ir.spec.to_dict())

    st.subheader("BOM")
    st.dataframe(
        [
            {"位号": i.ref, "值": i.value, "符号": i.symbol, "作用": i.role}
            for i in result.ir.bom
        ],
        use_container_width=True,
    )

    st.subheader("网络表")
    st.dataframe(
        [
            {"网络": n.name, "类型": n.kind, "连接点": ", ".join(n.members)}
            for n in result.ir.nets
        ],
        use_container_width=True,
    )

    st.subheader("校验与返修")
    st.text(report_text(result.issues))
    if result.log:
        st.code("\n".join(result.log), language="text")

    st.subheader("产物")
    for name, path in result.artifacts.items():
        st.write(f"**{name}**：`{path}`")
    sch = result.artifacts.get("原理图")
    if sch and Path(sch).exists():
        st.download_button(
            "下载 .kicad_sch",
            Path(sch).read_text(encoding="utf-8"),
            file_name="board.kicad_sch",
        )
    st.download_button(
        "下载 design.json",
        json.dumps(result.ir.to_dict(), ensure_ascii=False, indent=2),
        file_name="design.json",
    )
