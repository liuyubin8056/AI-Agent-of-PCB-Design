# KiCad 集成笔记

本仓库的 KiCad 侧能力全部实测过，下面是结论与踩坑记录，便于复现。

## 1. 本机环境（2026-10-30 实测）

| 项 | 值 |
|---|---|
| 版本 | KiCad **10.0.7**（注册表 `DisplayName`） |
| 安装位置 | `F:\KiCard`（注册表 `InstallLocation`，**非默认路径**） |
| CLI | `F:\KiCard\bin\kicad-cli.exe`，**不在 PATH** |
| 自动发现 | `kicad_gate.KiCadGate._from_registry()` 读 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall` 的 `DisplayName` / `InstallLocation` |

注册表查询命令：

```powershell
Get-ItemProperty HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\* |
  Where-Object { $_.DisplayName -like "*KiCad*" } |
  Select-Object DisplayName, DisplayVersion, InstallLocation
```

## 2. 环境事实

| 事实 | 影响 |
|---|---|
| KiCad 自带 `kicad-cli` | 网表、ERC、DRC 全部可命令行化，是闭环的前提 |
| `pcbnew` 有 Python 绑定，`eeschema` **没有** | 「用 Python 脚本画原理图」只能走 s-expression 或 SKiDL 两条路 |
| `.kicad_sch` 首行版本号 `20231120` = KiCad 8 格式 | 写错 KiCad 直接拒绝打开；KiCad 9/10 可向上读取 v8 文件 |
| 符号库坐标系 Y 轴向上、原理图 Y 轴向下 | 器件放在 (X, Y) 后，引脚世界坐标 = `(X + px, Y - py)` |
| 连接栅格是 1.27 mm | 坐标不在栅格上，ERC 报 `endpoint_off_grid` |

## 3. 本项目采用的路线

**自己生成 `.kicad_sch` + 配套 `pcbagent.kicad_sym` + `sym-lib-table`**：

1. 不依赖本机是否装了 KiCad（CI、助教机器都能跑）；
2. 引脚编号由器件库决定，与网表 100% 对齐；
3. 可精确控制坐标，天然可复现。

**连接方式：全局标签代替 wire。** 全局标签放在引脚端点上即视为电气相连，
整张图不需要任何 `wire`，也就不存在「器件重叠导致连线错乱」。
代价是图面标签密集，不影响电气正确性。

## 4. 实测结果与三条必修项

首版生成的原理图 ERC 结果：**17 error / 43 warning**。逐条修完后 **0 error**。

| ERC 类型 | 原因 | 修复 |
|---|---|---|
| `pin_to_pin` (error) | LDO 的 VOUT 是 `power_out`，又给它加了 `PWR_FLAG`（也是 `power_out`），两个 power output 相连 | 只在**没有** `power_out` 驱动源的网络上加 PWR_FLAG |
| `pin_not_connected` (error) × 17 | 28 脚 MCU 的空置 GPIO 没有任何标记 | 每个未连接引脚画 `no_connect` |
| `endpoint_off_grid` (warning) × 21 | 坐标 48.0 之类不在 1.27 栅格 | 器件坐标与引脚端点全部吸附 1.27，单元格尺寸取 2.54 的整数倍 |
| `lib_symbol_issues` (warning) × 20 | 无头 ERC 只读全局符号库表，读不到工程目录的 `sym-lib-table` | 判为环境噪声；符号已内嵌在原理图 `lib_symbols`，不影响渲染与网表 |

> `lib_symbol_issues` 试过两种解法都无效：把 `sym-lib-table` 的 uri 改成绝对路径、
> 在目录里补 `.kicad_pro`。`kicad-cli` 的 ERC 不加载工程级库表。
> 因此代码里在 `parse_erc_json` 过滤该类，并在 README 的「已知限制」中公开说明。

网表一致性（最关键的一条）：从原理图导出的网表与 IR **11/11 网络、48/48 连接点完全一致**。

## 5. 常用命令

```bash
# 版本
kicad-cli --version

# 从原理图导出网表（用于和 IR 交叉校验）
kicad-cli sch export netlist --format kicadsexpr -o board.net board.kicad_sch

# 电气规则检查（注意：--format json 才机器可读）
kicad-cli sch erc --format json --output erc.json board.kicad_sch

# 渲染成矢量图，肉眼确认布局
kicad-cli sch export svg -o svg/ board.kicad_sch

# PCB 设计规则检查（W7 进阶任务用）
kicad-cli pcb drc --format json --output drc.json board.kicad_pcb

# 导出 PDF 便于报告插图
kicad-cli sch export pdf -o board.pdf board.kicad_sch
```

各小版本 flag 有差异，以 `kicad-cli sch erc -h` 为准。

## 6. 首次在 KiCad 中打开后的检查清单

1. 打开 `examples/board.kicad_sch`，确认没有「文件版本过旧」或「符号缺失」弹窗；
2. 器件应为矩形框 + 引脚名（引脚名来自本仓库器件库，不是官方图形库）；
3. 放大看引脚端点是否正好落在全局标签锚点上——错位说明 Y 轴换算方向反了，
   改 `render.py` 中 `wy = y - pin.py` 的符号；
4. 跑「检查 → 电气规则检查」，预期 0 error；
5. `kicad-cli sch export netlist` + `diff_with_ir` 对比，应完全一致
   （已由 `tests/test_kicad.py` 覆盖）。

## 7. 后续可做的增强

- 换用官方库符号（美观 + 引脚图形正确），只改 `render.py::emit_lib_symbol`；
- 引入 SKiDL 作为第二条独立生成路径，与 s-expression 路径做网表 diff，互为验证；
- 自动布局从「网格摆放」升级为「按功能分区 + 力导向」；
- 生成 `.kicad_pcb` 后接 `pcb drc` 闭环（进阶任务）。

## 8. IPC API（kicad-python / kipy）实测

结论一句话：**当前版本 IPC 只能替代「打开/推送」，替代不了「校验」**。
详细证据与三种打开方式的尝试见 `docs/report-material.md` 第 3 节。

- 连接与版本：`KiCad().get_version()` → `10.0.7 (10.0.7)`，`get_api_version()` → `10.0.6`
- 文档级请求：`get_open_documents()` / `get_board()` → `ApiError: no handler available`
- 对象模型：`kipy` 只暴露 `Board`（PCB），**没有 Schematic**，也就没有 ERC / 网表接口
- 处理器挂在编辑框上：没有打开文档时，只有全局接口（版本等）可用

所以本项目的 `PCB_KICAD_BACKEND` 保留三档：`cli`（默认，负责校验）、`live`（推送 + 状态上报）、
`off`（无 KiCad 环境）。想用 IPC 做原理图校验，得等 KiCad 上游补齐 schematic 侧的 API。
