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

当前进度：**W1–W7 主体完成**。离线与真实 LLM 两条路径都跑通：一句话需求 → 18 器件 /
11 网络 / 48 连接点，**ERC 0 error**，从原理图导出的网表与 IR **逐引脚一致（差异 0）**，
故障注入能 2 轮自愈。剩下报告、视频与 PCB 侧 DRC。详见下文「六、八周排期」。

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

### 工作点仿真（IR → SPICE → ngspice）

```bash
python -m pcb_agent.spice --design examples/design.json --out runs/sim
```

不额外装任何东西：直接用 KiCad 自带的 `ngspice.dll`（本机是 **ngspice-46 shared
library**），用 `ctypes` 调 libngspice 的 C API。既没有 `ngspice.exe` 也不需要有。

用**子进程**跑，不是为了好看：libngspice 出问题是 access violation，会带走宿主
进程，仿真这种第三方 C 代码必须隔离。

实测输出（`examples/design.json`，18 器件 / 11 网络）：

| 网络 | 电压 | 手算 | 结论 |
|---|---|---|---|
| VIN_RAW | 12.0000 V | 12 V（外部输入） | ✅ |
| +5V | 5.0000 V | LDO 稳压 5 V | ✅ |
| LED_A | 2.0001 V | LED 正向压降 ≈2.0 V | ✅ |
| RESET | 4.9999 V | 10k 上拉 / (10k+1G) × 5 V | ✅ |
| XTAL1/2 | 0.0000 V | 未起振，直流为 0 | ✅ |

| 支路 | 电流 | 手算 | 结论 |
|---|---|---|---|
| R2 / D1 | 2.935 / 2.936 mA | (5−2.0)/1k = 3.0 mA，再减 22Ω 驱动内阻压降 | ✅ |

另外验证了压差行为：把 VIN_RAW 改成 5.5 V，输出掉到 **4.4 V**（5.5−1.1），
而不是硬撑 5 V —— 说明 LDO 不是"永远输出 5V"的假模型。

**器件怎么落到 SPICE** 全在 `data/spice_models.json`，四类：

| 类型 | 器件 | 建模方式 |
|---|---|---|
| `R`/`C`/`D` | 电阻、电容、LED | 两端元件；LED 用 `D(Is=1e-20 N=1.9 Rs=8)`，Vf≈2V |
| `X` | LDO、晶振 | 子电路：LDO 用 1.1V 压差 + 5mA 静态 + 输入电流随负载；晶振用 Lm/Cm/Rm+C0 |
| `LOAD` | MCU、I2C 传感器 | 电源脚对地等效电阻（MCU 2k/脚 ≈2.5mA），信号脚对地电容 |
| `SKIP` | 连接器、PWR_FLAG | 不建模，但**显式记入 unmodeled**，不静默当导线 |

**三个不粉饰的近似**：

- MCU 是数字器件，这里只有"电源脚等效负载 + GPIO 拉高"两个近似，不能替代时序仿真；
- 按键默认按**断开**处理，按下是瞬态事件，`.op` 里没有意义；
- AREF 显示 0 V：芯片内部基准没建模，只挂了一颗 100nF。

### 仿真断言：把实测值变成能触发返修的问题

五条判据，代码里是 `simcheck.py`，门限集中在 `data/spice_models.json` 的 `limits`：

| Issue code | 判据 | 抓什么错 |
|---|---|---|
| `SIM_RAIL_DEAD` | 电压 < 期望值的 50% | 稳压器没工作 / 轨被短死 |
| `SIM_RAIL_OUT_OF_RANGE` | 与期望值偏差 > 5% | 稳压值错、压差不足 |
| `SIM_LED_OVERDRIVE` | 电流 > 20 mA | 限流电阻过小（灯会烧） |
| `SIM_LED_OFF` | 电流 < 0.5 mA | 电阻过大 / 驱动没拉开 / 极性反 |
| `SIM_REGULATOR_OVERHEAT` | (Vin−Vout)×Iin > 0.8 W | SOT-223 不加散热片会烫 |
| `SIM_UNMODELED` | 有器件没模型 | 覆盖率透明，不假装全测了 |

实测一次完整闭环（`--inject-fault value`）：

```
注入故障：R2 改小 20 倍（值错误，连接不变）
R2 值 330 -> 16.5           # 故障态：LED 电流 61.8 mA
R2 值 16.5 -> 203.94        # 按实测电流反推阻值，一步回到合规区间
闭环：2 轮，0 个错误
```

注意这条故障 **ERC 0 error、DRC 0 error、11 条规则校验全绿**——只有仿真能看见。
修法也不是"乘 10 试一次"，而是 `R_new = R_old × I_实测 / I_目标`。

**为什么没有 RESET_TAU / I2C_RISE / DECOUPLE_Z**：这块板的 RESET 只有上拉没有
电容（没有 RC 可言）；去耦电容是理想电容，AC 阻抗恒为 0，测了也永远通过；
I2C 上升时间要单独做小信号瞬态分析。**宁可不写，也不写一条永远绿的假断言**，
这三条留作后续（需要先给电容补 ESR、给总线补集总电容模型）。


### 自动布局布线（进阶任务 ★★★★）

```bash
python -m pcb_agent --demo --pcb        # 原理图之后顺带出 .kicad_pcb 并布线
```

三个模块，各管一段，都不依赖 KiCad 进程：

| 模块 | 做什么 | 依据 |
|---|---|---|
| `footprints.py` | 解析官方 `.kicad_mod`，取真实焊盘（尺寸/钻孔/层） | 与你板上实际封装一致，DRC 才有意义 |
| `pcb.py` | 连接性驱动的贪心摆放：主控居中，其余器件放到它连接最多的伙伴附近 | 减少跨板长线 |
| `router.py` | 两层栅格 A*：直行代价 1、换层（过孔）12；先连最近一对再逐点成树 | 结果确定，可复现 |

实测（`--demo --pcb`，18 器件 / 11 网络）：

| 指标 | 结果 |
|---|---|
| 布通率 | **11/11（100%）** |
| 线长 / 过孔 | 1225.5 mm / 10 个 |
| PCB DRC | **0 个错误** / 8 个警告 |
| 板尺寸 | 185.8 × 78.6 mm（偏大，见下） |
| 耗时 | < 1 s |

**为什么不接 Freerouting**：`kicad-cli pcb export` 里没有 Specctra DSN 导出（实测子命令清单
只有 gerbers/step/svg 等），用 Freerouting 就得回到 GUI 或自己写 DSN，两条都不比写个路由器省事。
自己写还有个好处：同样的输入每次布出同样的线，能写进报告做复现。

**已知取舍（不粉饰）**：

- 只剩 8 个警告：2 个 `track_dangling`（个别贴片焊盘的短线端点悬空）、6 个
  `holes_co_located`（同网络的过孔与插针焊盘同位置，电气上无害）；
- 摆放是贪心的，板子偏大（185×78 mm）；没有做 rip-up&reroute，拥挤时某条网络可能布不通；
- 没有等长、阻抗、差分对、铺铜，这些不在本项目的目标里。


### 两个 KiCad 后端（cli / live）

```bash
python -m pcb_agent --demo                       # 默认 cli 后端：kicad-cli 导出网表 + ERC
python -m pcb_agent --demo --live                # live 后端：把原理图推进正在运行的 KiCad
PCB_KICAD_BACKEND=off python -m pcb_agent --demo # 彻底关掉 KiCad 校验
```

| 后端 | 谁在干活 | 擅长 | 局限 |
|---|---|---|---|
| `cli` | `kicad-cli` | 批量、可复现、进 CI | 看不见 GUI |
| `live` | KiCad IPC API（`kicad-python`） | 把产物推到人眼前、PCB 侧可编程 | **原理图没有对象模型**，ERC/网表还得靠 cli |
| `off` | 无 | 无 KiCad 环境也能跑 | 没有网表/ERC 交叉验证 |

`live` 的实测边界（KiCad 10.0.7 + kipy 0.7）：`get_version()` 正常，但
`get_open_documents()` / `get_board()` 都返回 `no handler available` —— IPC 的处理器挂在
编辑框上，没有打开文档时不可用；且 kipy 只暴露 `Board`（PCB），没有 Schematic。
所以 live 只做「推送 + 状态上报」，校验仍由 cli 负责。这条已在 `docs/report-material.md`
里连同三种打开方式的尝试一起记录，报告可直接引用。

### 器件库可以导入 KiCad 官方符号

手写的 10 个器件只是起步，KiCad 自带的 223 个符号库可以直接读：

```bash
python -m pcb_agent.symbol_import --libraries            # 列出 223 个库
python -m pcb_agent.symbol_import --search ATmega328P    # 按名字搜
python -m pcb_agent.symbol_import --import Device:R --import MCU_Microchip_ATmega:ATmega328P-P
```

导入后 id 形如 `pcbagent:Device_R`，引脚表来自官方 `.kicad_sym`（权威来源），
可被子电路模板与渲染器直接使用。`--dry-run` 只看解析结果不落盘。

### 真机验证结果（KiCad 10.0.7，2026-10-30）

不是"应该能打开"，是真跑了：

| 检查项 | 方法 | 结果 |
|---|---|---|
| 原理图可解析 | `kicad-cli sch export svg` | 成功导出，符号与网络名正常绘制 |
| 原理图 == 网表 | `kicad-cli sch export netlist` + `diff_with_ir` | **11/11 网络、48/48 连接点完全一致，差异 0 条** |
| 电气规则 | `kicad-cli sch erc --format json` | **0 error**（首版 17 error / 43 warning，按下面三条修掉） |
| 故障自愈 | `--inject-fault decoupling` | 2 轮收敛，删掉的去耦电容被自动补回，ERC 重新清零 |

首版 ERC 报错对应的三条修复，已写成测试锁定：

1. `pin_to_pin`：LDO 的 VOUT 已是 power_out，再加 PWR_FLAG 就成了两个 power output 相连
   → 只在"没有 power_out 驱动源"的网络上加 PWR_FLAG（`test_power_flag_only_when_no_driver`）。
2. `pin_not_connected`：28 脚 MCU 的空置 GPIO 没做标记 → 逐个打 `no_connect`
   （`test_floating_pins_get_no_connect`）。
3. `endpoint_off_grid`：坐标不在 1.27 mm 栅格 → 器件坐标与引脚端点全部吸附栅格
   （`test_all_connection_points_on_grid`）。

复现：

```bash
kicad-cli sch export netlist --format kicadsexpr -o board.net examples/board.kicad_sch
kicad-cli sch erc --format json --output erc.json examples/board.kicad_sch
python -m pytest tests/test_kicad.py -q
```

### 真实 LLM 实测（2026-11-06）

接入学校算力平台（OpenAI 兼容，`ByteDance-volcengine/DeepSeek-V4.1-Flash`）后的实测数据：

| 场景 | 结果 | 闭环轮次 | LLM 调用 | tokens | 耗时 |
|---|---|---|---|---|---|
| 基线：Arduino 最小系统 + LED + I2C + 7~12V | 18 器件 / 11 网络 / 48 连接点，0 error | 1 | 2 | 2556 | 17.8 s |
| 故障注入：删掉一颗去耦电容 | 自动补回，0 error | 2 | 3 | 3994 | 35.8 s |
| 简化需求：不要指示灯、不要传感器 | 13 器件 / 7 网络 / 33 连接点，0 error | 1 | 2 | — | — |
| 追问修订：把 C3 换成 100nF | 只改值，其余不动 | 1 | 1 | — | 37 s |

两次调用 = 需求分析 1 次 + 电路设计 1 次；闭环轮次增加时每次多 1 次评审调用。
同一份 `summary.json` 里能看到 `llm_usage`，报告里的实验数据直接取这里。

**真实 LLM 暴露并修掉的三个 bug**（都写成了回归测试）：

1. 模型给 `add_part` 写 `Device:C`（KiCad 官方库名），库里没有 → `add_part` 失败但配套
   `connect` 仍执行，留下「接了线但器件不存在」的悬空引用 `C9`，一次产生 5 个错误。
   修法：补丁改两阶段原子执行（预解析 symbol → 失败的位号整组丢弃），并给器件库加
   KiCad 官方库名别名（`Device:C` / `Device:R` / `Device:LED` …）。
2. 模型 `add_part ref=C1` 却 `connect C8.1`（位号笔误）→ 白跑一轮。修法：本批只有一个新增
   器件时按前缀重定向笔误位号；没有 `add_part` 支撑的 `connect` 一律剔除。
3. 追问「加一颗 10uF 电容」时模型只 `add_part` 不 `connect`，新增器件电气上是死的，
   而当时只算 warning。修法：新增 `PART_UNCONNECTED` 规则（error 级），并让 `revise`
   也走返修闭环——实测从「留下 2 个错误」变成「2 轮自动接好线」。

### 消融实验怎么跑

```bash
python -m pcb_agent.ablation                      # 8 用例 × 4 配置，默认各自注入一处故障
python -m pcb_agent.ablation --fault none         # 不注入故障，只看生成质量
python -m pcb_agent.ablation --cases 1-4 --configs A,B,C,D
python -m pcb_agent.ablation --offline --no-kicad # 冒烟，不烧 token
```

四组配置是**逐级加一个变量**的阶梯，差异才能归因：

| 配置 | 需求分析 | 返修方式 | 想验证什么 |
|---|---|---|---|
| A | 规则兜底 | 不返修 | 一次生成的错误率基线 |
| B | 规则兜底 | 确定性规则修复（不调 LLM） | 校验器本身值多少 |
| C | LLM | LLM 评审，但**看不到**校验结果 | 多 Agent 没有工具是不是白搭 |
| D | LLM | LLM 评审 + 结构化问题清单 | 本方案 |

为什么默认注入故障：生成层走子电路模板，天生正确，不注入时 A/B/C/D 全是零错误，
实验没有区分度。注入的故障必须**真的能被校验器抓到**（否则换下一个故障类型重试），
保证每个用例都有区分度。

为保证可复现，LLM 温度默认 **0**（`PCB_LLM_TEMPERATURE` 可改），否则同一个需求两次采样
选出的子电路都不一样，表格里的「选型准确率」会变成噪声。

产物：`ablation.md`（汇总表 + 明细表）、`ablation.csv`、`ablation.json`，
每格还留了 `runs/ablation-*/<用例>/<配置>/` 下的原理图与网表，可逐个复查。

### 消融实验结果（2026-11-06 跑，8 用例 × 4 配置）

> 数据源：`docs/ablation-2026-11-06.md`，原始明细见 `ablation.csv`（跑批命令见上一节）。每组配置在 8 条需求上
> 各注入一处可检测故障（去耦电容 / I2C 上拉 / 复位上拉），再交给该组的返修方式处理。

| 配置 | 说明 | 零错误率 | 平均错误 | 平均轮次 | 平均调用 | 平均 tokens | 平均耗时 | 选型准确率 |
|---|---|---|---|---|---|---|---|---|
| A | 单 Agent 一次生成 | 0% | 1.00 | 1.0 | 1.0 | 2027 | 12.1 s | 88% |
| B | 单 Agent + 规则校验器 | **100%** | 0.00 | 2.0 | 1.0 | **2283** | **16.3 s** | 100% |
| C | 多 Agent（评审无工具） | 0% | 1.75 | 1.75 | 3.75 | 12364 | 94.0 s | 100% |
| D | 多 Agent（评审带工具） | **100%** | 0.00 | 2.0 | 3.0 | 4447 | 34.8 s | 100% |

四条结论，含一条对方案不利的：

1. **不校验就一定带着错误交付**。A 组零错误率 0%，注入的故障全留在产物里。
2. **规则修复的性价比最高**。B 组只用 1 次 LLM 调用就把错误清零，比 D 省一半 token、
   快一半。说明「能用确定性代码解决的就别叫模型」。
3. **无工具的评审是负收益，比不修还差**。C 组零错误率 0%，平均错误 **1.75 个，比 A 的
   1.0 还多** —— 盲评审看不到问题，凭感觉改，反而制造了 `PART_UNCONNECTED`（只加器件
   不接线）。代价还是 D 的 2.8 倍 token、2.7 倍时间。这是本方案最想证明的一条。
4. **D 的价值不在本组故障上**。这轮注入的三种故障恰好都在规则覆盖范围内，所以 D 相比 B
   只是更贵、没有更好。D 的收益要等规则覆盖不到的问题（选型不合理、拓扑错误、
   跨模块约束冲突）才能体现 —— 这也是 W7 要补的实验：需要构造规则抓不到的故障类型。
   不做这个补充实验就宣称多 Agent 更好，是不诚实的。

### 补做：值错误 + 仿真闸门（2026-11-09）

上一轮的结论里有一条对方案不利：**D 组（多 Agent）相对 B 组没有优势**，原因是注入
的三种故障都在规则覆盖范围内。这一轮补上了规则抓不到的故障类型，并新增 **E 组**：

| 配置 | 需求分析 | 返修方式 | 仿真结论 | 与 D 的差别 |
|---|---|---|---|---|
| D | LLM | LLM 评审 + 问题清单 | 不进闭环 | — |
| **E** | LLM | LLM 评审 + 问题清单 | **进闭环** | 只多一个变量 |

故障改为 `value`：把 LED 限流电阻改小 20 倍，**连接一点没动**，所以 ERC/DRC/规则校验全绿。
3 个用例实测（离线执行，避免 LLM 抖动）：

| 用例 | 配置 | 结构错误 | **仿真错误** | 轮次 |
|---|---|---|---|---|
| C1 | B | 0 | **1** | 1 |
| C1 | E | 0 | **0** | 2 |
| C4 | B | 0 | **1** | 1 |
| C4 | E | 0 | **0** | 2 |
| C6 | B | 0 | **1** | 1 |
| C6 | E | 0 | **0** | 2 |

结论：**B 组不是"通过"，只是"看不见"**。E 组用同样的返修逻辑，多一个仿真证据就把
问题修干净了。这才是多 Agent + 工具的完整价值证明——之前缺的不是 Agent 数量，
是**一个能发现功能错误的工具**。

命令：`python -m pcb_agent.ablation --cases 1,4,6 --configs B,E --fault value`

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
├── data/spice_models.json       器件 → SPICE 模型映射（含 LDO/晶振子电路）
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
│   ├── kicad_live.py            KiCad IPC 后端（推送 + 状态上报）
│   ├── footprints.py            官方 .kicad_mod 解析
│   ├── pcb.py                   IR → .kicad_pcb（连接性驱动摆放）
│   ├── router.py                两层栅格 A* 自动布线
│   ├── ngspice.py               libngspice 封装（ctypes + 子进程隔离）
│   ├── spice.py                 IR → SPICE deck
│   ├── simulate.py              跑仿真 + 结构化报告（sim.json）
│   ├── simcheck.py              5 条电气行为断言 → Issue
│   ├── orchestrator.py          闭环编排、故障注入、产物落盘
│   ├── graph_langgraph.py       LangGraph 等价实现（可选依赖）
│   ├── trace.py                 JSONL 运行轨迹
│   ├── textutil.py              否定词检测（规则兜底用）
│   ├── agents/                  analyst / designer / critic
│   └── ui/streamlit_app.py      演示界面（可选）
└── tests/                       122 个用例（含 KiCad / ngspice 集成测试，未装自动跳过）
```

---

## 四、快速开始

要求 Python 3.10+。**不需要 API key；不装 KiCad 也能跑通全流程**（自动降级，跳过网表/ERC）。

```bash
git clone https://github.com/liuyubin8056/AI-Agent-of-PCB-Design.git
cd AI-Agent-of-PCB-Design

python -m pip install -e ".[dev]"     # 或直接 PYTHONPATH=src 运行
python -m pcb_agent --demo            # 零配置跑通全流程
python -m pytest -q                   # 54 passed
```

装了 KiCad 之后不用改配置：`KiCadGate` 会读注册表
`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall` 下的 `DisplayName` / `InstallLocation`
自动找到 `kicad-cli`（KiCad 安装器不写 PATH）。要手动指定就在 `.env` 里写 `KICAD_CLI=`。

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
2. **designer 也不能想加就加**。LLM 会过度设计：用户只要最小系统，它也给你加个指示灯。
   `DesignerAgent._prune_unsupported` 以**用户原话**为依据做后置裁剪（不看需求分析
   Agent 自己补的内容），没提到 `led_indicator` / `i2c_sensor` 就剔掉。和「引脚白名单」
   是同一个思路：模型提议，代码把关。
3. **回灌的是补丁不是重写**。`patch.py` 的 6 个原子操作（`set_value` / `add_part` /
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
| W3–W4 | 打通「自然语言 → BOM + 连接表」，输出结构化 JSON | **MVP 演示** | ✅ IR + 器件库 + 子电路模板 + 54 个测试 |
| W5–W6 | 用脚本接口把 JSON 变成真实原理图，支持简单改需求 | **原理图生成** | ✅ `.kicad_sch` 已在 KiCad 10.0.7 打开验证：网表零差异、ERC 零错误；补丁机制已完成 |
| W7 | 打磨提示词与体验，进阶组做 DRC 检查闭环 | 闭环通过 | 🟡 原理图侧闭环已通（故障注入可自愈，含 ERC）；PCB 侧 DRC 待接 |
| W8 | 写设计报告、录演示视频、准备答辩 PPT | 正式交付 | ⬜ |

近期待办：

- [x] 安装 KiCad，按 `docs/kicad-notes.md` 第 4 节清单验证 `.kicad_sch` 能打开
- [x] 跑 `kicad-cli sch export netlist`，用 `diff_with_ir` 证明「原理图 == 网表」
- [x] 接入真实 LLM，跑通 LLM 路径并记录 token 消耗（2 次调用 / 2556 tokens / 17.8 s）
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
- **LLM 接口要填到 `/v1`**：本项目的 `base_url` 会拼 `/chat/completions`，只写到域名会 404；
  模型名必须与 `GET {base_url}/models` 返回的 `id` 完全一致（学校平台是
  `ByteDance-volcengine/DeepSeek-V4.1-Flash`，不是 `deepseek-chat`）。
- **LLM 接口要填到 `/v1`**：本项目的 `base_url` 会拼 `/chat/completions`，只写到域名会 404；
  模型名必须与 `GET {base_url}/models` 返回的 `id` 完全一致（学校平台是
  `ByteDance-volcengine/DeepSeek-V4.1-Flash`，不是 `deepseek-chat`）。
- **只按 KiCad 8 格式输出**（版本号 `20231120`）。本机装的是 KiCad 10.0.7，向上兼容可直接
  打开；若要兼容更老的 KiCad，需要下调版本号并回归验证。
- **无头 ERC 会报 20 条 `lib_symbol_issues`**（"当前配置不包含符号库 pcbagent"）。原因是
  `kicad-cli` 只读全局符号库表，不读工程目录的 `sym-lib-table`；符号定义已内嵌在原理图
  `lib_symbols` 里，渲染和网表都正确，因此代码里把它当环境噪声过滤（见 `kicad_gate.py`）。
- **不做自动布线**，器件按网格摆放，标签可能视觉拥挤，不影响电气正确性。

---

## 八、学术诚信

- LLM 生成的内容：`prompts.py` 中的 prompt 模板、部分代码注释与文档初稿由 AI 辅助生成，
  接口、数据结构、校验规则、渲染逻辑由本人设计并逐条测试。
- 未参考任何往届作业或他人仓库。第三方依赖见 `pyproject.toml`。
- 关键设计决策（为什么不让 LLM 写 KiCad 文件、为什么用全局标签、为什么 critic 必须带工具）
  均可在答辩中口头解释。
