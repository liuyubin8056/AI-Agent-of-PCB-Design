# KiCad 8 集成笔记

记录踩过 / 预判过的问题，避免 W5–W6 集成阶段重复试错。

## 1. 环境事实

| 事实 | 影响 |
|---|---|
| KiCad 8 自带 `kicad-cli` | 网表、ERC、DRC 全部可命令行化，是闭环的前提 |
| `pcbnew` 有 Python 绑定，`eeschema` **没有** | 「用 Python 脚本画原理图」只能走 s-expression 或 SKiDL 两条路 |
| `.kicad_sch` 是 s-expression，首行版本号 `20231120` 对应 KiCad 8 | 版本号写错，KiCad 会直接拒绝打开并提示版本不兼容 |
| 原理图与符号库的 Y 轴方向相反 | 器件放在 (X, Y) 后，引脚世界坐标是 `(X + px, Y - py)` |

## 2. 本项目采用的路线

**自己生成 `.kicad_sch`**，理由：

1. 不依赖本机是否装了 KiCad（CI、答辩机、助教机器都能跑）；
2. 引脚编号由器件库直接决定，与网表 100% 对齐，不会出现「符号引脚和网表对不上」；
3. 可以精确控制坐标，天然可复现。

代价：图形从简（矩形框 + 引脚）。后续若要更好看，可以把 `lib_symbols` 换成
KiCad 官方库的符号定义，`render.py` 的 `emit_lib_symbol` 是唯一改动点。

**连接方式：全局标签代替 wire。**
在 KiCad 中，把 global label 放在引脚端点上即视为电气相连。因此整张图不需要任何
`wire` 元素，也就不存在「器件重叠导致连线错乱」这个自动生成原理图最头疼的问题。
代价是 ERC 会看到一堆标签，图面信息密度高于手绘。

## 3. 常用命令

```bash
# 版本
kicad-cli --version

# 从原理图导出网表（用于和 IR 交叉校验）
kicad-cli sch export netlist --format kicadsexpr -o board.net board.kicad_sch

# 电气规则检查
kicad-cli sch erc --format json --output erc.json board.kicad_sch

# PCB 设计规则检查（W7 进阶任务用）
kicad-cli pcb drc --format json --output drc.json board.kicad_pcb

# 导出 PDF / SVG 便于报告插图
kicad-cli sch export pdf -o board.pdf board.kicad_sch
```

各小版本 flag 可能有差异，以 `kicad-cli sch erc -h` 的输出为准。
`kicad_gate.py` 里的参数按 KiCad 8.0 编写，若报参数错误先查帮助。

## 4. 首次在 KiCad 中打开后的检查清单

1. 打开 `examples/board.kicad_sch`，确认没有「文件版本过旧」弹窗；
2. 看器件是否都显示为矩形框 + 引脚名（引脚名来自我们的器件库，不是官方图形库）；
3. 放大看引脚端点是否正好落在全局标签的锚点上——错位说明 Y 轴换算方向反了，
   改 `render.py` 中 `wy = y - pin.py` 的符号；
4. 跑 `检查 -> 电气规则检查`，预期只剩「引脚未连接」类警告；
5. 跑 `kicad-cli sch export netlist`，用 `diff_with_ir` 与 IR 对比，应完全一致。

## 5. 后续可做的增强

- 换用官方库符号（美观 + 引脚图形正确）；
- 引入 SKiDL 作为第二条独立生成路径，与 s-expression 路径做网表 diff，互为验证；
- 自动布局从「网格摆放」升级为「按功能分区 + 力导向」；
- 生成 `.kicad_pcb` 后接 DRC 闭环（进阶任务）。
