---
name: 新增扣非PEG与归母净利润曲线
overview: 在现有归母口径 PEG 之外，新增一套独立的扣非 PEG（扣非整体法PE ÷ 全口径加权扣非增速），并在历史图表/AI报告中并列展示归母净利润绝对额曲线（与现有扣非净利曲线对称）。涉及计算引擎、存储层（新列迁移）、回填、前端指标卡/图表/tooltip、AI报告共 5 处，升 CALC_VERSION 强制重算历史。
todos:
  - id: store-columns
    content: store.py：etf_daily 加 np_ttm/deduct_pe/g_deduct/deduct_peg 四列（建表+迁移+record_daily+get_history）
    status: completed
  - id: calc-engine
    content: peg.py：EtfResult 加 deduct_pe/g_deduct_ttm/deduct_peg 字段，analyze_etf 对称新增扣非PE/扣非增速/扣非PEG计算
    status: completed
    dependencies:
      - store-columns
  - id: backfill-v9
    content: peg.py：回填循环加归母/扣非累加与扣非PE累加，输出四新键，CALC_VERSION 升 9，aggregate_history 白名单扩列
    status: completed
    dependencies:
      - calc-engine
  - id: frontend-ui
    content: web/index.html：指标卡加归母净利/扣非PE/扣非增速/扣非PEG，图表下拉加归母净利与扣非增速，口径说明与 tooltip 同步
    status: completed
    dependencies:
      - backfill-v9
  - id: ai-report
    content: web/index.html：AI 报告核心结论表加行，口径章节加扣非 PEG 分工说明；run.py CLI 输出同步
    status: completed
    dependencies:
      - backfill-v9
  - id: verify
    content: 重算 013817 全历史验证：扣非PEG 在扣非增速>0 时段有值、归母净利曲线与扣非净利对称、README 补充口径文档
    status: completed
    dependencies:
      - frontend-ui
      - ai-report
---

## 用户需求

在现有归母口径估值体系之外，新增一套**扣非 PEG**，并把**归母净利润绝对额**也加入展示：

1. **扣非 PEG** = 扣非整体法 PE ÷ 扣非全口径加权增速（专业机构对制造业更倾向扣非口径）
2. **归母净利润绝对额**：与现有「扣非净利(亿)」曲线并列的归母 TTM 净利曲线
3. 与现有归母 PEG **并列展示而非替换**（光伏当前扣非大面积亏损，扣非 PEG 会长期无值）

## 口径规则（延续已确认的全口径加权原则）

- 扣非增速 = `Σ(w·扣非TTM) / Σ(w·上年扣非TTM) − 1`，全口径不筛样本；港股/缺扣非用归母顶上（与现有 † 口径一致）
- 扣非 PE = 整体法 `Σw / Σ(w/PE_i)`，`PE_i = 市值 ÷ 扣非TTM`，只统计扣非TTM>0 的股票
- 扣非 PEG 仅在扣非增速 > 0 时有值；为空显示 `—` 并说明原因
- 归母净利绝对额 = `Σ(w·归母TTM)`，与 `deduct_ttm_sum` 完全对称
- 历史序列（图表/AI 报告）同步支持：归母净利曲线、扣非增速曲线、扣非 PEG 曲线

## 产品效果

- 主界面指标卡新增：归母净利(亿)、扣非PE、扣非增速%、扣非PEG
- 历史图表指标下拉新增：「归母净利(亿)」「扣非增速%」（可选「扣非PEG」）
- 「复制给 AI」报告核心结论表与口径章节同步新增，并说明扣非 PEG 与归母 PEG 的适用分工（制造业看扣非，金融/港股看归母）

## 技术方案

### 数据流（复用现有管道）

`build_pit_timelines` 已产出每只股票的 `deduct_ttm` / `deduct_ttm_prev`（TTM 元组位置 7/8）与 `np_ttm` / `np_ttm_prev`（位置 1/2），数据完全就绪，**无需改取数层**。改动集中在计算、存储、展示三层。

```mermaid
```

build_pit_timelines（不动）
│  np_ttm / np_ttm_prev / deduct_ttm / deduct_ttm_prev + 市值
├─ analyze_etf（实时）
│    ├─ 归母 PE（已有）+ 归母 g_ttm（已有）
│    ├─ 归母净利 Σ(w·np)            [新] → EtfResult.np_ttm_sum（已有字段，改语义）
│    ├─ 扣非 PE  Σw/Σ(w·市值/扣非)   [新] → EtfResult.deduct_pe
│    ├─ 扣非增速 Σ(w·dnp)同比        [新] → EtfResult.g_deduct_ttm
│    └─ 扣非 PEG                     [新] → EtfResult.deduct_peg
├─ 回填路径（逐日，对称新增同一组累加）
│    └─ etf_daily 新列：np_ttm / deduct_pe / g_deduct / deduct_peg
└─ 前端 + AI 报告（并列展示，增速≤0 显示 —）
```

### 关键实现点

1. **EtfResult 扩展**（`etf_peg/peg.py` dataclass）：新增 `deduct_pe`、`g_deduct_ttm`、`deduct_peg` 三字段；`np_ttm_sum` 已存在（当前存加权归母合计），保持语义并直接输出到前端。

- 扣非口径缺数据时的替代值必须与 `deduct_ttm_sum` 一致：港股/缺扣非用归母顶上，保证扣非 PE 与扣非增速、扣非净利三者同一批口径可比。
- **注意**：扣非 PE 只统计「扣非TTM>0」的股票，若某股票扣非为负但归母为正（顶上后仍为负），不进扣非 PE 分子分母，但照常进扣非增速——两者样本天然不同，需在口径说明中写明。

2. **analyze_etf 计算**（约 1111-1142 行旁对称新增）：

- `deduct_pe`：循环 rows，`w>0 且 dnp_ttm>0 且 mktcap>0` 时 `num+=w; den+=w·dnp/mktcap`，`deduct_pe = num/den`
- `g_deduct_ttm`：`Σ(w·dnp)/Σ(w·dnp_prev)−1`，dnp 缺失时用 np 顶上（与 deduct_ttm_sum 同规则）
- `deduct_peg`：`deduct_pe / g_deduct_ttm`，仅 `g_deduct_ttm > 0` 时有值

3. **回填路径**（约 1730-1850 行）：循环内已有 `dnp_all`（扣非加权累加），对称新增：

- `np_all += (w/100)·np_ttm`（归母加权合计，落库 `np_ttm` 列）
- `dnp_now2/dnp_pre2 += (w/100)·(dnp 或 np 顶上)`（扣非增速累加）
- `dpe_num/dpe_den += w / (mktcap/dnp)`（扣非 PE 累加，仅 dnp>0）
- 输出 dict 新增 `np_ttm`、`deduct_pe`、`g_deduct`、`deduct_peg` 四键
- `CALC_VERSION = 8 → 9`，触发全量重算（沿用既有升版机制，零额外代码）

4. **存储层**（`etf_peg/store.py`）：

- 建表语句加 4 列（新库）
- 迁移：仿照 265-280 行 `PRAGMA table_info(etf_daily)` + `ALTER TABLE ADD COLUMN` 循环，对 `np_ttm/deduct_pe/g_deduct/deduct_peg` 逐一补列
- `record_daily`（978-999 行）INSERT OR REPLACE 列清单与取值加 4 项
- `get_history`（1014-1024 行）SELECT 与 dict 映射加 4 项
- `aggregate_history`（peg.py 约 1360-1390 行）桶聚合的 fields 白名单加 4 个新字段（月末值口径与其他指标一致）
- **注意**：`deduct_peg`/`g_deduct` 在桶内多数日期为 NULL，聚合取「最后一个非空值」需与现有 `cur[f] = r[f]` 逻辑一致（已是非空才覆盖，天然正确）

5. **前端**（`web/index.html`）：

- 指标卡 items（约 723-744 行）：`PEG（扣非口径）`（hero 可选高亮）、`扣非PE（整体法）`、`扣非增速%（全口径加权）`、`归母净利(亿)（全口径加权）`，增速≤0 或扣非为负时说明文案给出解释
- 图表指标下拉（约 318-324 行）：新增 `np_ttm 归母净利(亿)`、`g_deduct 扣非增速%`；`MONEY` 映射加 `np_ttm: 1e8`；`MNAME` 加名称
- 图表口径说明（约 1949-1971 行）：扣非增速为全口径加权、扣非 PEG 仅增速>0 有值、归母净利与扣非净利同规则
- AI 报告（buildAiReport）：核心结论表加 3-4 行（归母净利、扣非PE、扣非增速、扣非PEG），口径章节新增「扣非 PEG 的分工」段落（制造业/周期股看扣非剔除一次性损益；金融/港股用归母；两者背离时警惕非经常性损益撑利润）
- 历史 AI 表（约 1676-1697 行）：如加列则表头与月度说明同步（建议核心结论表加行即可，历史月度表保持宽度不膨胀，扣非 PEG 曲线看图表）

6. **CLI**（`run.py`）：输出加扣非 PE / 扣非增速 / 扣非 PEG 三行，格式仿现有 `r.g_all` 写法（None 时 N/A）

7. **README**：PEG 公式章节（约 115-145 行）补扣非 PEG 公式与适用边界；历史 PEG 口径章节（约 318-350 行）补归母净利与扣非增速曲线说明

### 性能与可靠性

- 全部为已有数据的衍生计算，**零新增网络请求**；回填重算约 90 秒（实测 1074 交易日），只在 v9 升版时发生一次
- 老库迁移：`ALTER TABLE ADD COLUMN` 幂等（PRAGMA 判断列存在），旧数据新列为 NULL，升版重算自动补齐——与 v5 加 `deduct_ttm` 列时的机制完全相同，已被验证
- 前端无新依赖，纯数据字段扩展

### 设计原则

- 扣非体系与归母体系**完全对称、独立成组**，不做混合口径（SoC）；两者共用同一组 PIT 报告期，保证可对照（DRY）
- 不动归母 PEG 主指标，向后兼容（blast radius 最小化）
- 所有新增指标在 AI 报告/图表说明中显式标注口径与无值原因，避免 AI 或用户把「—」误读为数据缺失