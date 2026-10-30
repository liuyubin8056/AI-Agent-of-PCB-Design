# AI Agent of PCB Design

一句话需求 → BOM + 引脚级网表 → KiCad 8 能打开的原理图。

课程大作业《AI Agent 电路板设计》的实现仓库：不画板子，而是做一个会画板子的 Agent。

```
python -m pcb_agent "帮我设计一块 Arduino 最小系统板，带指示灯和 I2C 接口"
```

```
板卡：Arduino 最小系统板
结果：18 个器件 / 11 条网络 / 48 个连接点
闭环：1 轮，0 个错误 / 1 个警告
产物：runs/<时间戳>/board.kicad_sch  bom.csv  design.json  summary.json  trace.jsonl
```

当前进度：**W1–W4 已完成**（骨架 + MVP + 闭环 + 追问改需求），KiCad 8 侧的真机验证与
DRC 闭环留给 W5–W7。详见[八周排期](#八周排期)。

---

## 一、三条设计原则

这个项目的难点不在「调通 API」，而在「让 LLM 不出电气错误」。三条原则贯穿全仓库：

**1. LLM 只产出结构化中间表示（IR），绝不直接写 KiCad 文件。**
模型输出 `{器件, 值, 封装, 网络成员}` 这样的 JSON，由确定性代码渲染成
`.kicad_sch`。让模型直接拼 s-expression，它一定会编出不存在的引脚和非法 UUID。

**2. 引脚必须「喂给」模型，而不是「问」模型。**
`library.pin_table_text()` 把器件真实引脚表注入 prompt，模型只能从枚举里选。
配合器件库白名单校验，引脚幻觉会被当场抓住（`tests/test_validate.py::test_fake_pin_number_caught`）。

**3. 校验必须是代码。**
11 条电气规则用几十行 Python 写完，100% 拦得住「28 脚 MCU 只配一颗电容」
「I2C 没上拉」「复位脚悬空」这类错误。让另一个模型「再看看」既贵又不可靠。

---

## 二、技术路线

### 数据流

```
自然语言
   │  analyst：需求分析（LLM）
   ▼
BoardSpec          功能 / 电源轨 / 接口 / 约束
   │  designer：只做「选子电路 + 填参数」的选择题（LLM）
   ▼
BOM + 引脚级网表    ← subcircuits 用引脚语义 + 器件库确定性生成
   │  validate：11 条电气规则（代码）
   ▼
.kicad_sch         ← render：自建 lib_symbols，全局标签表达连接
   │  kicad-cli export netlist / erc（可选）
   ▼
问题清单            → critic：产出最小补丁 → 应用 → 回到校验（≤ N 轮）
```

### 技术选型

| 层 | 选型 | 为什么 |
|---|---|---|
| 编排 | 手写状态机（主）+ LangGraph 适配（可选） | 零依赖、可读、好调试；`graph_langgraph.py` 提供等价图实现用于答辩演示与对照 |
| 结构化输出 | 自实现 JSON 抽取 + 校验失败回灌重试 | 不引重依赖，换任何 OpenAI 兼容接口都能用 |
| 器件与引脚 | 自建 `data/symbols.json`（10 个器件） | 引脚表是白名单，也是校验依据；不依赖本机 KiCad 安装 |
| 电路生成 | 参考子电路模板（按引脚语义生成） | 经典电路不会错，模型只做选择题 |
| 原理图 | 自生成 `.kicad_sch`（KiCad 8 / 20231120） | 引脚号与网表严格一致，可复现，不依赖 KiCad 安装 |
| 自动化 | `kicad-cli`（网表 / ERC / DRC） | 闭环的唯一可行入口，见 `docs/kicad-notes.md` |
| 界面 | CLI（主）+ Streamlit（可选） | 录屏演示用 Streamlit，评测用 CLI |

> 没有用 CrewAI / AutoGen：角色对话式编排在「引脚级网表」这种强结构化任务上会发散、
> 成本高、不可复现。多 Agent 的价值在「角色职责 + 工具权限 + 共享结构化状态」，不在聊天。

### 与课程「五步走」的对应

| 课程要求 | 本仓库实现 |
|---|---|
| 01 环境搭建 | `pyproject.toml` 零必需依赖；`make demo` 直接跑通 |
| 02 自然语言 → BOM | `agents/analyst.py` + `agents/designer.py` + `library.py` |
| 03 连接关系生成 | `subcircuits.py`（引脚级网表，JSON/`design.json`） |
| 04 导入 KiCad 生成原理图 | `render.py` → `.kicad_sch`；`kicad_gate.py` 交叉校验 |
| 05 简单交互修正 | `patch.py` + `orchestrator.revise()`，`--revise "把C3换成100nF"` |

---

## 三、目录结构

```
pcb-agent/
├── data/symbols.json            器件库：10 个器件的引脚表、封装、关键词
├── docs/kicad-notes.md          KiCad 8 集成笔记与首次打开检查清单
├── examples/                    一次真实运行的产物（可下载的示例原理图）
├── src/pcb_agent/
│   ├── __main__.py              CLI：--demo / --revise / --inject-fault / --json
│   ├── ir.py                    BoardSpec / BomItem / Net / DesignIR（唯一契约）
│   ├── library.py               器件库与引脚白名单
│   ├── subcircuits.py           4 个参考子电路模板（按引脚语义生成）
│   ├── prompts.py               三个角色的 system prompt
│   ├── llm.py                   标准库实现的 OpenAI 兼容客户端
│   ├── validate.py              11 条电气规则
│   ├── patch.py                 6 个原子补丁操作
│   ├── render.py                IR → .kicad_sch
│   ├── kicad_gate.py            kicad-cli 网关 + s-expr 解析 + 网表比对
│   ├── orchestrator.py          闭环编排、故障注入、产物落盘
│   ├── graph_langgraph.py       LangGraph 等价实现（可选依赖）
│   ├── trace.py                 JSONL 运行轨迹
│   ├── textutil.py              否定词检测（规则兜底用）
│   ├── agents/                  analyst / designer / critic
│   └── ui/streamlit_app.py      演示界面（可选）
└── tests/                       47 个用例，全部离线可跑
```

---

## 四、快速开始

要求 Python 3.10+。**不需要 API key，不需要安装 KiCad。**

```bash
git clone https://github.com/liuyubin8056/AI-Agent-of-PCB-Design.git
cd AI-Agent-of-PCB-Design

python -m pip install -e ".[dev]"     # 或直接 PYTHONPATH=src 运行
python -m pcb_agent --demo            # 零配置跑通全流程
python -m pytest -q                   # 47 passed
```

接入真实 LLM（DeepSeek / 通义 / 智谱 / 学校平台 / 本地 Ollama 均可）：

```bash
cp .env.example .env
# 填 PCB_LLM_BASE_URL / PCB_LLM_API_KEY / PCB_LLM_MODEL，并设 PCB_OFFLINE=false
python -m pcb_agent "帮我设计一块 Arduino 最小系统板"
```

三个演示命令，正好对应要交的视频内容：

```bash
# 1) 一句话需求到原理图
python -m pcb_agent --demo --out runs/demo

# 2) 追问改需求（走补丁，不重新生成）
python -m pcb_agent --demo --revise "把C3换成100nF" --out runs/rev

# 3) 闭环实证：故意删掉一颗去耦电容，看 Agent 自己发现并修好
python -m pcb_agent --demo --inject-fault decoupling --out runs/fault
```

第 3 条是最值得录进视频的：日志会打印「注入故障：删除 C1」→「校验发现 1 个错误」
→「新增 C1 (CAP 100nF)」→「C1.1 → +5V、C1.2 → GND」→「0 个错误，2 轮收敛」。

可选依赖：

```bash
pip install -e ".[ui]"      # streamlit run src/pcb_agent/ui/streamlit_app.py
pip install -e ".[graph]"   # LangGraph 版状态图
```

---

## 五、多 Agent 协作

四个节点，三个 LLM 角色 + 一个确定性工具节点：

| 角色 | 输入 | 输出 | 权限边界 |
|---|---|---|---|
| 需求分析 analyst | 一句话需求 | `BoardSpec` | 只读器件库；不碰电路连接 |
| 电路设计 designer | `BoardSpec` + 上轮问题 | 子电路选择 + 参数 → `BOM` + `Netlist` | 只能从子电路库和器件库里选，不许编 id |
| 工具校验（非 LLM） | `Netlist` | `.kicad_sch` + 规则问题 + ERC 报告 | 只读写文件，不做判断 |
| 评审 critic | 问题清单 + 设计概况 | `PatchOp[]` 最小补丁 | 只能改被指出的问题，不能重写 |

```
analyst → designer → [render + validate + kicad] → critic → apply patch → 回到校验
                            ↑                                        │
                            └────────────── ≤ N 轮 ──────────────────┘
```

两条硬规矩：

1. **critic 必须带工具**。它先跑确定性校验再发言，否则就是两个模型互相附和。
2. **回灌的是补丁不是重写**。`patch.py` 的 6 个原子操作（`set_value` / `add_part` /
   `remove_part` / `connect` / `disconnect` / `rename_net`）让每次修改都有 diff 可审计，
   同时解决第 5 步「追问改需求」和 DRC 闭环里的自动修复。

### 计划中的消融实验（报告 20 分的核心数据）

固定 8 条测试需求（LED 闪烁 / Arduino 最小系统 / I2C 传感器 / LDO 供电 / RS-485 收发 /
按键消抖 / 继电器驱动 / 电源监测），跑四组对照：

| 组 | 配置 | 观察指标 |
|---|---|---|
| A | 单 Agent 一次生成 | JSON 一次通过率 / 引脚幻觉率 |
| B | 单 Agent + 规则校验器 | ERC 零错误率 / 平均轮次 |
| C | 多 Agent（critic 无工具） | 是否优于 B（预期：不会） |
| D | 多 Agent（critic 带工具，本方案） | 收敛率 / token / 耗时 |

预期 C 不如 B —— 这个负结果本身就是宣讲里要求的「失败案例分析」。

---

## 六、八周排期

| 周 | 目标 | 里程碑 | 状态 |
|---|---|---|---|
| W1 | 认识 KiCad 与 LLM API，装环境，跑通模板工程 | 脚手架跑通 | ✅ 仓库骨架 + 零依赖 demo |
| W2 | 学 prompt 设计与工具调用，写最小 Agent | 会写工具调用 | ✅ LLM 客户端 + JSON 回灌重试 |
| W3–W4 | 打通「自然语言 → BOM + 连接表」，输出结构化 JSON | **MVP 演示** | ✅ IR + 器件库 + 子电路模板 + 47 个测试 |
| W5–W6 | 用脚本接口把 JSON 变成真实原理图，支持简单改需求 | **原理图生成** | 🟡 渲染器已产出 `.kicad_sch`，待在 KiCad 8 真机打开验证；补丁机制已完成 |
| W7 | 打磨提示词与体验，进阶组做 DRC 检查闭环 | 闭环通过 | 🟡 原理图侧闭环已通（故障注入可自愈）；PCB 侧 DRC 待接 |
| W8 | 写设计报告、录演示视频、准备答辩 PPT | 正式交付 | ⬜ |

近期待办：

- [ ] 安装 KiCad 8，按 `docs/kicad-notes.md` 第 4 节清单验证 `.kicad_sch` 能打开
- [ ] 跑 `kicad-cli sch export netlist`，用 `diff_with_ir` 证明「原理图 == 网表」
- [ ] 接入真实 LLM，跑通一次 LLM 路径并记录 token 消耗
- [ ] 扩充测试需求到 8 条，产出消融实验表格
- [ ] 生成 `.kicad_pcb`，接 `pcb drc` 做进阶任务

---

## 七、设计取舍与已知限制

- **原理图图形从简**：`lib_symbols` 由本仓库生成（矩形 + 引脚），不是 KiCad 官方符号图形。
  优先保证「引脚编号与网表一致」这件对电气正确性至关重要的事。换官方库只需改
  `render.py::emit_lib_symbol`。
- **不画 wire，用全局标签**：免疫器件重叠导致的连线错乱，代价是图面标签较多。
- **去耦判据是近似**：「电容数 >= 电源脚数」。严格判断「是否紧靠引脚」需要 PCB 布局信息。
- **当前器件库只有 10 个器件**，只够做最小系统类板卡；扩充方式见 `data/symbols.json`。
- **规则兜底不等于语义理解**：离线模式靠关键词 + 否定词检测（`textutil.py`），
  复杂需求仍需要真实 LLM。
- **KiCad 侧尚未真机验证**：本机未安装 KiCad 8，`.kicad_sch` 目前只做了括号配平、
  s-expression 解析、引脚端点与 IR 一一对应三层自检。

---

## 八、学术诚信

- LLM 生成的内容：`prompts.py` 中的 prompt 模板、部分代码注释与文档初稿由 AI 辅助生成，
  接口、数据结构、校验规则、渲染逻辑由本人设计并逐条测试。
- 未参考任何往届作业或他人仓库。第三方依赖见 `pyproject.toml`。
- 关键设计决策（为什么不让 LLM 写 KiCad 文件、为什么用全局标签、为什么 critic 必须带工具）
  均可在答辩中口头解释。
