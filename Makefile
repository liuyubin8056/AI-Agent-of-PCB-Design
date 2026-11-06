PY ?= python
OUT ?= runs/demo

.PHONY: help install demo llm test ablate clean

help:
	@echo "make install  安装为可编辑包"
	@echo "make demo     离线跑通端到端 demo（无需 API key）"
	@echo "make llm      用真实 LLM 跑 demo（需先配好 .env）"
	@echo "make test     运行单元测试"
	@echo "make ablate   消融实验：8 用例 × 4 配置"
	@echo "make clean    清理运行时产物"

install:
	$(PY) -m pip install -e ".[dev]"

demo:
	$(PY) -m pcb_agent --demo --out $(OUT)

llm:
	$(PY) -m pcb_agent "帮我设计一块 Arduino 最小系统板" --out $(OUT)

test:
	$(PY) -m pytest -q

ablate:
	$(PY) -m pcb_agent.ablation --out runs/ablation

clean:
	rm -rf runs build dist .pytest_cache src/*.egg-info
