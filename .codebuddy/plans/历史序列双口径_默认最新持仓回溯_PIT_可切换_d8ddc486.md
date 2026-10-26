---
name: 历史序列双口径：默认最新持仓回溯，PIT 可切换
overview: 历史估值序列（etf_daily/图表/AI报告）改为双口径：默认「最新持仓回溯」（所有历史日期统一用最新一期持仓权重+PIT财报/行情，消除调仓噪音，柱子间可比、净利与增速分母自洽），PIT 持仓（现行逻辑）作为可切换的对照口径保留。存储层加 basis 列区分两套序列，回填双写，前端图表下拉与 AI 报告支持口径切换，默认最新持仓。CALC_VERSION 升 10。
todos:
  - id: store-basis
    content: store.py：重建 etf_daily 主键含 basis（老数据迁为 pit），etf_calc_version 双口径版本，record_daily/get_history/clear_daily 带 basis 默认 fixed
    status: completed
  - id: backfill-dual
    content: peg.py：回填一次循环双写 fixed/pit 两套指标，fixed 固定最新持仓不裁起点，CALC_VERSION 升 10
    status: completed
    dependencies:
      - store-basis
  - id: api-cli
    content: server.py：/api/history 加 basis 参数，_auto_backfill_history 检查两套增量；run.py 输出与 --basis 参数同步
    status: completed
    dependencies:
      - store-basis
  - id: frontend-switch
    content: web/index.html：图表口径切换下拉默认最新持仓，口径说明文案按 basis 区分
    status: completed
    dependencies:
      - backfill-dual
      - api-cli
  - id: ai-report-basis
    content: web/index.html：AI 报告历史月度表用 fixed 序列，口径章节注明前视性质与 PIT 切换建议
    status: completed
    dependencies:
      - backfill-dual
      - api-cli
  - id: verify-readme
    content: 重算 013816/013817 验证 fixed 口径净利柱子与增速分母自洽（13.8 亿/8.8 亿/-36%），A/C 对比 0 差异，README 补双口径文档
    status: completed
    dependencies:
      - frontend-switch
      - ai-report-basis
---

## 用户需求

历史估值序列改为**双口径**，默认使用「最新持仓回溯」：

- **主口径（fixed，默认）**：全部历史日期统一用最新一期持仓权重（财报/行情仍按披露日 PIT 对齐），消除调仓噪音——柱子之间可比、增速分母与净利柱子自洽，回答「当前这篮子股票历史上贵不贵」
- **副口径（pit，保留）**：现行逻辑（每个日期用当时已知的持仓报告期），供历史复盘/回测场景，无前视偏差
- 前端图表与 AI 报告默认 fixed，可切换 PIT；口径含义需在界面与文档中说明

## 背景问题（要解决的用户困惑）

013816 的 2026-08 增速 -36%，但归母净利柱子从 2025-08 的 0.6 亿涨到 8.8 亿——因为 PIT 口径下两年用的不是同一套持仓（53 只 vs 69 只），增速分母（69 只的 2025H1 TTM ≈ 13.8 亿）不等于去年柱子（53 只的 0.6 亿）。fixed 口径下三个数字互相印证，困惑消除。

## 核心功能

1. `etf_daily` 支持 basis 维度（fixed/pit 两套序列并存，独立增量）
2. 回填一次循环内双写两套指标（PE/增速/PEG/扣非三件套/归母净利/PB/PS/股息率）
3. 图表口径切换下拉（默认「最新持仓回溯」）、口径说明文案、AI 报告默认 fixed 并标注前视性质
4. CALC_VERSION 升 10 触发全量重算

## Tech Stack

沿用现有栈：Python 标准库 http.server + SQLite + 原生 HTML/JS 前端，零新依赖。

## 实现方案

### 1. 存储层双口径改造（`etf_peg/store.py`）

**表结构迁移**（一次性，老库自动执行）：

- `etf_daily` 是 `WITHOUT ROWID` 表且主键 `(d, etf_code)`，同一日期两套口径会主键冲突，必须重建表把 `basis` 纳入主键：

```sql
CREATE TABLE etf_daily_v2 (...同列..., basis TEXT NOT NULL DEFAULT 'pit',
PRIMARY KEY (d, etf_code, basis)) WITHOUT ROWID;
INSERT INTO etf_daily_v2 SELECT d,...,全部列..., 'pit' FROM etf_daily;
DROP TABLE etf_daily; ALTER TABLE etf_daily_v2 RENAME TO etf_daily;
```

- 检测方式沿用现有 `PRAGMA table_info` 模式（约 271-280 行）：老库无 `basis` 列即触发迁移；1074 行数据瞬间完成
- `etf_calc_version` 同样重建为 `(etf_code, basis)` 主键，两套序列版本独立、增量互不干扰
- `record_daily`：行 dict 带 `basis` 字段写入
- `get_history(etf_code, days, basis='fixed')`：WHERE 加 basis 过滤，**默认 fixed**
- `clear_daily(etf_code)`：清两套（口径变更时用）
- 历史分位列（pe_pct 等）在 fixed/pit 下分别计算，随各自序列写入

### 2. 回填双口径计算（`etf_peg/peg.py`）

- `backfill_etf`（约 1460-1929 行）：
- 循环前确定 `rep_fixed = max(rep_dates)`（最新一期有持仓的报告期）与 `wmap_fixed`
- 循环内（1787 行处分支）：PIT 用 `_latest_known`；fixed 固定 `rep_fixed`
- **一次循环对两套 wmap 各算一遍**全部指标（`pit_as_of` 时间线已构建，纯内存查找，零新增网络请求），每天输出两行（basis='fixed' 与 'pit'）
- 起点裁剪（1541-1553 行）：fixed 不裁（PE 从 2018-01-02 起），todo_dates 取两套并集；早于 PIT 起点的日期只写 fixed 行
- 财报深度 `n_quarters` 按 fixed 起点计算（2018 起约 34 期，全部本地库命中）
- 次新股早期无数据由现有 `pit_as_of` 自动处理，覆盖率如实反映
- `CALC_VERSION` 9 → 10：注释写明双口径动机（013816 增速-36% vs 净利 0.6→8.8 亿的反直觉现象）
- `aggregate_history`：无需改（操作已按 basis 过滤的 rows）
- 增量判定：两套分别检查日期覆盖，任一缺失则补齐该套；版本落后则两套全量重算

### 3. API 与 CLI（`etf_peg/server.py`、`run.py`）

- `/api/history`（约 539-557 行）：加 `basis` 参数（默认 fixed），透传 `get_history`
- `_auto_backfill_history`（约 206-225 行）：检查两套序列完整性与两套版本号
- 回填 job（`/api/job/start` kind=backfill）：双写，进度条共用
- `run.py`：`--backfill` 输出两套行数；`--history` 加 `--basis` 参数

### 4. 前端（`web/index.html`）

- 图表选择区（约 314-328 行）加口径下拉：`最新持仓回溯（默认）` / `PIT 持仓`，`loadHist` 请求带 basis，重绘
- 图表口径说明（约 1990-2005 行）按 basis 显示：fixed 强调「全部历史用最新持仓，消除调仓噪音；持仓有前视，财报/行情仍按披露日」；pit 强调「每期用当时持仓，无前视，柱子间含调仓影响」
- AI 报告（`ensureAiHist` 约 1797-1805 行 + 历史月度表口径注释）：拉 fixed 序列，口径章节写明「历史序列为最新持仓回溯口径：净利柱子、增速分母同一套持仓，可直接相除；持仓存在前视（2022 年的组合并非当时实际持有），不适合回测，复盘当时估值请切换 PIT 口径」
- 历史分位卡片默认读 fixed 序列的分位

### 5. 验证与文档

- 重算 013816/013817 全历史，验证 fixed 口径下：2025-08 柱子 ≈ 13.8 亿（69 只的 2025H1 TTM）、2026-08 柱子 8.8 亿、增速 -36% 三者自洽
- A/C 份额 fixed 序列逐日对比（应 0 差异）
- README 历史 PEG 章节补双口径说明、前视偏差边界、两种口径的适用场景

## 性能与可靠性

- 零新增网络请求；回填耗时约从 90s 增至 2~3 分钟（两套遍历 + fixed 多回溯约 1000 个交易日），仅在 v10 升版时发生一次
- 表迁移幂等（PRAGMA 检测），老数据全部保留为 pit 序列，不丢失
- 两套版本独立，避免一套重算连带另一套

## 架构原则

- fixed 与 pit 共用同一组时间线/行情/分红数据，仅持仓映射不同（DRY）
- 存储层 basis 是第一维度，所有历史读取默认 fixed——前端、AI 报告、CLI 一处定义、处处一致
- 前视偏差显式声明而非隐藏：界面与 AI 报告都写明 fixed 的性质与不适用场景（回测）