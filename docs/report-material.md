# 报告素材：两条技术路线对比（KiCAD-MCP-Server vs 本项目）

> 这份文件是给**设计报告的「相关工作 / 方案对比」章节**准备的素材，不是对外文档。
> 结论可以直接引用，实测数据来自 2026-11-09 本机 KiCad 10.0.7 + kicad-python 0.7 的验证。

## 1. 对比对象

| | KiCAD-MCP-Server | 本项目（pcb-agent） |
|---|---|---|
| 来源 | GitHub: `mixelpixx/KiCAD-MCP-Server`（多个 fork 活跃），B 站有完整演示 | 本次大作业从零搭建 |
| 形态 | Node.js 编写的 **MCP 服务器**，向 LLM 客户端暴露 122 个工具 | Python 包 + CLI，内部三角色 Agent + 确定性流水线 |
| 与 KiCad 的关系 | 四套后端并存：`pcbnew`(SWIG)、**KiCad 9+ IPC API（kipy）**、`kicad-skip`(s-expr)、`kicad-cli` 包装 | 自生成 `.kicad_sch` + `kicad-cli` 自动校验；可选 IPC 后端 |
| 交互范式 | **人在环**：模型逐次调工具，用户看着它一点点改 | **无人流水线**：一句话进，产物 + 校验报告出 |
| 典型能力 | 布线、铺铜、Gerber、3D、JLCPCB 料库、Freerouting 自动布线 | 需求 → BOM → 引脚级网表 → 原理图 → ERC/网表校验 → 补丁返修 |

## 2. 六个维度的差别（写报告时按这个顺序讲）

| 维度 | MCP 路线 | 本项目路线 | 判据 |
|---|---|---|---|
| 真值载体 | KiCad 进程内存里的活状态 | 自建 `DesignIR`（可 diff 的文件） | 能否复现、能否做实验 |
| 连接决策者 | LLM 每次工具调用临场决定 | 子电路模板 + 引脚白名单确定性生成 | 引脚幻觉率 |
| 正确性时机 | 事后 ERC/DRC 报错再改 | 事前规则拦截 + 渲染确定性 + 闭环返修 | 零错误率（见消融表） |
| 单次成本 | 几十次工具调用 | 2–3 次 LLM 调用 + 本地计算 | tokens / 耗时 |
| 可复现性 | 差（依赖会话、UI、采样） | 强（同 IR 同文件） | 能否写消融表 |
| 适合场景 | 交互式改板、演示、布局布线 | 批量生成、评测、可测流水线 | — |

一句话总结（可直接用作报告结论）：

> MCP 路线把 KiCad 变成 LLM 的手；本项目把 LLM 关在 IR 的笼子里。
> 前者适合「人盯着改」，后者适合「跑起来能验收」。

## 3. 实测证据（本机，2026-11-09）

环境：KiCad 10.0.7（`F:\KiCard`）、`pip install kicad-python`（kipy 0.7）、已勾选
Preferences → Plugins → Enable KiCad IPC API。

```
from kipy import KiCad
KiCad().get_version()          -> 10.0.7 (10.0.7)      # 连接成功
KiCad().get_api_version()      -> 10.0.6
KiCad().get_open_documents(...) -> ApiError: no handler available
                                   for request of type GetOpenDocument
KiCad().get_board()            -> 同上（无打开文档时）
```

为了让 IPC 识别到文档，试过三种打开方式，均未成功建立文档级 handler：

1. `os.startfile("board.kicad_sch")` —— 系统关联打开，窗口仍是工程管理器（标题 `1 — KiCad 10.0`）
2. `eeschema.exe board.kicad_sch` —— 未产生新的编辑窗口
3. 生成 `board.kicad_pro` 后 `kicad.exe board.kicad_pro` —— 同样无 handler

结论（写进报告时要写明这是**当时版本**的行为边界）：

- IPC API 的请求处理器挂在**编辑框**上，没有打开文档时只有 `get_version` 一类全局接口可用；
- `kipy 0.7` 暴露的对象模型只有 `Board`（PCB），**原理图没有对象模型**，也没有 ERC / 网表接口；
- 因此「用 IPC 代替 kicad-cli 做原理图校验」在当前版本不可行，ERC/网表仍必须走 CLI。

## 4. 借鉴了什么（本轮已落地）

| # | 借鉴点 | 落地方式 | 状态 |
|---|---|---|---|
| 1 | IPC 作为「运行时闸门」 | `src/pcb_agent/kicad_live.py`：连接、版本、文档列表、把产物推进 GUI；`PCB_KICAD_BACKEND=cli\|live\|off` 三选一，`--live` 开关 | ✅ 已实现（受上节能力边界限制，只做推送 + 状态上报） |
| 2 | 用官方符号库替代手写器件表 | `src/pcb_agent/official.py` + `python -m pcb_agent.symbol_import`：读 KiCad 自带 223 个 `.kicad_sym`，导出引脚表并入 `data/symbols.json` | ✅ 已实现并实测（ATmega328P-P 28 脚、封装 DIP-28 正确） |
| 3 | 用 `pcbnew` + Freerouting 做自动布局布线 | 未做 | ⬜ 列为后续工作（对应进阶任务 ★★★★） |
| 4 | 工具粒度：一个能力一个原子工具 | 对齐到 `patch.py` 的 6 个原子补丁；新增 `open_document` / `status` / `select_footprints` 三个 live 工具 | ✅ 部分落地 |

## 5. 为什么不整体换成 MCP 路线（报告里要有这段"不做什么"）

1. **做不出实验**：会话状态不可复现，本项目的消融表（8 用例 × 4 配置）直接无法产出，
   而报告 20 分里"实验有数据"是硬指标。
2. **成本高一个量级**：实测盲评审（C 组）3.75 次调用已是单 Agent 的 3.75 倍；
   MCP 路线单次任务几十次工具调用，成本和时延都不可接受。
3. **错误难定位**：没有 IR 就没有 diff，也就没有 `PART_UNCONNECTED` 这类规则级校验，
   错误会散落在几十次调用的中间态里。
4. **它自己也是混合的**：122 个工具里只有 21 个走 IPC，其余仍是 SWIG / kicad-cli。
   成熟做法本来就是多后端，这反过来支持本项目的「保留 IR + IPC 作为可选后端」。

## 6. 引用

- 项目主页：https://github.com/mixelpixx/KiCAD-MCP-Server
- 视频参考：https://www.bilibili.com/video/BV17iem6XEDv/
- KiCad IPC API 文档：KiCad Developer Docs → IPC API
- 客户端库：`kicad-python`（import 名 `kipy`）
