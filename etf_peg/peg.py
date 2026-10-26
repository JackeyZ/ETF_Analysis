# -*- coding: utf-8 -*-
"""
PEG 计算引擎。

设计要点
--------
1) PE 用「整体法」而非算术平均
   算术平均会被微利股 / 亏损股严重污染（PE=1000 和 PE=10 平均是 505，毫无意义）。
   整体法：PE_组合 = Σ总市值 / Σ归母净利润(TTM)，等价于按市值加权的调和平均，
   是指数公司（中证、国证）与 Wind 的通行口径。

2) 增速 G 用 TTM 归母净利润同比
   TTM_本期 = 本期累计归母 + 上年年报归母 - 去年同期累计归母
   再与「去年同期 TTM」比，避免单季度波动和一季报的季节性失真。
   同时保留东财 SJLTZ（最新报告期累计同比）作为「高灵敏度」口径供切换。

3) PEG = PE_组合 / G_组合（G 为百分数，如 25 表示 25%）
   G <= 0 或 PE <= 0 的成分股不进入加权，但要统计其权重占比作为「覆盖率」，
   覆盖率过低时结果不可信，需在 UI 上警示。
"""
from __future__ import annotations

import math
import re
import threading
from bisect import bisect_right
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import datasource as ds
from . import bond
from . import forecast
from . import hk
from . import store


# 计算口径版本：PE/PEG 等算法发生不兼容变更时 +1。
# 回填时若库里该 ETF 的版本低于此值，会自动 force 重算全部历史
# （增量判定只看「日期在不在」，不会重算已存日期的口径）。
#   v1: 早期版本 —— PE 只算「正增长有效样本」（PE>0 且增速>0）
#   v2: 整体法 PE 改用「所有盈利股」（不筛增速），与指数官方口径对齐
#   v3: 回填路径的 PB/PS 也改用「所有有数据的股票」（原来被增速筛选挡在后面，
#       PS 只统计正增长股，导致历史表 PS 3.11 vs 实时 1.27 的矛盾）
#   v4: 半年报/年报改用真正的全持仓。旧逻辑抓 (year, 6) 时上游会返回当年年报的
#       前十大，被当成全持仓存进该期并挡住后续 (year, 12) 的全量抓取，
#       于是那些年份的整体法 PE 是按前十大权重算的（515370 / 512480 等实测）
#   v5: 每日快照新增 deduct_ttm（TTM 扣非归母净利润合计）。老日期该列为 NULL，
#       靠升版强制重算全部历史补齐，否则「扣非净利」曲线只有最近几天有值
#   v6: 回填路径的「有效样本」补上 PE>0 筛选，与实时分析、前端文案
#       （"样本与 PE 相同：PE>0 且增速>0"）三者统一。v5 只筛了增速>0，
#       「亏损收窄」的公司（TTM 同比为正但绝对亏损）也进了样本，把巨额
#       负扣非计入合计——光伏 ETF(013817) 实测：2025 年报披露后 deduct_ttm
#       从 +96 亿跳到 -89 亿，全是隆基/TCL中环/天合/晶澳等亏损收窄股；
#       且与当日实时分析（+211 亿）口径不一致，同一条曲线一天跳 332 亿。
#   v7: 「扣非净利」改为**全口径 + 持仓权重加权**：
#       - 不筛样本：亏损股、负增长股全部计入（v6 的 PE>0且g>0 只用于 PE/PEG）；
#       - 乘持仓权重：净利润是绝对额，直接相加等于假设每只都 100% 持有，
#         013817 实测简单相加 165 亿 vs 按权重折算十几亿，差一个量级。
#   v8: 组合增速改为**全口径 + 持仓权重加权**：g = Σ(w·np)/Σ(w·np_prev) − 1。
#       亏损股、负增长股不再剔除（旧 v6 口径只留 PE>0且g>0，增速被系统性抬高），
#       且乘持仓权重（旧口径是等权简单相加）。g_all 保留为等权简单相加对照，
#       用于衡量持仓集中度的影响。PEG 仍只在加权增速 > 0 时有值。
#       013817 实测：正增长样本 +26.2% vs 全样本等权 -108.1%。
#   v9: 新增**扣非 PEG** 独立体系 + 归母净利润绝对额：
#       - deduct_pe = Σw / Σ(w·扣非TTM/市值)，只统计扣非TTM>0 的股票；
#       - g_deduct  = Σ(w·扣非TTM)/Σ(w·上年扣非TTM) − 1，全口径加权；
#       - deduct_peg = deduct_pe / g_deduct，仅 g_deduct>0 时有值；
#       - np_ttm = Σ(w·归母TTM)，与 deduct_ttm 完全对称，供绝对值曲线。
#       扣非与归母两套并存：制造业看扣非（剔除一次性损益），金融/港股看归母。
#   v10: 历史序列改**双口径**（etf_daily 主键加 basis）：
#        - fixed（默认）：全部历史用**最新一期持仓**回溯，财报/行情仍 PIT。
#          柱子之间可比、增速分母与净利同源，消除调仓噪音；
#          代价是持仓有前视（不适合回测，只适合判断「当前这篮子贵不贵」）。
#        - pit：每期用当时已知持仓（旧口径），无前视但含调仓影响。
#        背景：013816 的 2026-08 增速 -36% 而净利柱子从 0.6 亿涨到 8.8 亿——
#        PIT 下两年用的不是同一套持仓（53 只 vs 69 只），分母 13.8 亿≠去年柱子。
CALC_VERSION = 10


# --------------------------------------------------------------------------- #
#  数据结构
# --------------------------------------------------------------------------- #
@dataclass
class StockRow:
    code: str
    name: str
    weight: Optional[float] = None        # 占 ETF 净值比例 %
    price: Optional[float] = None
    pe_ttm: Optional[float] = None
    mktcap: Optional[float] = None        # 总市值（元）
    np_ttm: Optional[float] = None        # TTM 归母净利润（元）
    np_ttm_prev: Optional[float] = None   # 去年同期 TTM 归母净利润（元）
    # 扣非口径：与归母净利润用完全相同的一组报告期滚动，保证两者可比。
    # A 股有数据；港股/美股没有「扣非」概念，保持 None。
    deduct_ttm: Optional[float] = None      # TTM 扣非归母净利润（元）
    deduct_ttm_prev: Optional[float] = None # 去年同期 TTM 扣非归母净利润（元）
    g_ttm: Optional[float] = None         # TTM 归母同比 %
    g_latest: Optional[float] = None      # 最新报告期累计同比 %（东财口径）
    g_deduct: Optional[float] = None      # TTM 扣非同比 %（港股无此口径，保持 None）
    peg: Optional[float] = None
    # 本行计算依据：用的是哪一期财报（含"2026 年半年报"这类可读名）、何时披露
    fin_period: Optional[str] = None
    fin_label: Optional[str] = None
    fin_date: Optional[str] = None
    market: Optional[str] = None      # 市场标识：1/0=A股，116=港股，105/106/107=美股
    # 近若干期的增速（与 fin_basis.growth_series 的期别一一对应）
    g_series: Optional[List[Optional[float]]] = None
    # 近若干期的 TTM 环比（本期 TTM 归母净利 vs 上一期 TTM 归母净利，与 g_series 同期别）
    qoq_ttm_series: Optional[List[Optional[float]]] = None
    # 近若干期的单季环比（单季归母净利 vs 上一单季，仅 A 股成分，与 g_series 同期别）
    qoq_single_series: Optional[List[Optional[float]]] = None
    # 近若干期的 TTM 扣非同比（与 g_series 同期别；港股无扣非口径，整列为 None）
    g_deduct_series: Optional[List[Optional[float]]] = None
    # 近若干期的财务快照（累计口径，与 g_series 同期别一一对应）
    fin_series: Optional[List[Optional[dict]]] = None
    # ---- 多指标估值扩展（一期）----
    pb: Optional[float] = None          # 市净率（A 股东财 PB_MRQ；港股 市值/净资产）
    ps: Optional[float] = None          # 市销率（市值 / TTM 营收）
    pcf: Optional[float] = None         # 市现率（A 股东财 PCF_OCF_TTM）
    equity: Optional[float] = None      # 最新已披露归母净资产（元）
    rev_ttm: Optional[float] = None     # TTM 营业收入（元）
    op_ttm: Optional[float] = None      # TTM 营业利润（元，EV/EBIT 用）
    ev: Optional[float] = None          # 企业价值 = 市值 + 总负债 − 现金（元，近似）
    div_yield: Optional[float] = None   # 个股股息率 %
    status: str = "ok"                    # ok / no_quote / no_pe / no_growth / no_data


@dataclass
class ETFResult:
    code: str
    name: str
    report_date: Optional[str]
    fin_period: Optional[str]
    holdings_full: bool = True      # False = 只拿到季报前十大，样本不完整
    pe_date: Optional[str] = None   # PE 数据日期（PE-TTM 口径，滞后 1~2 个交易日）
    hk_pe_date: Optional[str] = None  # 港股行情快照日期（A 股估值库不含港股，单独标注）
    fin_basis: Optional[dict] = None  # 增速的计算依据：用到了哪几份报告
    growth_series: Optional[List[dict]] = None   # 近 N 个报告期各自的组合增速
    # 普通（场外）基金相关
    fund_type: Optional[str] = None    # 基金类型，如 混合型-偏股 / 指数型-股票
    equity_ratio: Optional[float] = None   # 股票仓位 = Σ 持仓权重（%）
    n_a: int = 0                       # A 股只数
    n_hk: int = 0                      # 港股只数（自己算 PE：市值 ÷ TTM 股东应占溢利）
    hk_weight: Optional[float] = None  # 港股权重合计（%）
    n_oversea: int = 0                 # 其余（美股等）只数：确实无法估值
    oversea_weight: Optional[float] = None   # 其余权重合计（%）
    n_total: int = 0
    n_valid: int = 0
    coverage: float = 0.0                 # 有效样本权重覆盖率 %
    pe: Optional[float] = None            # 整体法 PE
    g_ttm: Optional[float] = None         # 整体法 TTM 净利增速 %（正增长样本）
    g_latest: Optional[float] = None      # 整体法 最新报告期累计增速 %（正增长样本）
    g_all: Optional[float] = None         # 全样本（含负增长）整体法 TTM 增速 %，用于衡量剔除偏差
    peg: Optional[float] = None           # pe / g_ttm
    peg_median: Optional[float] = None    # 稳健参考：PE中位数 / 增速中位数
    peg_weighted_avg: Optional[float] = None  # 参考：成分股 PEG 的权重加权平均
    np_ttm_sum: Optional[float] = None
    np_ttm_prev_sum: Optional[float] = None
    # TTM 扣非归母净利润合计（元），与 np_ttm_sum 同一批样本；
    # 港股不披露扣非，该部分用归母净利润顶上（与成分股表的 † 口径一致）
    deduct_ttm_sum: Optional[float] = None
    # ---- 扣非 PEG 三件套（v9）：与归母体系完全对称、独立成组 ----
    # 扣非 PE 只统计扣非TTM>0 的股票；增速全口径加权；PEG 仅增速>0 有值
    deduct_pe: Optional[float] = None       # 整体法扣非 PE = Σw / Σ(w·扣非/市值)
    g_deduct_ttm: Optional[float] = None    # 全口径加权扣非增速 %
    deduct_peg: Optional[float] = None      # deduct_pe / g_deduct_ttm
    mktcap_sum: Optional[float] = None
    warn: str = ""
    stocks: List[dict] = field(default_factory=list)
    updated_at: str = ""
    # ---- 多指标估值扩展（一期）----
    pb: Optional[float] = None            # 整体法 PB = Σw / Σ(w/PB_i)
    pb_coverage: Optional[float] = None   # 参与 PB 计算的权重覆盖 %
    ps: Optional[float] = None            # 整体法 PS
    ps_coverage: Optional[float] = None
    div_yield: Optional[float] = None     # 组合股息率 %（权重加权）
    ev_ebit: Optional[float] = None       # EV/EBIT（近似口径：EBIT=TTM营业利润，仅港股成分）
    pcf: Optional[float] = None           # 整体法市现率（A 股 EV 缺材料的现金流补充）
    pe_equal: Optional[float] = None      # 等权 PE（有效样本算术平均）
    dist: Optional[dict] = None           # 估值分布：正负样本分开的 PE/增速统计
    loss_weight: Optional[float] = None   # 亏损股合计权重 %
    negg_weight: Optional[float] = None   # 负增长股合计权重 %
    peg_all: Optional[float] = None       # 等权 PEG = 整体法PE / 等权增速（权重口径对照）
    percentile: Optional[dict] = None     # 历史分位 {pe/pb/ps: {all/y5/y3: 0~100}}
    forward: Optional[dict] = None        # 前瞻估值 {pe_f, peg_f, cagr, cons_weight, ...}
    erp: Optional[dict] = None            # 股债性价比 {value, y10, y10_date, pct}
    feeder_from: Optional[dict] = None    # 联接基金穿透：{code, name} 表示本结果由该联接基金穿透而来


# --------------------------------------------------------------------------- #
#  TTM 净利润构造
# --------------------------------------------------------------------------- #
def _np(period_data: Dict[str, Dict[str, dict]], code: str, key: str) -> Optional[float]:
    row = period_data.get(code)
    if not row:
        return None
    return row.get(key)


_QNAME = {1: "一季报", 2: "半年报", 3: "三季报", 4: "年报"}


def report_label(period: str) -> str:
    """2026-06-30 → 2026 年半年报"""
    y, m, _ = period.split("-")
    return f"{y} 年{_QNAME[{3: 1, 6: 2, 9: 3, 12: 4}[int(m)]]}"


def ttm_basis(latest: str) -> List[dict]:
    """
    构造 TTM 用到了哪几份报告、各自扮演什么角色。
    用于把计算依据透明地展示给用户——增速究竟是哪几份报告算出来的。
    """
    if not latest:
        return []
    ly, lq = _parse_period(latest)

    def part(y, q, role):
        return {"period": ds.period_key((y, q)), "label": report_label(ds.period_key((y, q))),
                "role": role}

    if lq == 4:                       # 年报本身即 TTM，直接与上年年报比
        return [part(ly, 4, "本期（年报即 TTM）"),
                part(ly - 1, 4, "去年同期（上年年报）")]
    return [
        part(ly, lq, "本期累计"),
        part(ly - 1, 4, "上年年报"),
        part(ly - 1, lq, "去年同期累计"),
        part(ly - 2, 4, "前年年报"),
        part(ly - 2, lq, "前年同期累计"),
    ]


def _acc_ttm(vals: Dict[Tuple[int, int], float],
             ly: int, lq: int) -> Tuple[Optional[float], Optional[float]]:
    """
    累计值 → (本期TTM, 去年同期TTM)。
    净利 / 营收 / 营业利润等「累计披露」的科目共用同一个公式，
    缺任一依赖期返回 (None, None)。
    """
    cur = vals.get((ly, lq))
    prev = vals.get((ly - 1, lq))
    if cur is None or prev is None:
        return None, None
    if lq == 4:                           # 年报本身即 TTM
        return cur, prev
    ann1 = vals.get((ly - 1, 4))
    prev2 = vals.get((ly - 2, lq))
    ann2 = vals.get((ly - 2, 4))
    if ann1 is None or prev2 is None or ann2 is None:
        return None, None
    return cur + ann1 - prev, prev + ann2 - prev2


def _disclosure_date(row: dict) -> Optional[str]:
    """
    该行财报的「可用日」：公告日与上游入库时间戳里较早的那个。

    A 股盘后披露很常见（18:00 前后整批出报），交易所口径会把公告日记成次日，
    但财报当晚就已公开。只认公告日的话，披露当晚的分析仍会把重仓股算成上一期
    （药明康德 2026 三季报：公告日 10-27，实际 10-26 17:15 已入库）。
    两者取早，既不漏当晚，也不会早于真实公开时间。
    """
    nd = row.get("notice_date")
    ts = (row.get("notice_ts") or "")[:10] or None
    if nd and ts:
        return min(nd, ts)
    return nd or ts


def build_pit_timelines(fin: Dict[str, Dict[str, dict]],
                        codes: List[str]) -> Dict[str, List[tuple]]:
    """
    为每只股票构建 TTM 时间线，实现 PIT（point-in-time）对齐。

    返回 {code: [(生效日, np_ttm, np_ttm_prev, g, 基准期,
                  rev_ttm, op_ttm, deduct_ttm, deduct_ttm_prev), ...]}（按生效日升序）

    「生效日」= 构造这个 TTM 所需全部报告的**最晚真实披露日**。
    例如某公司 7/28 就披露了半年报，那它的 TTM 从 7/28 起就可用，
    不必等到 8/31 的法定截止日；反之拖到 8/28 披露的，就按 8/28。

    这样每家公司各自对齐，不会出现「用 A 公司 7 月就公开的数据，
    却等到 8/31 才计入」这种失真，也不会有前视偏差。

    rev_ttm / op_ttm（TTM 营收、TTM 营业利润）供 PS 与 EV/EBIT 使用：
    与净利同一组报告、同一公式，保证 PE/PS/EV·EBIT 同源同基准。
    某些公司财报缺营收/营业利润字段时为 None，不影响净利部分。
    """
    periods = sorted(fin.keys(), reverse=True)
    out: Dict[str, List[tuple]] = {}

    for c in codes:
        tl: List[tuple] = []
        for p in periods:
            ly, lq = _parse_period(p)
            deps = ds.ttm_required_periods(p)
            vals: Dict[Tuple[int, int], float] = {}
            rev_vals: Dict[Tuple[int, int], float] = {}
            op_vals: Dict[Tuple[int, int], float] = {}
            dnp_vals: Dict[Tuple[int, int], float] = {}
            dates: List[str] = []
            ok = True
            for dp in deps:
                row = (fin.get(dp) or {}).get(c)
                if not row or row.get("parent_np") is None:
                    ok = False
                    break
                dy, dq = _parse_period(dp)
                vals[(dy, dq)] = row["parent_np"]
                if row.get("revenue") is not None:
                    rev_vals[(dy, dq)] = row["revenue"]
                if row.get("op_profit") is not None:
                    op_vals[(dy, dq)] = row["op_profit"]
                if row.get("deduct_np") is not None:
                    dnp_vals[(dy, dq)] = row["deduct_np"]
                # 缺披露日时退回法定截止日（保守）
                dates.append(_disclosure_date(row)
                             or str(ds.period_deadline(dp)))
            if not ok:
                continue

            ttm, ttm_prev = _acc_ttm(vals, ly, lq)
            if ttm is None or ttm_prev is None:
                continue
            g = (ttm - ttm_prev) / abs(ttm_prev) * 100.0 if ttm_prev else None
            rev_ttm, _ = _acc_ttm(rev_vals, ly, lq)
            op_ttm, _ = _acc_ttm(op_vals, ly, lq)
            # 扣非与归母净利润共用同一组报告期：缺任意一期的扣非就整条置空，
            # 不做部分拼接，避免拼出四不像的口径
            dnp_ttm, dnp_ttm_prev = _acc_ttm(dnp_vals, ly, lq)
            tl.append((max(dates), ttm, ttm_prev, g, p, rev_ttm, op_ttm,
                       dnp_ttm, dnp_ttm_prev))

        if tl:
            tl.sort(key=lambda x: x[0])
            out[c] = tl
    return out


def pit_as_of(tl: Optional[List[tuple]], d: str):
    """取该日期已生效的最新一条 TTM；没有则返回 None"""
    if not tl:
        return None
    best = None
    for row in tl:
        if row[0] <= d:
            best = row
        else:
            break
    return best


def pit_at_period(tl: Optional[List[tuple]], period: Optional[str]):
    """
    取时间线上「基准期 == period」的那一条；没有就返回 None，**不回退**。

    统一口径用：全组合强制停在同一报告期上。缺这一期的公司直接不参与计算，
    而不是退到它自己能用的最新期 —— 否则又变回混合口径了。
    """
    if not tl or not period:
        return None
    for row in tl:
        if row[4] == period:
            return row
    return None


def _ttm_dps(xs: List[str], pre: List[float], d: str) -> float:
    """
    近 365 天（(d-365, d]）累计每股现金分红。

    xs = 除息日（升序），pre = 前缀和（pre[i] = xs[:i+1] 的 dps 合计）。
    二分定位窗口，O(log n)。A 股股息率 = 该值 ÷ 当日股价。
    """
    if not xs:
        return 0.0
    lo = (date(*map(int, d.split("-"))) - timedelta(days=365)).isoformat()
    i = bisect_right(xs, lo)                 # 第一个 > lo 的下标
    j = bisect_right(xs, d)                  # 第一个 > d 的下标
    if i >= j:
        return 0.0
    return pre[j - 1] - (pre[i - 1] if i > 0 else 0.0)


def _ttm_for(fin: Dict[str, Dict[str, dict]], code: str,
             base: str, key: str = "parent_np"
             ) -> Tuple[Optional[float], Optional[float]]:
    """
    某只股票在指定基准期上的 (TTM 净利, 去年同期 TTM 净利)；缺数据返回 (None, None)。

    key：parent_np=归母（默认）；deduct_np=扣非。两者用完全相同的报告期与公式，
    缺任意一期的该项就整条置空，避免拼出口径不一致的 TTM。
    """
    ly, lq = _parse_period(base)
    vals: Dict[Tuple[int, int], float] = {}
    for dp in ds.ttm_required_periods(base):
        row = (fin.get(dp) or {}).get(code)
        if not row or row.get(key) is None:
            return None, None
        vals[_parse_period(dp)] = row[key]
    try:
        if lq == 4:                       # 年报本身即 TTM
            return vals[(ly, 4)], vals[(ly - 1, 4)]
        cur = vals[(ly, lq)] + vals[(ly - 1, 4)] - vals[(ly - 1, lq)]
        prev = vals[(ly - 1, lq)] + vals[(ly - 2, 4)] - vals[(ly - 2, lq)]
        return cur, prev
    except KeyError:
        return None, None


def series_bases(n: int = 9) -> List[str]:
    """最近 n 个报告期，作为增速序列的基准期（由新到旧）"""
    return ds.latest_report_periods(limit=n)


def ensure_series_periods(n: int = 9, on_stage=None) -> None:
    """补齐构造 n 期增速序列所需的报告期（缺的才回源，入库存久保存）"""
    need: List[str] = []
    for b in series_bases(n):
        need.extend(ds.ttm_required_periods(b))
    missing = [p for p in sorted(set(need), reverse=True) if not store.has_fin(p)]
    if missing:
        if on_stage:
            on_stage(f"补拉 {len(missing)} 个历史报告期…")
        _fetch_periods(missing, on_stage)


def _qoq(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    """环比 = (cur − prev) / |prev| × 100；prev 缺失或为 0 时返回 None。"""
    if cur is None or not prev:
        return None
    return (cur - prev) / abs(prev) * 100.0


def _single_quarter(cums: List[Optional[float]], i: int,
                    period: Optional[str]) -> Optional[float]:
    """
    由「年初至今累计」还原单季净利。

    A 股财报是累计口径：Q1 累计本身就是单季，Q2/Q3/Q4 才需要减上一季累计。
    跨年不能直接相减——Q1 累计只有三个月，减上年 Q4（十二个月）会得到负数，
    进而让这一行和下一行的「单季环比」双双失真。
    """
    if not period or i >= len(cums):
        return None
    cur = cums[i]
    if cur is None:
        return None
    if period[5:7] == "03":                 # 一季报：累计 = 单季
        return cur
    nxt = cums[i + 1] if i + 1 < len(cums) else None
    return None if nxt is None else cur - nxt


def quarterly_growth(codes: List[str], bases: List[str],
                     with_stocks: bool = False,
                     weights: Optional[Dict[str, float]] = None):
    """
    按报告期逐期计算组合 TTM 同比增速，并派生两种环比。

    返回 (series, per_stock)
      series    = [{period, label, g, g_valid, np_now, np_prev, qoq_ttm,
                    qoq_single, used, vused, total}]        组合口径（整体法）
      per_stock = {code: {"yoy": [...], "qoq_ttm": [...], "qoq_single": [...],
                          "deduct": [...]}}
                  每只股票的逐期同比 / TTM 环比 / 单季环比 / 扣非同比
                  （港股无扣非口径，"deduct" 整列为 None）

    weights 为持仓权重（%）。传入时组合层全部按权重折算（全口径，含亏损与
    负增长）；不传则退化为等权相加（向后兼容）。

    环比与同比同为「全样本」口径（含亏损与负增长），序列由新到旧：
      · TTM 环比 = 本期 TTM 归母净利 vs 上一期 TTM 归母净利
      · 单季环比 = 单季归母净利 vs 上一单季（单季由累计值还原，见 _single_quarter：
        Q1 累计即单季，Q2 之后减上一季累计，跨年不做减法）
      单季环比仅 A 股成分（港股半年度财报无单季数据）；最旧一期 TTM 环比、
      最旧两期单季环比为 None。
    """
    # 逐期只取成分股的数据，避免把全市场读进内存。
    # A 股财报在 A 股库、港股财报在港股库（5 位代码），必须各查各的再合并；
    # 只查 A 股库的话，含港股的基金近 N 期增速里港股一行全是空。
    hk_codes = [c for c in codes if store.market_of(None, c) is store.HK]
    a_codes = [c for c in codes if c not in set(hk_codes)]
    periods = sorted({dp for b in bases for dp in ds.ttm_required_periods(b)},
                     reverse=True)
    fin: Dict[str, Dict[str, dict]] = {}
    for p in periods:
        fin[p] = store.load_fin_subset(p, a_codes)

    # 港股：按公司财年构造 TTM 时间线（12/3/6/9 月年结都正确），逐基准期
    # 取「该期披露截止日已生效」的最新条目——比旧逻辑（只在 06-30/12-31
    # 基准期才有港股数据）更准，且 3 月年结公司不再整段丢失。
    # 这里只读库不回源：近期财报由 analyze_etf 的拉取循环保证已入库。
    hk_tl: Dict[str, List[tuple]] = {}
    if hk_codes:
        try:
            hk_tl = hk.build_hk_ttm_timelines(hk_codes)
        except Exception:                                   # noqa: BLE001
            hk_tl = {}

    series: List[dict] = []
    cum_comb: List[float] = []          # 每期组合累计归母净利（A 股全样本，已按权重折算）
    per_yoy: Dict[str, List[Optional[float]]] = {c: [] for c in codes} if with_stocks else {}
    per_ttm: Dict[str, List[Optional[float]]] = {c: [] for c in codes} if with_stocks else {}
    per_cum: Dict[str, List[Optional[float]]] = {c: [] for c in codes} if with_stocks else {}
    # 逐期扣非同比：港股没有扣非口径，整列 None（不是"缺数据"）
    per_deduct: Dict[str, List[Optional[float]]] = {c: [] for c in codes} if with_stocks else {}
    # 持仓权重（%）：传入则组合层按权重折算；未传退化为等权（兼容旧调用）
    wmap = weights or {}
    use_w = bool(wmap)

    for p in bases:
        p_dl = ds.period_deadline(p).isoformat()   # 该基准期「已知」的截止日
        now = pre = 0.0            # 全样本加权 TTM：含亏损与负增长
        vnow = vpre = 0.0          # 正增长样本（仅作对照）：只留增速 > 0 的
        cum = 0.0                  # 全样本加权累计归母净利（仅 A 股，单季环比用）
        used = vused = 0
        fin_p = fin.get(p) or {}
        for c in codes:
            w = ((wmap.get(c) or 0.0) / 100.0) if use_w else 1.0
            is_hk = c in hk_tl
            if is_hk:
                entry = pit_as_of(hk_tl[c], p_dl)
                ttm = entry[1] if entry else None
                ttm_prev = entry[2] if entry else None
            else:
                ttm, ttm_prev = _ttm_for(fin, c, p)
            g = ((ttm - ttm_prev) / abs(ttm_prev) * 100.0
                 if (ttm is not None and ttm_prev) else None)
            # 累计归母净利（A 股口径）：parent_np 为报告期累计值
            cum_c = None
            if not is_hk:
                row_c = fin_p.get(c)
                if row_c and row_c.get("parent_np") is not None:
                    cum_c = row_c["parent_np"]
            if cum_c is not None:
                cum += w * cum_c
            if with_stocks:
                per_yoy[c].append(g)
                per_ttm[c].append(ttm)
                per_cum[c].append(cum_c)
                if is_hk:
                    per_deduct[c].append(None)      # 港股不披露扣非
                else:
                    d_ttm, d_prev = _ttm_for(fin, c, p, "deduct_np")
                    per_deduct[c].append(
                        ((d_ttm - d_prev) / abs(d_prev) * 100.0)
                        if (d_ttm is not None and d_prev) else None)
            if ttm is None or ttm_prev is None:
                continue
            now += w * ttm
            pre += w * ttm_prev
            used += 1
            if g is not None and g > 0:
                vnow += w * ttm
                vpre += w * ttm_prev
                vused += 1
        series.append({
            "period": p,
            "label": report_label(p),
            "g": ((now - pre) / abs(pre) * 100.0) if pre else None,
            "g_valid": ((vnow - vpre) / abs(vpre) * 100.0) if vpre else None,
            # 组合 TTM 归母净利润（本期 / 去年同期），单位元。
            # 导出给 AI 时和增速并列放：只有增速没有基数，判断不了增长的分量。
            "np_now": now,
            "np_prev": pre,
            "used": used,
            "vused": vused,
            "total": len(codes),
        })
        cum_comb.append(cum)

    # ---- 派生环比（序列由新到旧，环比 = 本期 vs 上一期） ----
    n = len(series)
    for i in range(n):
        qoq_ttm = None
        if i + 1 < n:
            qoq_ttm = _qoq(series[i]["np_now"], series[i + 1]["np_now"])
        series[i]["qoq_ttm"] = qoq_ttm
        series[i]["qoq_single"] = _qoq(
            _single_quarter(cum_comb, i, series[i]["period"]),
            _single_quarter(cum_comb, i + 1,
                            series[i + 1]["period"] if i + 1 < n else None))

    if not with_stocks:
        return series, None

    per: Dict[str, dict] = {}
    for c in codes:
        yoy = per_yoy[c]
        ttm_list = per_ttm[c]
        cum_list = per_cum[c]
        qoq_ttm_l = [None] * n
        qoq_single_l = [None] * n
        for i in range(n):
            if i + 1 < n:
                qoq_ttm_l[i] = _qoq(ttm_list[i], ttm_list[i + 1])
            qoq_single_l[i] = _qoq(
                _single_quarter(cum_list, i, series[i]["period"]),
                _single_quarter(cum_list, i + 1,
                                series[i + 1]["period"] if i + 1 < n else None))
        per[c] = {"yoy": yoy, "qoq_ttm": qoq_ttm_l, "qoq_single": qoq_single_l,
                  "deduct": per_deduct[c]}
    return series, per


def stock_fin_series(codes: List[str], bases: List[str]) -> Dict[str, List[Optional[dict]]]:
    """
    每只股票在 series_bases 各报告期的财务快照（累计口径，与 g_series 同期别）。
    返回 {code: [row_or_None, ...]}，与 bases 一一对齐；row 含
    np / np_yoy / revenue / eps / roe / div_yield 六项。
    A 股读 A 股库、港股读港股库（字段同名，各自 save_fin 后入库）。
    """
    out: Dict[str, List[Optional[dict]]] = {c: [None] * len(bases) for c in codes}
    a_codes = [c for c in codes if store.market_of(None, c) is store.A]
    hk_codes = [c for c in codes if store.market_of(None, c) is store.HK]
    for i, p in enumerate(bases):
        for lib, cs in ((store.A, a_codes), (store.HK, hk_codes)):
            if not cs:
                continue
            rows = lib.load_fin_subset(p, cs)
            for c in cs:
                r = rows.get(c)
                if not r:
                    continue
                out[c][i] = {
                    "np": r.get("parent_np"),
                    "np_yoy": r.get("np_yoy"),
                    "revenue": r.get("revenue"),
                    "eps": r.get("eps"),
                    "roe": r.get("roe"),
                    "div_yield": r.get("dividend_rate"),
                }
    return out


def build_ttm_series(fin: Dict[str, Dict[str, dict]],
                     periods: List[str]) -> Dict[str, dict]:
    """
    fin: {period_str: {code: {...}}}
    返回 {code: {np_ttm, np_ttm_prev, g_ttm, g_latest, period}}
    """
    if not periods:
        return {}
    latest = periods[0]
    ly, lq = _parse_period(latest)
    need = {
        "cur":   ds.period_key((ly, lq)),
        "prev":  ds.period_key((ly - 1, lq)),
        "ann1":  ds.period_key((ly - 1, 4)),
        "prev2": ds.period_key((ly - 2, lq)),
        "ann2":  ds.period_key((ly - 2, 4)),
    }
    have = {k: fin.get(v, {}) for k, v in need.items()}
    if not have["cur"]:
        return {}

    codes = set(have["cur"].keys())
    out: Dict[str, dict] = {}
    for c in codes:
        cur = _np(have["cur"], c, "parent_np")
        prev = _np(have["prev"], c, "parent_np")
        ann1 = _np(have["ann1"], c, "parent_np")
        prev2 = _np(have["prev2"], c, "parent_np")
        ann2 = _np(have["ann2"], c, "parent_np")

        ttm = None
        if cur is not None and ann1 is not None and prev is not None:
            ttm = cur + ann1 - prev
        elif lq == 4 and cur is not None:            # 年报本身就是 TTM
            ttm = cur

        ttm_prev = None
        if prev is not None and ann2 is not None and prev2 is not None:
            ttm_prev = prev + ann2 - prev2
        elif lq == 4 and prev is not None:
            ttm_prev = prev

        g_ttm = None
        if ttm is not None and ttm_prev not in (None, 0):
            g_ttm = (ttm - ttm_prev) / abs(ttm_prev) * 100.0

        out[c] = {
            "np_ttm": ttm,
            "np_ttm_prev": ttm_prev,
            "g_ttm": g_ttm,
            "g_latest": have["cur"].get(c, {}).get("np_yoy"),
            "period": latest,
        }
    return out


def _parse_period(p: str) -> Tuple[int, int]:
    y, m, _ = p.split("-")
    return int(y), {3: 1, 6: 2, 9: 3, 12: 4}[int(m)]


def _fetch_periods(periods: List[str], on_stage=None, cancel=None) -> Dict[str, Dict[str, dict]]:
    """并发拉取若干报告期，逐期上报进度（每期约 20+ 页）。"""
    import concurrent.futures as cf
    out: Dict[str, Dict[str, dict]] = {}
    lock = threading.Lock()
    state = {"done": 0, "pg": 0}

    def one(p):
        def on_page(_done_pg, _total_pg):
            with lock:
                state["pg"] += 1
                snapshot = (state["done"], state["pg"])
            if on_stage:
                on_stage(f"拉取全市场财报 {snapshot[0]}/{len(periods)} 期（{snapshot[1]} 页）…")

        d = ds.fetch_financials(p, on_progress=on_page)
        with lock:
            state["done"] += 1
            snapshot = (state["done"], state["pg"])
            # 定稿期条目过少视为数据残缺；披露窗口内的期按「开闸」阈值放行 ——
            # 此时条目数逐日增长（10 月下旬的三季报只有几百家），拉到多少算多少，
            # 之后由 ACTIVE_FIN_TTL 每 6 小时刷新补齐，未披露的公司走 PIT 回退
            if len(d) >= 1000 or (not ds.is_settled(p) and len(d) >= ds.EMERGING_MIN_ROWS):
                out[p] = d
        if on_stage:
            on_stage(f"拉取全市场财报 {snapshot[0]}/{len(periods)} 期（{snapshot[1]} 页）…")
        return d

    if on_stage:
        on_stage(f"拉取全市场财报 0/{len(periods)} 期…")
    with cf.ThreadPoolExecutor(max_workers=min(4, len(periods))) as pool:
        list(pool.map(one, periods))
    return out


def _fetch_hk_fin_guarded(period: str, on_progress=None,
                          timeout_s: float = 600) -> Optional[Dict[str, dict]]:
    """
    单期港股财报拉取的护栏。

    正常耗时 1~2 分钟（全市场约 5 页）；但上游偶发会把连接黑洞住——
    DNS / 代理层挂起时 urlopen 的 socket 超时并不生效，实测能把整个
    回填任务挂死 8 小时（005889 卡在 2013-06-30 一期）。这里用
    daemon 线程 + 墙钟超时兜底：超时或异常即放弃该期，该期港股成分的
    TTM 落空、覆盖率如实下降，绝不让一期数据拖死整个任务。
    """
    import threading
    box: Dict[str, Optional[Dict[str, dict]]] = {}

    def _pull() -> None:
        try:
            box["got"] = hk.fetch_hk_financials(period, on_progress=on_progress)
        except Exception:                               # noqa: BLE001
            box["got"] = None                           # 单期失败不拖死整个回填

    t = threading.Thread(target=_pull, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return None
    return box.get("got")


def financial_periods_for(start: Optional[date] = None, depth: int = 4) -> List[str]:
    """
    构建 TTM 时间线所需的全部报告期。depth 是需要的**基准期个数**。

    长区间回填时每个季度都是一个基准期（区间早期那几天用的就是旧报告），
    依赖必须全部并进来，否则 2023 年这样的早期日期会因构造不出 TTM 被整段丢弃。
    """
    if start is None:
        cands = ds.latest_report_periods(limit=depth)
    else:
        # start 之前最近的一期也可能是区间早期用到的基准期，多留 2 期余量
        cands = [p for p in ds.latest_report_periods(limit=200)
                 if ds.period_deadline(p) >= start - timedelta(days=110)]
        cands = cands[:depth]
    needed: List[str] = []
    for p in cands:
        needed.extend(ds.ttm_required_periods(p))
    return sorted(set(needed), reverse=True)


def load_financials(on_stage=None, cancel=None) -> Dict[str, Dict[str, dict]]:
    """载入构建 TTM 时间线所需的财报（含回退期）。"""
    fin = _fetch_periods(financial_periods_for(depth=4), on_stage, cancel)
    if not fin:
        fin = _fetch_periods(ds.ttm_required_periods(), on_stage, cancel)
    if not fin:
        raise RuntimeError("财报数据为空，上游接口可能暂时不可用，请稍后重试")
    return fin


# --------------------------------------------------------------------------- #
#  单只 ETF 的 PEG
# --------------------------------------------------------------------------- #
def analyze_etf(etf_code: str, etf_name: str = "",
                growth_mode: str = "ttm",
                quotes: Optional[Dict[str, dict]] = None,
                fin: Optional[Dict[str, Dict[str, dict]]] = None,
                holdings: Optional[dict] = None,
                record: bool = True,
                as_of: Optional[str] = None,
                series_quarters: int = 9,
                pe_mode: str = "mixed") -> ETFResult:
    """
    growth_mode: 'ttm'（TTM 归母净利同比，稳健）| 'latest'（最新报告期累计同比，灵敏）
    as_of: 计算基准日，默认今天。每只股票只采用该日**之前已披露**的财报。
    series_quarters: 额外计算最近多少个报告期的组合增速（0 表示不算）。
    pe_mode: 'mixed'（默认）每只成分股各用自己**最新已披露**的那一期，时效性最好，
    但披露季内组合里会同时存在三季报与中报，横向可比性下降；
    'uniform' 全组合强制停在同一报告期（基准日之前**已定稿**的最新期），
    牺牲时效换可比性，缺该期的公司不参与计算。
    """
    # ETF 联接基金：本身不持有股票（持有目标 ETF 份额），直接分析会拿到
    # 多年前残留的旧股票持仓（如 2021 年），PEG 完全失真。这里自动穿透到
    # 目标 ETF 重新分析；匹配不到目标 ETF 时直接阻断，避免用失真数据出结果。
    feeder = ds.resolve_feeder_target(etf_code, etf_name)
    if feeder:
        tgt = feeder.get("target")
        if tgt and tgt["code"] != etf_code:
            res = analyze_etf(tgt["code"], tgt["name"], growth_mode,
                              quotes, fin, holdings, record, as_of,
                              series_quarters, pe_mode)
            res.feeder_from = {"code": etf_code, "name": etf_name or etf_code}
            return res
        res = ETFResult(code=etf_code, name=etf_name or etf_code,
                        report_date=None, fin_period=None,
                        updated_at=ds._now())
        res.warn = ("该基金是 ETF 联接基金，不直接持有股票（持有目标 ETF 份额），"
                    "无法计算有意义的 PE/PEG。"
                    + (f"请分析其跟踪标的对应的场内 ETF（跟踪：{feeder['tracking']}）。"
                       if feeder.get("tracking")
                       else "请在搜索框直接搜索对应的场内 ETF。"))
        res.feeder_from = {"code": etf_code, "name": etf_name or etf_code}
        return res

    if holdings is None:
        holdings = ds.fetch_etf_holdings(etf_code)
    if quotes is None:
        quotes = ds.fetch_realtime_quotes()

    # fin is None 才自动拉取；显式传入空 dict 时不再回源，
    # 否则批量扫描里每只 ETF 都会各自触发一次全量财报拉取。
    if fin is None:
        fin = load_financials()
    periods = sorted(fin.keys(), reverse=True)
    basis = {
        "latest": periods[0] if periods else None,
        "latest_label": report_label(periods[0]) if periods else None,
        "parts": ttm_basis(periods[0]) if periods else [],
        "mode": growth_mode,
    }

    stocks = holdings.get("stocks", [])
    res = ETFResult(code=etf_code, name=etf_name or etf_code,
                    report_date=holdings.get("report_date"),
                    fin_period=periods[0] if periods else None,
                    holdings_full=bool(holdings.get("full", True)),
                    fin_basis=basis,
                    updated_at=ds._now())
    res.n_total = len(stocks)
    if not stocks:
        res.warn = "未获取到该 ETF 的持仓明细（可能是新成立基金或债券/商品类 ETF）"
        return res

    # 近 N 个报告期各自的组合增速 + 每只股票的逐期增速 / 环比 / 财务快照
    per_stock_series: Optional[Dict[str, dict]] = None
    fin_series_map: Optional[Dict[str, List[Optional[dict]]]] = None
    if series_quarters > 0:
        try:
            ensure_series_periods(series_quarters)
            bases = series_bases(series_quarters)
            sers, per_stock_series = quarterly_growth(
                [s["code"] for s in stocks], bases, with_stocks=True,
                weights={s["code"]: s.get("weight") or 0 for s in stocks})
            res.growth_series = sers
            basis["series_bases"] = bases
            fin_series_map = stock_fin_series([s["code"] for s in stocks], bases)
        except Exception:                               # noqa: BLE001
            res.growth_series, per_stock_series, fin_series_map = None, None, None

    # 场外基金：统计股票仓位与非 A 股持仓。
    # 主动基金常持有现金/债券，重仓港股的基金 A 股部分可能只有一半，
    # 这些都会让「PE = 整个基金的 PE」这个说法失真，必须显式告知。
    all_w = sum(s.get("weight") or 0 for s in stocks)
    res.equity_ratio = all_w
    # 港股已经能算了，不能再和美股一起归到「无法估值」里
    is_hk = lambda s: store.market_of(s.get("market"), s.get("code")) is store.HK   # noqa: E731
    res.n_hk = sum(1 for s in stocks if is_hk(s))
    res.n_a = sum(1 for s in stocks
                  if store.market_of(s.get("market"), s.get("code")) is store.A)
    res.n_oversea = len(stocks) - res.n_a - res.n_hk
    res.hk_weight = sum(s.get("weight") or 0 for s in stocks if is_hk(s))
    ow = sum(s.get("weight") or 0 for s in stocks
             if store.market_of(s.get("market"), s.get("code")) is store.A
             or is_hk(s))
    res.oversea_weight = all_w - ow
    try:
        f = ds.lookup_fund(etf_code)
        if f:
            res.fund_type = f.get("type")
            if not etf_name or etf_name == etf_code:
                res.name = f.get("name") or res.name
    except Exception:                                   # noqa: BLE001
        pass

    # ---- 港股：东财估值库没有港股 PE，自己算「当日市值 ÷ TTM 股东应占溢利」----
    # 与 A 股完全同口径（A 股也是 mktcap / np_ttm），所以下面只多一个数据来源，
    # 不加任何特例分支。只有持仓里真有港股时才发请求，纯 A 股 ETF 零额外开销。
    hk_codes = [s["code"] for s in stocks
                if store.market_of(s.get("market"), s["code"]) is store.HK]
    hk_mc: Dict[str, Optional[float]] = {}
    hk_close: Dict[str, Optional[float]] = {}
    if hk_codes:
        try:
            # depth=6（6 个季度基准期）：四季期口径下足够覆盖最新一期 +
            # 其 TTM 依赖 + prev 依赖；首批调用会触发 v3 重拉（补财报
            # 类型字段），之后永久缓存。纯 A 股 ETF 零开销。
            for p in hk.hk_fin_periods(depth=6):
                got = hk.fetch_hk_financials(p)
                if got:
                    fin.setdefault(p, {}).update(got)   # 5 位与 6 位代码不会撞
        except Exception:                               # noqa: BLE001
            pass                                        # 港股不可用时退化为「只算 A 股」
    hk_d = None
    try:
        # 上游行情挂了会退回库里最近的快照（可能滞后几天），日期随结果返回，
        # 界面上单独标出「港股行情 YYYY-MM-DD」，不与 A 股的 PE 数据日混为一谈
        hk_d = hk.ensure_hk_market_snapshot()
        if hk_d:
            m = store.HK.load_stock_pe(hk_codes, hk_d, hk_d)
            # cell = (pe_ttm, mktcap, close, pb, ps, pcf)。close 一并取出：
            # 港股不在 A 股实时行情里，不补的话股价一栏是空的
            hk_mc = {c: ((m.get(c) or {}).get(hk_d) or (None,) * 6)[1] for c in m}
            hk_close = {c: ((m.get(c) or {}).get(hk_d) or (None,) * 6)[2]
                        for c in m}
    except Exception:                               # noqa: BLE001
        hk_mc = {}
        hk_close = {}

    # PE 统一用 PE-TTM（东财估值库）：历史序列只有这个口径，
    # 实时行情的「市盈率(动态)」是年化值、无历史，两边混用会导致曲线断裂。
    # PB/PS/PCF 与 PE 同一行数据（RPT_VALUEANALYSIS_DET），一并取出零额外开销。
    pe_date = None
    val_src: Dict[str, dict] = {}
    try:
        pe_date = ds.ensure_market_pe()
        if pe_date:
            m = store.load_stock_pe([s["code"] for s in stocks
                                     if s["code"] not in set(hk_codes)], pe_date, pe_date)
            for c, days in m.items():
                cell = days.get(pe_date) or (None,) * 6
                val_src[c] = {"pe": cell[0], "mktcap": cell[1],
                              "pb": cell[3], "ps": cell[4], "pcf": cell[5]}
    except Exception:                               # noqa: BLE001
        pe_date = None
    res.pe_date = pe_date
    res.hk_pe_date = hk_d

    # 按每家公司真实披露日逐只对齐（PIT）：
    # 只用 as_of 之前已公开的数据，早披露的公司提前计入，晚披露的延后计入。
    as_of_date = as_of or date.today().isoformat()

    # 统一口径：全组合停在「基准日之前已定稿」的最新一期上。
    # 必须卡死「已过法定截止日 + 宽限期」，不能只按 candidate_periods 的准入：
    # 后者含披露季探活，会把正在陆续披露的当期也算进来，而当期只有少数公司
    # 出报，拿它当统一基准会让绝大多数成分股缺席（003096 实测覆盖率掉到 12%）。
    # 同时用 as_of 而不是今天判断，历史回填时才会停在当时的期上。
    uni_base: Optional[str] = None
    if pe_mode == "uniform":
        try:
            asof_d = date(*(int(x) for x in as_of_date.split("-")))
        except Exception:                               # noqa: BLE001
            asof_d = date.today()
        for p in (ds.period_key(yq) for yq in
                  ds.candidate_periods(asof_d, years_back=1, include_emerging=False)):
            if p in fin and asof_d > ds.period_deadline(p) + timedelta(days=7):
                uni_base = p
                break
    if uni_base:
        res.fin_period = uni_base
    # 没选到期（如 fin 为空）时实际按 mixed 跑，必须如实上报，
    # 否则前端会显示「统一口径」文案而数值却是混合口径的
    basis["pe_mode"] = pe_mode if uni_base else "mixed"
    basis["uniform"] = uni_base           # 统一口径实际采用的报告期
    # A 股走日历季度公式；港股统一走按公司财年的构造器（12/3/6/9 月年结
    # 都正确），元组结构一致，pit_as_of 与下游零改动。混用 A 股公式会把
    # 非 12 月年结公司（财报在 03-31/09-30 期）静默漏掉。
    timelines = build_pit_timelines(
        fin, [s["code"] for s in stocks
              if store.market_of(s.get("market"), s["code"]) is store.A])
    if hk_codes:
        try:
            timelines.update(hk.build_hk_ttm_timelines(hk_codes))
        except Exception:                               # noqa: BLE001
            pass                                        # 构造失败退化为「该股无 TTM」
    used_periods: Dict[str, int] = {}

    # 股息率：A 股用分红明细（近 12 个月滚动窗口），港股用财报 dps。
    # 分红未初始化时 A 股部分为 None（不误报）；港股逻辑维持现状。
    a_div: Dict[str, tuple] = {}
    try:
        div_map = store.load_stock_div([s["code"] for s in stocks
                                        if ds.is_a_share(s.get("market"), s["code"])])
        for c, evs in div_map.items():
            if evs:
                xs: List[str] = []
                pre: List[float] = []
                acc = 0.0
                for ex, dps in evs:
                    acc += dps
                    xs.append(ex)
                    pre.append(acc)
                a_div[c] = (xs, pre)
    except Exception:                                   # noqa: BLE001
        a_div = {}

    rows: List[StockRow] = []
    for s in stocks:
        code = s["code"]
        q = quotes.get(code) or {}
        hit = (pit_at_period(timelines.get(code), uni_base)
               if uni_base else pit_as_of(timelines.get(code), as_of_date))
        if hit:
            (_, np_ttm, np_ttm_prev, g_ttm, base_p, rev_ttm, op_ttm,
             dnp_ttm, dnp_ttm_prev) = hit
            fin_row = (fin.get(base_p) or {}).get(code) or {}
            g_latest = fin_row.get("np_yoy")
            # 与 PIT 生效日同口径：盘后披露的按当晚计，不是次日的公告日，
            # 否则表里会出现「披露日在未来、却已经用它算了 PE」的矛盾
            fin_date = _disclosure_date(fin_row)
            used_periods[base_p] = used_periods.get(base_p, 0) + 1
        else:
            np_ttm = np_ttm_prev = g_ttm = base_p = None
            rev_ttm = op_ttm = None
            dnp_ttm = dnp_ttm_prev = None
            g_latest = fin_date = None

        # PE = 实时总市值 ÷ 该股「最新已披露」的 TTM 归母净利。
        # 与增速共用同一期财报，保证同源同基准；实测与东财 PE_TTM 完全一致。
        # 港股的 mc 来自港股库的当日快照，其余逻辑与 A 股完全相同。
        mc = q.get("mktcap") or hk_mc.get(code)
        pe = (mc / np_ttm) if (mc and np_ttm and np_ttm > 0) else None
        vs = val_src.get(code) or {}
        if pe is None or pe <= 0:                 # 兜底：东财估值库 → 行情动态 PE
            pe = vs.get("pe") or q.get("pe_ttm")

        # ---- 多指标（PB / PS / 净资产 / 股息 / EV）----
        # PB：A 股直接用东财 PB_MRQ（与 PE 同源）；净资产反推 = 市值 / PB。
        # 港股净资产用最新已披露的 TOTAL_PARENT_EQUITY，PB = 当日市值 ÷ 净资产。
        equity = fin_row.get("equity") if base_p else None
        pb = vs.get("pb")
        if pb is None and equity and mc and equity > 0 and mc > 0:
            pb = mc / equity
        if pb is not None and (equity is None) and pb and mc and pb > 0:
            equity = mc / pb
        # PS：市值 ÷ TTM 营收（与 PE 同一套报告同源构造）
        ps = (mc / rev_ttm) if (mc and rev_ttm and rev_ttm > 0) else None
        if ps is None:
            ps = vs.get("ps")
        # 股息率：A 股 = 近 12 个月累计每股分红 ÷ 现价（与回填同口径）；
        # 港股 = DPS(近一年每股分红)×股本 ÷ 市值，退化用报告期股息率。
        div_y = None
        price = q.get("price") or hk_close.get(code)
        if code in a_div and price and price > 0:
            xs, pre = a_div[code]
            ttm = _ttm_dps(xs, pre, as_of_date)
            if ttm and ttm > 0:
                div_y = ttm / price * 100.0
        if div_y is None:
            shares = fin_row.get("shares") if base_p else None
            dps = fin_row.get("dps") if base_p else None
            if dps and shares and mc and mc > 0:
                div_y = dps * shares / mc * 100.0
            elif fin_row.get("dividend_rate") is not None:
                div_y = fin_row.get("dividend_rate")
        # EV/EBIT（近似口径）：EV = 市值 + 总负债 − 现金，EBIT = TTM 营业利润。
        # 负债/现金只有港股财报提供，A 股成分 EV 为 None。
        ev = None
        tliab = fin_row.get("total_liab") if base_p else None
        cash = fin_row.get("end_cash") if base_p else None
        if mc and tliab is not None and cash is not None:
            ev = mc + tliab - cash
        pcf = vs.get("pcf")
        # 扣非 TTM 同比：与归母同一组报告期、同一公式。港股没有扣非口径，
        # dnp_ttm 本身就是 None，这里会自动留空（不做归母顶替）。
        g_deduct = (((dnp_ttm - dnp_ttm_prev) / abs(dnp_ttm_prev) * 100.0)
                    if (dnp_ttm is not None and dnp_ttm_prev) else None)

        row = StockRow(
            code=code,
            name=s.get("name") or q.get("name") or code,
            weight=s.get("weight"),
            price=q.get("price") or hk_close.get(code),
            pe_ttm=pe,
            mktcap=mc,
            np_ttm=np_ttm,
            np_ttm_prev=np_ttm_prev,
            deduct_ttm=dnp_ttm,
            deduct_ttm_prev=dnp_ttm_prev,
            g_ttm=g_ttm,
            g_latest=g_latest,
            g_deduct=g_deduct,
            fin_period=base_p,
            # 港股财报自带类型（如「2025年年报」），按公司财年显示；
            # A 股 fin 行没有该字段，退回日历标签
            fin_label=((fin_row.get("report_type") if base_p else None)
                       or (report_label(base_p) if base_p else None)),
            fin_date=fin_date,
            market=s.get("market"),
            g_series=((per_stock_series or {}).get(code) or {}).get("yoy"),
            qoq_ttm_series=((per_stock_series or {}).get(code) or {}).get("qoq_ttm"),
            qoq_single_series=((per_stock_series or {}).get(code) or {}).get("qoq_single"),
            g_deduct_series=((per_stock_series or {}).get(code) or {}).get("deduct"),
            fin_series=(fin_series_map or {}).get(code),
            pb=pb,
            ps=ps,
            pcf=pcf,
            equity=equity,
            rev_ttm=rev_ttm,
            op_ttm=op_ttm,
            ev=ev,
            div_yield=div_y,
        )

        g = row.g_ttm if growth_mode == "ttm" else row.g_latest
        if store.market_of(s.get("market"), code) is store.HK:
            # 港股能算：缺的只是数据（未初始化 / 该股没入库），不是"不能算"
            if row.pe_ttm is None or row.pe_ttm <= 0:
                # 连市值都没有 → 行情没到（快照缺该股/港股库未初始化），
                # 不该报成「PE 无效」误导成公司亏损
                row.status = "no_quote" if not row.mktcap else "no_pe"
            elif g is None or g <= 0:
                row.status = "no_growth"
            else:
                row.peg = row.pe_ttm / g
        elif not ds.is_a_share(s.get("market"), code):
            row.status = "oversea"    # 美股等：确实无法估值，不是"亏损"
        elif row.pe_ttm is None or q.get("price") is None:
            row.status = "no_quote" if row.pe_ttm is None and q.get("price") is None else "no_pe"
        elif not (row.pe_ttm > 0):
            row.status = "no_pe"          # 亏损 / 微利，PE 无意义
        elif g is None:
            row.status = "no_growth"
        elif g <= 0:
            row.status = "no_growth"
        else:
            row.peg = row.pe_ttm / g
            row.status = "ok"
        rows.append(row)

    # 各公司披露进度不同，实际用到的基准期可能不止一个；
    # 这里回填「占比最高」的那个，并把分布一并交给前端展示。
    if used_periods:
        top = max(used_periods.items(), key=lambda kv: kv[1])[0]
        res.fin_period = top
        basis.update(latest=top, latest_label=report_label(top),
                     parts=ttm_basis(top), pit=True,
                     dist=sorted(used_periods.items(), key=lambda kv: -kv[1]),
                     pe="实时总市值 ÷ 最新已披露 TTM 归母净利（与增速同源同期）")
        res.fin_basis = basis

    valid = [r for r in rows if r.status == "ok"]
    res.n_valid = len(valid)
    # 分母只算「本工具能估值」的部分（A 股 + 港股）。
    # 把美股等根本无法估值的权重算进分母，会让覆盖率被系统性低估。
    w_sum = sum(r.weight or 0 for r in rows if r.status != "oversea")
    vw_sum = sum(r.weight or 0 for r in valid)
    res.coverage = (vw_sum / w_sum * 100.0) if w_sum > 0 else (
        len(valid) / len(rows) * 100.0 if rows else 0.0)

    # 有效样本（PE>0 且正增长）为空时不再提前返回：PE 与全口径加权增速
    # 仍按全部持仓计算，只是 PEG 无意义（g≤0）。
    if not valid:
        res.warn = ("没有可用于计算 PEG 的有效成分股（PE>0 且正增长）；"
                    "PE 与全口径加权增速仍按全部持仓计算")

    # ---- 整体法 PE：按持仓权重加权 ---------------------------------------
    #   PE = Σw / Σ(w / PE_i)   （w = 基金披露的占净值比例）
    # 不能改用「总市值」加权：A 股总市值含大量非流通国有股，
    # 会把银行等低 PE 大市值股权重放大，得到的其实是「全市场口径」PE，
    # 与 ETF 真正持有的那一篮子不是一回事（实测会低估 40% 以上）。
    # PE 只要求「盈利」（pe_ttm > 0），不要求「增速为正」：
    # 负增长但盈利的股票（如阿里、小米、比亚迪）也应计入 PE，否则整体法 PE
    # 会被高 PE 的正增长股拉高，与指数官方 PE（含所有盈利股）对不上。
    num = den = 0.0
    for r in rows:
        w = r.weight
        if w and w > 0 and r.pe_ttm and r.pe_ttm > 0:
            num += w
            den += w / r.pe_ttm
    if den > 0:
        res.pe = num / den

    # ---- 整体法增速（全口径 + 持仓权重加权）--------------------------------
    # 全部成分股都计入（亏损股、负增长股不剔除），并按持仓权重折算：
    #   g = Σ(w·np) / Σ(w·np_prev) − 1
    # 旧口径只留「PE>0 且增速>0」，把负增长整段剔除，增速被系统性抬高
    # （013817 实测：正增长样本 +26% vs 全样本 -108%）。PEG 仍只在 g>0 时有值。
    full_w = [r for r in rows
              if (r.weight or 0) > 0
              and r.np_ttm is not None and r.np_ttm_prev is not None]
    ttm_sum = sum((r.weight or 0) / 100.0 * r.np_ttm for r in full_w)
    ttm_prev_sum = sum((r.weight or 0) / 100.0 * r.np_ttm_prev for r in full_w)
    lt_sum = 0.0
    lt_base = 0.0
    for r in rows:
        w = r.weight or 0
        if w > 0 and r.np_ttm is not None and r.g_latest is not None and r.g_latest != -100:
            base = r.np_ttm / (1 + r.g_latest / 100.0)
            lt_sum += w / 100.0 * r.np_ttm
            lt_base += w / 100.0 * base
    res.np_ttm_sum = ttm_sum
    res.np_ttm_prev_sum = ttm_prev_sum
    # 扣非 TTM（全口径 + 持仓权重加权）：
    # 1) 不筛样本——亏损股、负增长股全部计入（要的是整篮子，不是盈利子集）；
    # 2) 必须乘权重——净利润是绝对额，直接相加等于假设每只都 100% 持有；
    # 3) 港股不披露扣非 / A 股某期缺扣非，用归母净利润顶上（与明细表 † 一致）。
    res.deduct_ttm_sum = sum(
        (r.weight or 0) / 100.0
        * (r.deduct_ttm if r.deduct_ttm is not None else r.np_ttm)
        for r in rows
        if r.np_ttm is not None and (r.weight or 0) > 0) or None
    if ttm_prev_sum:
        res.g_ttm = (ttm_sum - ttm_prev_sum) / abs(ttm_prev_sum) * 100.0
    if lt_base:
        res.g_latest = (lt_sum - lt_base) / abs(lt_base) * 100.0

    g_used = res.g_ttm if growth_mode == "ttm" else res.g_latest
    if res.pe and g_used and g_used > 0:
        res.peg = res.pe / g_used

    # ---- 扣非体系（v9）：与归母完全对称的独立三件套 ----------------------
    # 扣非 PE = Σw / Σ(w·扣非TTM/市值)，只统计扣非TTM>0 的股票：
    # 扣非为负的公司在扣非口径下没有 PE（与归母 PE 的亏损剔除同理）。
    # 扣非增速 = Σ(w·扣非TTM)/Σ(w·上年扣非TTM) − 1，全口径不筛样本；
    # 港股/缺扣非用归母顶上（与 deduct_ttm_sum 完全同一套取值规则）。
    # 注意扣非 PE 与扣非增速的样本天然不同：增速含负扣非股，PE 只含正扣非股。
    dpe_num = dpe_den = 0.0
    dnp_now = dnp_pre = 0.0
    for r in rows:
        w = r.weight or 0
        if w <= 0 or r.np_ttm is None:
            continue
        dnp = r.deduct_ttm if r.deduct_ttm is not None else r.np_ttm
        if dnp is not None and r.mktcap and r.mktcap > 0 and dnp > 0:
            dpe_num += w
            dpe_den += w * dnp / r.mktcap
        if r.np_ttm_prev is not None:
            dnp_p = (r.deduct_ttm_prev
                     if r.deduct_ttm_prev is not None else r.np_ttm_prev)
            if dnp_p is not None:
                dnp_now += w / 100.0 * dnp
                dnp_pre += w / 100.0 * dnp_p
    if dpe_den > 0:
        res.deduct_pe = dpe_num / dpe_den
    if dnp_pre:
        res.g_deduct_ttm = (dnp_now - dnp_pre) / abs(dnp_pre) * 100.0
    if res.deduct_pe and res.g_deduct_ttm and res.g_deduct_ttm > 0:
        res.deduct_peg = res.deduct_pe / res.g_deduct_ttm

    # ---- 等权全样本对照（简单相加，不乘权重）-----------------------------
    # 与加权增速的差额反映持仓集中度：大权重股主导组合增速。
    if full_w:
        s_now = sum(r.np_ttm for r in full_w)
        s_pre = sum(r.np_ttm_prev for r in full_w)
        if s_pre:
            res.g_all = (s_now - s_pre) / abs(s_pre) * 100.0

    # ---- 稳健参考：中位数法 ---------------------------------------------
    pes = sorted(r.pe_ttm for r in valid if r.pe_ttm and r.pe_ttm > 0)
    gs = sorted((r.g_ttm if growth_mode == "ttm" else r.g_latest) for r in valid
                if (r.g_ttm if growth_mode == "ttm" else r.g_latest) is not None
                and (r.g_ttm if growth_mode == "ttm" else r.g_latest) > 0)
    if pes and gs:
        med_pe = _median(pes)
        med_g = _median(gs)
        if med_g > 0:
            res.peg_median = med_pe / med_g

    # ---- 参考：成分股 PEG 的权重加权平均（仅作对照，易被极端值带偏） -----
    wsum = sum(r.weight or 0 for r in valid)
    if wsum > 0:
        res.peg_weighted_avg = sum((r.weight or 0) * r.peg for r in valid) / wsum

    # ---- 等权 PEG：权重口径对照 ------------------------------------------
    # 主 PEG 用全口径加权增速；等权 PEG 用同一 PE ÷ 等权增速（简单相加）。
    # 两者差距反映持仓集中度（大权重股主导），不是样本剔除。
    if res.pe and res.g_all is not None and res.g_all > 0:
        res.peg_all = res.pe / res.g_all

    # ---- 多指标估值：PB / PS / 股息率 / EV·EBIT / 等权 PE ----------------
    # 口径与整体法 PE 完全一致：Σw / Σ(w / 指标_i)，只统计有该指标数据的样本，
    # 并分别给出各指标的权重覆盖率（分母缺样本时值会失真，必须可查）。
    def _harm(items: List[tuple]) -> Tuple[Optional[float], float]:
        """items=[(weight, value>0), ...] → (Σw/Σ(w/v), 参与权重合计)"""
        num = den = 0.0
        for w, v in items:
            if w and w > 0 and v and v > 0:
                num += w
                den += w / v
        return (num / den, num) if den > 0 else (None, num)

    pb_items = [(r.weight, r.pb) for r in rows if r.pb and r.pb > 0]
    res.pb, pb_w = _harm(pb_items)
    res.pb_coverage = (pb_w / all_w * 100.0) if all_w else None
    ps_items = [(r.weight, r.ps) for r in rows if r.ps and r.ps > 0]
    res.ps, ps_w = _harm(ps_items)
    res.ps_coverage = (ps_w / all_w * 100.0) if all_w else None
    pcf_items = [(r.weight, r.pcf) for r in rows if r.pcf and r.pcf > 0]
    res.pcf, _ = _harm(pcf_items)
    # 股息率是比率，用权重加权平均（不是调和平均）
    dv_num = dv_den = 0.0
    for r in rows:
        if r.div_yield is not None and r.weight and r.weight > 0:
            dv_num += r.weight * r.div_yield
            dv_den += r.weight
    res.div_yield = (dv_num / dv_den) if dv_den > 0 else None
    # EV/EBIT：只统计有 EV 材料的样本（目前仅港股），口径近似，界面明示
    ev_items = [(r.weight, r.ev / r.op_ttm) for r in rows
                if r.ev is not None and r.ev > 0 and r.op_ttm and r.op_ttm > 0]
    res.ev_ebit, ev_w = _harm(ev_items)
    # 等权 PE：有效样本算术平均（对照整体法，看大权重的拉偏程度）
    pe_vals = [r.pe_ttm for r in valid if r.pe_ttm and r.pe_ttm > 0]
    res.pe_equal = (sum(pe_vals) / len(pe_vals)) if pe_vals else None

    # ---- 估值分布：盈利/亏损、正增长/负增长样本分开统计 -------------------
    g_of = (lambda r: r.g_ttm if growth_mode == "ttm" else r.g_latest)
    profit = [r for r in rows if r.np_ttm is not None and r.np_ttm > 0]
    loss = [r for r in rows if r.np_ttm is not None and r.np_ttm <= 0]
    pos_g = [r for r in rows if g_of(r) is not None and g_of(r) > 0]
    neg_g = [r for r in rows if g_of(r) is not None and g_of(r) <= 0]
    w_of = lambda rs: sum(r.weight or 0 for r in rs)                # noqa: E731
    pe_of = lambda rs: [r.pe_ttm for r in rs if r.pe_ttm and r.pe_ttm > 0]   # noqa: E731
    g_of_vals = lambda rs: [g_of(r) for r in rs if g_of(r) is not None]      # noqa: E731
    harm_of = lambda rs: _harm([(r.weight, r.pe_ttm) for r in rs
                                if r.pe_ttm and r.pe_ttm > 0])[0]            # noqa: E731
    res.dist = {
        "profit": {"n": len(profit), "weight": round(w_of(profit), 2),
                   "pe_med": _median(pe_of(profit)) if pe_of(profit) else None,
                   "pe_harm": harm_of(profit)},
        "loss": {"n": len(loss), "weight": round(w_of(loss), 2)},
        "growth_pos": {"n": len(pos_g), "weight": round(w_of(pos_g), 2),
                       "g_med": _median(g_of_vals(pos_g)) if g_of_vals(pos_g) else None},
        "growth_neg": {"n": len(neg_g), "weight": round(w_of(neg_g), 2),
                       "g_med": _median(g_of_vals(neg_g)) if g_of_vals(neg_g) else None},
    }
    res.loss_weight = w_of(loss) or 0.0
    res.negg_weight = w_of(neg_g) or 0.0

    # ---- 警示 ------------------------------------------------------------
    warns = []
    if not res.holdings_full:
        warns.append("⚠ 仅获取到季报前十大重仓（该 ETF 未披露半年报/年报全持仓），"
                     "样本不完整，PEG 仅供粗略参考")
    if res.coverage < 50:
        warns.append(f"PEG 有效样本权重覆盖率仅 {res.coverage:.0f}%，正增长样本代表性不足")
    if g_used is not None and g_used > 100:
        warns.append("组合增速异常高（>100%），PEG 会被显著低估，请结合绝对增速判断")
    if g_used is not None and 0 < g_used < 5:
        warns.append("组合增速低于 5%，PEG 对增速极度敏感，参考价值有限")
    if res.g_all is not None and g_used is not None and abs(g_used - res.g_all) > 5:
        warns.append(f"加权增速 {g_used:.1f}% 与等权增速 {res.g_all:.1f}% 相差 "
                     f"{abs(g_used - res.g_all):.1f}pct，组合权重集中，增速受大权重股主导")
    if res.report_date:
        warns.append(f"持仓为 {res.report_date} 定期报告数据，非实时")
    if res.forward and res.forward.get("cons_weight") is not None \
            and res.forward["cons_weight"] < 70:
        warns.append(f"一致预期仅覆盖 {res.forward['cons_weight']:.0f}% 权重"
                     f"（港股/小票无预测数据），前瞻指标代表性不足")

    # ---- 场外/主动基金特有的警示 ------------------------------------------
    if res.equity_ratio is not None and res.equity_ratio < 70:
        warns.append(f"股票仓位仅 {res.equity_ratio:.0f}%，其余为现金/债券/其他资产，"
                     f"PE 与 PEG 只反映其股票部分，不是整个基金的估值")
    if res.oversea_weight and res.oversea_weight > 15:
        warns.append(f"含 {res.oversea_weight:.0f}% 美股等其他市场持仓（{res.n_oversea} 只），"
                     f"没有可用的估值数据，未计入本次计算")
    if res.n_hk and not res.hk_weight:
        warns.append(f"含 {res.n_hk} 只港股，但港股数据尚未初始化，暂未计入；"
                     f"执行「港股初始化」后会自动纳入")
    elif res.n_hk and res.hk_weight:
        warns.append(f"含 {res.hk_weight:.1f}% 港股（{res.n_hk} 只），"
                     f"PE 由「市值 ÷ TTM 股东应占溢利」自算，"
                     f"港股财报为半年度口径，增速更新比 A 股慢半年")
    if res.n_total <= 15 and (res.equity_ratio or 0) < 15:
        warns.append("持仓多为其他基金而非股票（常见于 ETF 联接 / FOF），"
                     "穿透到底层股票才有意义，本结果不适用")
    if res.fund_type and ("混合" in res.fund_type or "股票型" in res.fund_type):
        warns.append(f"主动管理型基金（{res.fund_type}）季度间可能大幅调仓，"
                     f"用 {res.report_date} 的持仓做「实时」PEG，滞后风险高于指数基金")
    res.warn = "；".join(warns)

    # ---- 历史分位：当前值在自身每日快照序列中的位置 ----------------------
    # 数据来自 etf_daily：pe 一期前就有，pb/ps 随 --init-valuation + 回填积累。
    # 单 ETF 至多 2100 行，分位查询时现算（毫秒级），不做预聚合表。
    try:
        hist = list(reversed(store.get_history(etf_code, 99999)))
        if len(hist) >= 60:
            def _pct(vals: List[Optional[float]], cur: Optional[float]) -> Optional[float]:
                if cur is None:
                    return None
                have = [x for x in vals if x is not None]
                # 样本过少的分位没有参考意义（如 PS 序列刚积累一两天），
                # 宁可显示 — 也不要给一个误导值
                if len(have) < 30:
                    return None
                return sum(1 for x in have if x <= cur) / len(have) * 100.0

            def _win(days: Optional[int]) -> List[dict]:
                if not days:
                    return hist
                cutoff = (date.today() - timedelta(days=days)).isoformat()
                return [r for r in hist if r["date"] >= cutoff]

            pl: Dict[str, dict] = {}
            for key, cur in (("pe", res.pe), ("pb", res.pb), ("ps", res.ps)):
                if cur is None:
                    continue
                wins = {"all": _win(None), "y5": _win(365 * 5), "y3": _win(365 * 3)}
                hit_pct = {w: _pct([r.get(key) for r in rs], cur)
                           for w, rs in wins.items()}
                if any(v is not None for v in hit_pct.values()):
                    pl[key] = hit_pct
            res.percentile = pl or None
    except Exception:                                   # noqa: BLE001
        res.percentile = None

    # ---- 前瞻 PE / 前瞻 PEG（机构一致预期，仅 A 股成分）------------------
    # 前瞻 PEG = 当前市值口径的组合前瞻 PE ÷ 2026E→2028E 一致预期净利 CAGR。
    # 港股无免费一致预期数据源（实测），只覆盖 A 股成分，cons_weight 如实反映覆盖比例。
    try:
        a_rows = [r for r in rows
                  if ds.is_a_share(r.market, r.code) and r.weight and r.weight > 0]
        res.forward = forecast.forward_valuation(
            a_rows,
            a_weight_total=sum(r.weight or 0 for r in a_rows),
            all_weight=all_w)
    except Exception:                                   # noqa: BLE001
        res.forward = None

    # ---- 股债性价比（ERP）= 盈利收益率 1/PE − 10Y 国债收益率 --------------
    # 分位基于「本 ETF 的逐日 PE × 当日 10Y」构成的历史 ERP 序列；
    # 10Y 数据增量拉取（滞后 2 天内零网络）。
    try:
        bond.ensure_y10()
        y10_d, y10 = bond.latest_y10()
        if res.pe and res.pe > 0 and y10:
            erp_now = 100.0 / res.pe - y10
            y10_map = bond.load_y10_series("1900-01-01", date.today().isoformat())
            erps = [100.0 / r["pe"] - y10_map[r["date"]]
                    for r in reversed(store.get_history(etf_code, 99999))
                    if r.get("pe") and r["pe"] > 0 and r["date"] in y10_map]
            res.erp = {
                "value": erp_now, "y10": y10, "y10_date": y10_d,
                "pct": (sum(1 for x in erps if x <= erp_now) / len(erps) * 100.0)
                if len(erps) >= 60 else None,
            }
    except Exception:                                   # noqa: BLE001
        res.erp = None

    rows.sort(key=lambda r: (-(r.weight or 0)))
    res.stocks = [asdict(r) for r in rows]

    # 统一口径的结果不写历史序列：etf_daily 的口径是 PIT 混合口径，
    # record_daily 是 INSERT OR REPLACE，会把当天的 mixed 记录覆盖成
    # uniform 值，历史曲线与增量回填（只看日期在不在）都会被口径污染。
    if record and res.g_ttm is not None and not uni_base:
        try:
            d = asdict(res)
            d.pop("stocks", None)
            # v10：当天两套口径的持仓是同一套（都是最新一期），值相同，双写
            store.record_daily([dict(d, basis="fixed"), dict(d, basis="pit")])
        except Exception:                               # noqa: BLE001
            pass                                        # 入库失败不影响分析结果
    return res


def aggregate_history(rows: List[dict], by: str = "month") -> List[dict]:
    """
    把每日序列聚合成 年 / 季 / 月 的柱。
    每个桶取该桶**最后一个交易日**的值作为柱高（代表期末估值），
    同时保留区间内的极值、样本数，供悬停查看。
    """
    def key_of(d: str) -> str:
        if by == "year":
            return d[:4]
        if by == "quarter":
            return f"{d[:4]}Q{(int(d[5:7]) - 1) // 3 + 1}"
        return d[:7]                       # month

    out: List[dict] = []
    cur = None
    for r in sorted(rows, key=lambda x: x["date"]):
        k = key_of(r["date"])
        if cur is None or cur["key"] != k:
            if cur:
                out.append(cur)
            cur = {"key": k, "date": r["date"], "pe": r.get("pe"),
                   "g_ttm": r.get("g_ttm"), "g_all": r.get("g_all"),
                   "peg": r.get("peg"), "coverage": r.get("coverage"),
                   "pb": r.get("pb"), "ps": r.get("ps"),
                   "div_yield": r.get("div_yield"),
                   "deduct_ttm": r.get("deduct_ttm"),
                   "np_ttm": r.get("np_ttm"), "deduct_pe": r.get("deduct_pe"),
                   "g_deduct": r.get("g_deduct"), "deduct_peg": r.get("deduct_peg"),
                   "n_valid": r.get("n_valid"), "n_total": r.get("n_total"),
                   "report_date": r.get("report_date"), "days": 1,
                   "pe_lo": r.get("pe"), "pe_hi": r.get("pe"),
                   "peg_lo": r.get("peg"), "peg_hi": r.get("peg")}
        else:
            cur["date"] = r["date"]
            for f in ("pe", "g_ttm", "g_all", "peg", "coverage", "pb", "ps",
                      "div_yield", "deduct_ttm", "np_ttm", "deduct_pe",
                      "g_deduct", "deduct_peg"):
                if r.get(f) is not None:
                    cur[f] = r[f]
            cur["n_valid"], cur["n_total"] = r.get("n_valid"), r.get("n_total")
            cur["report_date"] = r.get("report_date")
            cur["days"] += 1
            for f, lo, hi in (("pe", "pe_lo", "pe_hi"), ("peg", "peg_lo", "peg_hi")):
                v = r.get(f)
                if v is None:
                    continue
                if cur[lo] is None or v < cur[lo]:
                    cur[lo] = v
                if cur[hi] is None or v > cur[hi]:
                    cur[hi] = v
    if cur:
        out.append(cur)
    return out


def backfill_etf(etf_code: str, days: int = 180, on_stage=None,
                 cancel=None, years: Optional[float] = None,
                 force: bool = False, on_progress=None) -> int:
    """
    回填 ETF 的历史 PE / PEG 序列，写入 etf_daily。

    on_stage(text)          阶段文字
    on_progress(pct, label) 总进度百分比（0~100），用于画进度条。
                            各阶段按耗时加权：持仓/财报 30%、拉 PE 55%、逐日计算 15%。

    思路（全部用免费数据，且不需要等到未来）：
      · 每日个股 PE_TTM 与总市值  ← 东财 RPT_VALUEANALYSIS_DET（一次请求可拿 4 年）
      · 各期基金持仓              ← fundf10，取当日「已知」的最新报告
      · 各期归母净利润            ← 已入库的财报，按当日已披露的最新期构造 TTM
      · ETF 整体法 PE = Σw / Σ(w / PE)   （w 为持仓权重，等价于 Σ市值/Σ净利润）
      · PEG = PE / 该期组合 TTM 净利增速

    「已知」的口径：以报告期的法定披露截止日为准（保守，避免前视偏差）。
    """
    stage = on_stage or (lambda t: None)
    prog = on_progress or (lambda p, l: None)

    def tell(pct: float, label: str) -> None:
        """同时喂给两个回调：stage 只想要文字，进度条还要百分比。"""
        stage(label)
        prog(pct, label)

    # 口径版本升级：历史快照是旧口径算的，必须整体重算。
    # 必须在增量判定之前，否则旧日期会被当成「已算过」而跳过。
    # v10：两套序列各记版本，任一落后就整体重算（重算会同时补齐两套）。
    if not force:
        try:
            if (store.get_calc_version(etf_code, "fixed") < CALC_VERSION
                    or store.get_calc_version(etf_code, "pit") < CALC_VERSION):
                force = True
                tell(1, "计算口径已升级，重算全部历史…")
        except Exception:                           # noqa: BLE001
            pass

    # 补齐最近 35 天缺失的全市场 PE：工具没开的日子会留空档，
    # 「最近一月逐日」会只剩十几天。只在有缺口时回源（每天一次全市场请求）。
    try:
        ds.backfill_recent_market_pe(35)
    except Exception:                               # noqa: BLE001
        pass

    today = date.today()
    if years:
        days = int(years * 365)
    # 个股每日 PE 上游最早只到 2018-01-02，再往前没有数据，直接截断到可用边界
    floor = date(*map(int, ds.pe_history_earliest().split("-")))
    start = max(today - timedelta(days=days), floor)
    start_s = start.strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")

    # ---- 1. 该区间内会用到哪些财报期 ------------------------------------
    tell(3, "确定所需报告期…")
    # 需要覆盖「区间内各期 + 开始日之前最近的一期 + 足够的回退期」。
    # 区间越长，需要的基准期越多（每个季度一个），否则回退链会断。
    n_quarters = max(6, int((today - start).days // 91) + 3)
    needed = financial_periods_for(start, depth=n_quarters)
    if not needed:
        needed = ds.ttm_required_periods()

    # ---- 2. 历史持仓（半年报 / 年报） ------------------------------------
    # 先于财报载入：成分股并集确定后，财报只读这些股票（子集查询），
    # 否则 44 期 × 全市场 1.1 万行 ≈ 50 万行的读取会成为回填的耗时大头。
    tell(6, "载入历史持仓…")
    rep_dates: List[str] = []

    def _try_report(y: int, m: int):
        if cancel and cancel():
            return
        if date(y, m, 1) > today:
            return
        h = ds.fetch_holdings_period(etf_code, y, m)
        if h and h.get("report_date"):
            rep_dates.append(h["report_date"])

    # 长区间需要全部历史持仓（每半年一份）；已入库的直接命中，无额外请求
    years_back = max(3, (today - start).days // 365 + 2)
    for y in range(today.year, today.year - years_back - 1, -1):
        for m in (6, 12):
            _try_report(y, m)
    if not rep_dates:
        cur = ds.fetch_etf_holdings(etf_code)
        if cur.get("report_date"):
            rep_dates.append(cur["report_date"])
    if not rep_dates:
        raise RuntimeError("未找到任何历史持仓，无法回填")
    rep_dates = sorted(set(rep_dates))

    # v10：不再把起点裁到「最旧一期持仓披露截止日」。
    # fixed 口径用最新持仓回溯，这段早期区间同样可算（有行情 + 财报即可）；
    # PIT 口径在这些日期没有「当时已知的持仓」，由 _latest_known 返回 None 自然跳过。
    rep_fixed = rep_dates[-1] if rep_dates else None

    weights_by_rep: Dict[str, Dict[str, float]] = {}
    codes: set = set()
    market_by_code: Dict[str, Optional[str]] = {}
    for rd in rep_dates:
        h = store.load_holdings(etf_code, rd)
        if not h:
            continue
        # 注意用 is not None：权重为 0.00% 的微小持仓也要保留在总数里
        wmap = {s["code"]: s["weight"] for s in h["stocks"]
                if s.get("code") and s.get("weight") is not None}
        for s in h["stocks"]:
            if s.get("code"):
                market_by_code[s["code"]] = s.get("market")
        if wmap:
            weights_by_rep[rd] = wmap
            codes |= set(wmap)
    if not codes:
        raise RuntimeError("历史持仓为空，无法回填")

    # 只保留「至少在某期报告里有正权重」的成分股。
    # 权重为 0 的条目在下面的逐日循环里永远被 `if not w or w <= 0` 跳过，
    # 却照样要载入它们全区间的历史估值与财报——纯浪费。
    # 实测 512480：并集 717 只里只有 148 只有正权重，载入量从 71.5 万格降到 21.4 万格。
    active = {c for wm in weights_by_rep.values()
              for c, w in wm.items() if w and w > 0}
    if active:
        codes = sorted(active)

    # 按市场拆成分股：A 股走 A 库、港股走港股库。
    # 代码位数天然不冲突（A 股 6 位、港股 5 位），合并后可直接当一份数据用。
    hk_codes = [c for c in codes if store.market_of(None, c) is store.HK]
    a_codes = [c for c in codes if c not in set(hk_codes)]
    # 港股只有中报/年报，报告期集合与 A 股不同，两份都要覆盖到。
    # 基准期下限与 A 股 financial_periods_for 同一裁剪逻辑：早于
    # 「区间起点前一个季度」的基准期，其 TTM 生效日必然早于 first_dl，
    # 逐日循环里 _latest_known 会把那些日期整段跳过，财报拉了也用不上。
    # 不裁剪的话（005889 实测）会去逐期拉 2012~2018 年的全市场港股财报，
    # 每期 1~2 分钟且无进度反馈，进度条在 15% 停半小时起步，像卡死。
    hk_periods: set = set()
    if hk_codes:
        hk_periods = set(hk.hk_fin_periods(
            depth=max(6, n_quarters),
            min_deadline=start - timedelta(days=110)))
    all_periods = sorted(set(needed) | hk_periods, reverse=True)

    # ---- 2b. 财报：已入库的期只读成分股子集，缺失的期才回源全市场 --------
    tell(15, f"载入 {len(all_periods)} 期财报…")
    fin: Dict[str, Dict[str, dict]] = {}
    missing = [p for p in needed if not store.has_fin(p)]
    if missing:
        def _fin_stage(t: str) -> None:
            # 文本形如「拉取全市场财报 3/22 期（45 页）…」，从中取进度
            m = re.search(r"(\d+)\s*/\s*(\d+)\s*期", t)
            pct = 15 + 15.0 * int(m.group(1)) / max(1, int(m.group(2))) if m else 15.0
            tell(pct, t)

        got = _fetch_periods(missing, on_stage=_fin_stage, cancel=cancel)
        fin.update(got)
    hk_fin_list = [p for p in all_periods if p in hk_periods]
    hk_fin_done = 0
    for p in all_periods:
        if cancel and cancel():
            return 0
        if p not in fin:
            fin[p] = store.load_fin_subset(p, a_codes)
        if p in hk_periods:
            hk_fin_done += 1
            base = 15 + 15.0 * (hk_fin_done - 1) / max(1, len(hk_fin_list))
            # 已入库的期 fetch_hk_financials 零网络秒回；缺失的期要拉全市场
            # 约 5 页、实测 1~2 分钟。必须逐页上报进度，否则进度条会在
            # 15% 停上半小时起步（005889 实测），用户只能以为卡死了。
            def _hk_page(pg: int, total: int, _b=base, _p=p) -> None:
                tell(_b, f"载入财报…（港股 {_p} 第 {pg}/{total} 页）")

            got = _fetch_hk_fin_guarded(p, on_progress=_hk_page)
            if got:
                tgt = fin.setdefault(p, {})
                for c in hk_codes:                  # 只取持仓里的，别把全市场塞进来
                    if c in got:
                        tgt[c] = got[c]
    fin = {p: d for p, d in fin.items() if d}
    if not fin:
        raise RuntimeError("财报数据为空，无法回填")
    fin_periods = sorted(fin.keys(), reverse=True)

    # ---- 3. 增量判定（必须排在拉取之前）----------------------------------
    # 历史数据不可变（财报定稿、历史 PE 既成事实），已算过的日期不重算。
    # 放在拉取之前是刻意的：持仓报告会越攒越多，成分股并集随之变大，
    # 若先拉取，即便结果早已算满，也会为「新发现的历史成分股」白跑一遍
    # （510300 实测约 790 只 × 1.36 s ≈ 18 分钟的纯浪费）。
    # 若计算方法变更，用 force=True 或 --clear-history 强制重算。
    # v10：两套序列各自判断增量——fixed 覆盖全区间，pit 只可能出现在
    # 「最旧一期持仓披露截止日」之后，因此早期日期不会因缺 pit 被反复重算。
    have_fixed: set = set()
    have_pit: set = set()
    div_have_fixed: set = set()
    div_have_pit: set = set()
    if not force:
        for _basis, _hs, _ds in (("fixed", have_fixed, div_have_fixed),
                                 ("pit", have_pit, div_have_pit)):
            hist = store.get_history(etf_code, 6000, _basis)
            # 残缺快照（有效样本 < 3 只）不算「已算满」：这类日期是当天港股数据
            # 残缺时算出来的（如 513180 某天只有 1 只有效样本、PE 高达 150 倍），
            # 必须重算。n_valid 为 None 是旧数据，不据此判定，保持原样。
            _hs |= {r["date"] for r in hist
                    if r.get("n_valid") is None or r["n_valid"] >= 3}
            _ds |= {r["date"] for r in hist if r.get("div_calc")}
    pit_start = min((ds.period_deadline(r) for r in rep_dates), default=None)
    pit_start_s = str(pit_start) if pit_start else None

    def _need(d: str) -> bool:
        if d not in have_fixed or d not in div_have_fixed:
            return True
        return bool(pit_start_s and d >= pit_start_s
                    and (d not in have_pit or d not in div_have_pit))

    dates = store.pe_dates(start_s, today_s)
    todo_dates = dates
    if dates and not force:
        # 缺 PE 的日期，或「有 PE 但还没补过股息率」的日期，都要算。
        # 后者是升级老库的一次性补算：算完后 div_calc=1，下次秒回。
        todo_dates = [d for d in dates if _need(d)]
        if not todo_dates:
            return len(dates)

    # ---- 4. 成分股历史 PE ------------------------------------------------
    # 只拉「覆盖不到 start 且没问过上游」的股票（判定见 store.pe_pending）。
    # A 股与港股各自用自己的库和自己的抓取函数，进度条 30%→70%→85%。
    todo_a = store.pe_pending(a_codes, start_s)
    n_a = len(todo_a)
    for i, c in enumerate(todo_a, 1):
        if cancel and cancel():
            return 0
        tell(30 + 40.0 * (i - 1) / n_a,
             f"拉取成分股历史 PE {i}/{n_a}（共 {len(a_codes)} 只，"
             f"{len(a_codes) - n_a} 只已入库）…")
        try:
            ds.fetch_stock_pe(c, start_s)
        except Exception:                               # noqa: BLE001
            continue
    if not todo_a:
        tell(70, "A 股历史 PE 已全部入库")

    todo_hk = store.HK.pe_pending(hk_codes, start_s) if hk_codes else []
    gap_hk: List[str] = []
    if hk_codes:
        try:
            # 内部日历空洞（K 线拉取时刻 → 下次快照之间的缺口）：逐只补拉
            # 覆盖回填区间，否则那些天该股被静默跳过、覆盖率骤降
            gap_hk = [c for c in hk.detect_kline_gaps(hk_codes, start_s, today_s)
                      if c not in set(todo_hk)]
        except Exception:                               # noqa: BLE001
            gap_hk = []
    if todo_hk or gap_hk:
        try:
            shares_now = {c["code"]: c.get("shares")
                          for c in hk.fetch_hk_list() if c.get("shares")}
        except Exception:                               # noqa: BLE001
            shares_now = {}
        pulls = [("补拉" if c in set(gap_hk) else "拉取", c)
                 for c in todo_hk + gap_hk]
        n_hk = len(pulls)
        for i, (verb, c) in enumerate(pulls, 1):
            if cancel and cancel():
                return 0
            tell(70 + 15.0 * (i - 1) / n_hk, f"{verb}港股历史行情 {c} {i}/{n_hk}…")
            try:
                closes = hk.fetch_hk_kline(c, start_s)
                if closes:
                    store.HK.save_stock_pe(
                        hk.attach_mktcap(c, closes, shares_now.get(c)))
                store.HK.set_pe_fetch_log(c, start_s)
            except Exception:                           # noqa: BLE001
                continue
    elif hk_codes:
        tell(85, "港股历史行情已全部入库")

    # 交易日历：两个市场的日历不完全一致（假期不同）。
    # 混合基金取并集（覆盖率会如实反映当天缺了哪部分），纯港股基金只用港股日历。
    dates = store.pe_dates(start_s, today_s)
    if hk_codes:
        hk_dates = store.HK.pe_dates(start_s, today_s)
        dates = hk_dates if not a_codes else sorted(set(dates) | set(hk_dates))
    if not dates:
        raise RuntimeError("未获取到任何历史 PE 数据")
    todo_dates = dates if force else [d for d in dates if _need(d)]
    if not todo_dates:
        return len(dates)

    tell(86, f"载入 {len(codes)} 只成分股的历史估值…")
    pe_map = store.load_stock_pe(a_codes, start_s, today_s)
    if hk_codes:
        pe_map.update(store.HK.load_stock_pe(hk_codes, start_s, today_s))

    # 股息率数据：A 股分红明细 → (除息日升序, 前缀和)，供逐日滚动窗口；
    # 港股用财报 dps（近一年每股分红，DPS_HKD），在逐日循环里按 PIT 基准期取。
    # 分红未初始化时 div_series 为空，A 股股息率列为 NULL，不误报。
    div_series: Dict[str, tuple] = {}
    if a_codes:
        div_map = store.load_stock_div(a_codes)
        for c, evs in div_map.items():
            if evs:
                xs: List[str] = []
                pre: List[float] = []
                acc = 0.0
                for ex, dps in evs:
                    acc += dps
                    xs.append(ex)
                    pre.append(acc)
                div_series[c] = (xs, pre)
    # 分红数据可用性：A 股分红已初始化（div_series 非空）或纯港股（无 A 股成分）。
    # 分红未初始化时 div_calc 保持 0 —— 不能因为「暂时算不出」就标记完成，
    # 否则分红数据补全后增量判定会误以为已算过、永远不再重算（512480 实测踩坑）。
    div_ready = bool(div_series) or not a_codes

    # ---- 5. 构建每只股票的 TTM 时间线（按真实披露日生效） ------------------
    tell(88, "按各公司披露日构建 TTM 时间线…")
    # A 股走日历季度公式；港股按公司财年构造（12/3/6/9 月年结都正确，
    # 3 月年结公司的财报在 03-31/09-30 期，A 股公式会静默漏掉它们）
    timelines = build_pit_timelines(fin, a_codes)
    if hk_codes:
        try:
            timelines.update(hk.build_hk_ttm_timelines(hk_codes))
        except Exception:                               # noqa: BLE001
            pass                                        # 构造失败退化为「该股无 TTM」

    # ---- 6. 逐交易日计算 --------------------------------------------------
    etf_name = etf_code
    try:
        etf_name = next((e["name"] for e in ds.fetch_etf_list() if e["code"] == etf_code),
                        etf_code)
    except Exception:                                   # noqa: BLE001
        pass

    n_dates = len(todo_dates)
    out: List[dict] = []
    wmap_fixed = weights_by_rep.get(rep_fixed) if rep_fixed else None

    def calc_day(d: str, wmap: Dict[str, float], rep: str) -> Optional[dict]:
        """
        某一天、某套持仓映射下的全部组合指标。
        wmap 决定口径：fixed=最新一期持仓，pit=当日已知持仓；
        财报时间线与行情都是同一套 PIT 数据（只换持仓，不换财报口径）。
        无可用行情（pe_den<=0）时返回 None。
        """
        # 财报：逐只按真实披露日对齐（PIT），谁先披露谁先进组合
        # 样本 —— 只保留 PE>0 且增速>0 的成分股
        # PE   —— Σw / Σ(w / PE_i)，w 为基金披露的持仓权重
        # PB/PS 与 PE 同口径：只统计有数据的样本，覆盖率随列数如实反映
        pe_num = pe_den = 0.0                     # PE 用：所有盈利股
        w_sum = 0.0                               # coverage 用：有效样本（PE>0 且正增长）
        np_now = np_pre = 0.0                     # 增速：全口径加权 TTM
        all_now = all_pre = 0.0                   # 等权全样本对照（简单相加）
        dnp_all = 0.0                             # 扣非净利：全口径 × 持仓权重
        # 扣非 PEG 三件套（v9）：增速成对累加（两期都有才计入），
        # 扣非 PE 只收扣非TTM>0 且当日有市值的样本
        dnp_g_now = dnp_g_pre = 0.0
        dpe_num = dpe_den = 0.0
        pb_num = pb_den = ps_num = ps_den = 0.0
        div_num = div_den = 0.0
        n_valid = 0
        for c, w in wmap.items():
            if not w or w <= 0:
                continue
            cell = (pe_map.get(c) or {}).get(d)
            # ---- 股息率：在 PE/增速过滤之前累积。亏损股照样分红，
            # 不能因被 PE 剔除而连带剔除其股息，否则组合股息率被高估。
            # A 股 = 近 12 个月累计每股分红 ÷ 当日收盘价（分红明细滚动窗口）
            if cell and cell[2] and cell[2] > 0 and c in div_series:
                xs, pre = div_series[c]
                ttm = _ttm_dps(xs, pre, d)
                if ttm and ttm > 0:
                    div_num += w * ttm / cell[2] * 100.0
                    div_den += w
            hit = pit_as_of(timelines.get(c), d)
            if not hit:
                continue
            _, np_ttm, np_ttm_prev, g, _base, _rev, _op, _dnp, _dnp_p = hit
            # 增速（全口径 + 持仓权重加权）：所有有两期 TTM 的持仓都计入，
            # 亏损股/负增长股不剔除；g = Σ(w·np)/Σ(w·np_prev) − 1。
            # all_now/all_pre 是等权简单相加，仅作对照（衡量权重集中度影响）。
            if np_ttm is not None and np_ttm_prev is not None:
                np_now += (w / 100.0) * np_ttm
                np_pre += (w / 100.0) * np_ttm_prev
                all_now += np_ttm
                all_pre += np_ttm_prev
            # 扣非净利（全口径）：所有持仓股都计入，并按持仓权重折算。
            # 不筛 PE/增速——这是「整篮子实际盈利」，不是 PE/PEG 的盈利子集；
            # 净利润是绝对额，必须乘权重（简单相加 = 假设每只都 100% 持有）。
            # 港股不披露扣非 / A 股某期缺扣非，用归母净利润顶上。
            if np_ttm is not None:
                dnp_all += (w / 100.0) * (_dnp if _dnp is not None else np_ttm)
            # 扣非 PEG 三件套（v9）：
            # ① 扣非增速：两期扣非都可得才成对累加（港股/缺扣非用归母顶上）
            # ② 扣非 PE：只收扣非TTM>0 且当日有市值的样本
            if np_ttm is not None and np_ttm_prev is not None:
                dnp_c = _dnp if _dnp is not None else np_ttm
                dnp_p = _dnp_p if _dnp_p is not None else np_ttm_prev
                dnp_g_now += (w / 100.0) * dnp_c
                dnp_g_pre += (w / 100.0) * dnp_p
            dnp_v = _dnp if _dnp is not None else np_ttm
            if cell and cell[1] and cell[1] > 0 and dnp_v is not None and dnp_v > 0:
                dpe_num += w
                dpe_den += w * dnp_v / cell[1]
            # 港股股息率：财报近一年每股分红（DPS_HKD）÷ 当日收盘价
            if (c not in div_series) and cell and cell[2] and cell[2] > 0 and _base:
                hk_dps = ((fin.get(_base) or {}).get(c) or {}).get("dps")
                if hk_dps and hk_dps > 0:
                    div_num += w * hk_dps / cell[2] * 100.0
                    div_den += w
            # PE：所有盈利股（不筛增速，负增长但盈利的也计入）
            pe = None
            if cell and cell[1] and np_ttm and np_ttm > 0:
                pe = cell[1] / np_ttm            # cell = (pe_ttm, mktcap, close, pb, ps)
            if pe is None or pe <= 0:            # 兜底：东财当日 PE_TTM
                pe = cell[0] if cell else None
            if pe is not None and pe > 0:
                pe_num += w
                pe_den += w / pe
            # PB：所有有数据的股票（不筛增速，与 analyze_etf 口径一致）
            # A 股用当日 PB_MRQ 列（老数据可能为 NULL）；港股自算市值÷净资产
            # 净资产按 PIT 基准期取（当日之前最新已披露的归母净资产）
            pb_i = cell[3] if cell else None
            if not pb_i and _base:
                eq = ((fin.get(_base) or {}).get(c) or {}).get("equity")
                if eq and eq > 0 and cell and cell[1] and cell[1] > 0:
                    pb_i = cell[1] / eq
            if pb_i and pb_i > 0:
                pb_num += w
                pb_den += w / pb_i
            # PS：所有有营收数据的股票（不筛增速，与 analyze_etf 口径一致）
            if _rev and _rev > 0 and cell and cell[1] and cell[1] > 0:
                ps_num += w
                ps_den += w * _rev / cell[1]     # w / PS_i = w·rev/mc
            # coverage：有效样本 = PE>0 且增速>0，只用于覆盖率统计与
            # 中位数/加权平均 PEG 的样本定义，与组合增速口径无关
            # （增速已改为全口径加权，在 hit 之后即已累加）。
            if g is None or g <= 0:
                continue
            if pe is None or pe <= 0:
                continue
            w_sum += w
            n_valid += 1

        if pe_den <= 0:
            return None
        pe_etf = pe_num / pe_den
        g = ((np_now - np_pre) / abs(np_pre) * 100.0) if np_pre else None
        g_eq = ((all_now - all_pre) / abs(all_pre) * 100.0) if all_pre else None
        # 分母只算「本工具能估值」的持仓（A 股 + 港股）。
        # 把美股等根本拿不到数据的权重算进去，覆盖率会被系统性低估。
        w_avail = sum(w for c, w in wmap.items()
                      if w and w > 0 and store.is_valuable(market_by_code.get(c), c))
        return {
            "code": etf_code, "name": etf_name, "date": d,
            "pe": pe_etf, "g_ttm": g, "g_all": g_eq,
            "peg": (pe_etf / g) if (g and g > 0) else None,
            "coverage": (w_sum / w_avail * 100.0) if w_avail else 0.0,
            "n_valid": n_valid, "n_total": len(wmap), "report_date": rep,
            "pb": (pb_num / pb_den) if pb_den > 0 else None,
            "ps": (ps_num / ps_den) if ps_den > 0 else None,
            "div_yield": (div_num / div_den) if div_den > 0 else None,
            "div_calc": 1 if div_ready else 0,
            "deduct_ttm_sum": dnp_all or None,
            # v9：归母净利合计（全口径加权，与扣非净利对称）+ 扣非 PEG 三件套
            "np_ttm": np_now or None,
            "deduct_pe": (dpe_num / dpe_den) if dpe_den > 0 else None,
            "g_deduct": (((dnp_g_now - dnp_g_pre) / abs(dnp_g_pre) * 100.0)
                         if dnp_g_pre else None),
            "deduct_peg": ((dpe_num / dpe_den)
                           / ((dnp_g_now - dnp_g_pre) / abs(dnp_g_pre) * 100.0)
                           if (dpe_den > 0 and dnp_g_pre
                               and (dnp_g_now - dnp_g_pre) > 0) else None),
        }

    # ---- 逐日双写：fixed（默认，最新持仓回溯）+ pit（当时已知持仓） ----
    for _i, d in enumerate(todo_dates, 1):
        if cancel and cancel():
            break
        # 逐日上报太密（2109 次），每 25 天报一次足够撑起进度条
        if _i % 25 == 1 or _i == n_dates:
            tell(88 + 12.0 * _i / n_dates, f"计算每日 PEG {_i}/{n_dates}…")
        # 主口径：最新一期持仓回溯。财报/行情仍是 PIT，只固定持仓，
        # 这样柱子之间可比、增速分母与柱子同源（消除调仓噪音）。
        if wmap_fixed:
            row = calc_day(d, wmap_fixed, rep_fixed)
            if row:
                row["basis"] = "fixed"
                out.append(row)
        # 对照口径：基金定期报告没有公开披露日，按法定截止日取当日已知的
        # 最新一期持仓（无前视，但柱子之间含调仓影响）。
        rep = _latest_known(rep_dates, d, ds.period_deadline)
        if rep:
            wmap = weights_by_rep.get(rep)
            if wmap:
                row = calc_day(d, wmap, rep)
                if row:
                    row["basis"] = "pit"
                    out.append(row)

    if out:
        store.record_daily(out)
    # 记录本次口径版本：两套序列各自独立，版本一致才只做增量
    store.set_calc_version(etf_code, CALC_VERSION, "fixed")
    store.set_calc_version(etf_code, CALC_VERSION, "pit")
    tell(100, f"完成，写入 {len(out)} 行（{len(todo_dates)} 天 × 双口径）")
    return len(out)


def _latest_known(cands: List[str], d: str, deadline_fn) -> Optional[str]:
    """在日期 d，市场「已知」的最新一期（以披露截止日为准，避免前视偏差）"""
    ok = [c for c in cands if deadline_fn(c) <= date(*map(int, d.split("-")))]
    return max(ok) if ok else None


def _median(xs: List[float]) -> float:
    n = len(xs)
    if n == 0:
        return float("nan")
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


# --------------------------------------------------------------------------- #
#  批量：多只 ETF 的 PEG 排行
# --------------------------------------------------------------------------- #
def analyze_many(etf_list: List[dict], growth_mode: str = "ttm",
                 on_progress=None, on_stage=None, cancel=None,
                 workers: int = 5, pe_mode: str = "mixed") -> List[dict]:
    """
    共享行情与财报数据，批量计算多只 ETF 的 PEG。

    on_progress(i, total, name) —— 每只 ETF 完成回调
    on_stage(text)              —— 准备阶段回调（拉行情 / 拉财报 / 抓持仓）
    cancel()                    —— 返回 True 时尽快中止
    workers                     —— 抓持仓的并发数，串行时一只卡住会拖垮整批
    """
    import concurrent.futures as cf

    def stage(t):
        if on_stage:
            on_stage(t)

    stage("拉取全市场实时行情…")
    quotes = ds.fetch_realtime_quotes()
    if cancel and cancel():
        return []

    fin = load_financials(on_stage=stage, cancel=cancel)
    if cancel and cancel():
        return []

    total = len(etf_list)
    stage(f"穿透 {total} 只 ETF 的持仓…")

    # 第一阶段：并发抓持仓。这一步才是耗时大头，必须逐只上报进度，
    # 否则界面会在几十秒内看不到任何变化（容易被误判成卡死）。
    holdings: Dict[int, dict] = {}
    done = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(ds.fetch_etf_holdings, e["code"]): i
                for i, e in enumerate(etf_list)}
        for fu in cf.as_completed(futs):
            if cancel and cancel():
                for f in futs:
                    f.cancel()
                return []
            i = futs[fu]
            try:
                holdings[i] = fu.result()
            except Exception:                           # noqa: BLE001
                holdings[i] = {"stocks": [], "report_date": None, "full": False}
            done += 1
            if on_progress:
                on_progress(done, total, etf_list[i].get("name", etf_list[i]["code"]))

    # 第二阶段：纯内存计算，很快，不再逐只回调
    stage("汇总计算 PEG…")
    out: List[dict] = []
    for i, etf in enumerate(etf_list):
        if cancel and cancel():
            return []
        try:
            r = analyze_etf(etf["code"], etf.get("name", ""), growth_mode,
                            quotes=quotes, fin=fin, record=False,
                            holdings=holdings.get(i) or {"stocks": [], "report_date": None,
                                                         "full": False},
                            pe_mode=pe_mode)
        except Exception as exc:                       # noqa: BLE001
            r = ETFResult(code=etf["code"], name=etf.get("name", ""),
                          warn=f"计算失败：{exc}", report_date=None, fin_period=None)
        d = asdict(r)
        d.pop("stocks", None)
        d["market_cap"] = etf.get("market_cap")
        d["price"] = etf.get("price")
        d["pct"] = etf.get("pct")
        out.append(d)

    # 批量写入当日快照（逐只写会产生 N 次提交）。
    # uniform 口径不写：etf_daily 序列固定为 PIT 混合口径，别被口径切换污染
    if pe_mode != "uniform":
        try:
            store.record_daily([r for r in out if r.get("peg") is not None])
        except Exception:                               # noqa: BLE001
            pass
    return out


def batch_backfill_rank(top: int = 160, on_progress=None, cancel=None) -> dict:
    """
    为「PE 历史百分位排行」批量准备数据。

    排行的数据源是 etf_daily 的逐日快照——只有分析过 / 回填过历史的
    ETF 才有 PE 序列，默认只覆盖几十只。本任务按流通市值取前 top 只
    场内 ETF（剔除债券/商品/货币等无股票持仓的），逐只回填全可用历史
    （约 8.6 年，上游个股每日 PE 最早 2018-01-02），把排行覆盖面从
    「恰好回填过的几十只」滚雪球到「规模前 top 只」。

    单只首次约 1~5 分钟（个股历史 PE 已初始化时，耗时大头是本地逐日
    计算），全量几十只需数十分钟到数小时。backfill_etf 的增量机制保证：
    已算满的直接秒回，中断后重跑自动续传，重复执行没有额外成本。
    on_progress(done, total, label)
    """
    prog = on_progress or (lambda *a: None)
    etfs = ds.fetch_etf_list()              # 已按规模（市值）降序
    targets = [e for e in etfs
               if not any(k in e["name"] for k in ds.SKIP_KEYWORDS)][:max(1, top)]
    total = len(targets)
    ok = 0
    failed: List[str] = []
    for i, e in enumerate(targets, 1):
        if cancel and cancel():
            break
        prog(i - 1, total, f"回填 {e['name']}（{e['code']}）历史 {i}/{total}")
        try:
            backfill_etf(e["code"], days=3650, cancel=cancel)
            ok += 1
        except Exception:                               # noqa: BLE001
            failed.append(e["code"])                    # 单只失败不影响整体，重跑续传
    prog(total, total, "完成")
    return {"total": total, "ok": ok, "failed": len(failed),
            "failed_codes": failed[:10], "cancelled": bool(cancel and cancel())}


def estimate_day_pct(stocks) -> dict:
    """
    按最新定期报告持仓成分股 × 当日实时涨跌 × 权重加权，估算基金当日涨跌
    （天天基金盘中估值口径，现金/债券等非股票部分按 0 贡献）。
    stocks 为 ETFResult.stocks（dict 列表：code/weight/market）。
    返回 {pct, coverage, n_used, n_total}：
      pct       = 基金整体估算涨跌 %（Σ(w_i×pct_i)/100）
      coverage  = 参与估算的权重占比 %（停牌/美股/无数据成分不计入）
    """
    quotes = ds.fetch_realtime_quotes()
    hk_codes = [s.get("code") for s in stocks
                if store.market_of(s.get("market"), s.get("code")) is store.HK]
    hk_pct = ds.fetch_hk_realtime_pct(hk_codes)
    num = 0.0
    den = 0.0
    n_used = 0
    for s in stocks:
        code = s.get("code")
        w = s.get("weight") or 0.0
        if w <= 0:
            continue
        p = None
        q = quotes.get(code)
        if q and q.get("pct") is not None:
            p = q["pct"]
        elif code in hk_pct:
            p = hk_pct[code]
        if p is None:
            continue
        num += w * p
        den += w
        n_used += 1
    pct = (num / 100.0) if den > 0 else None
    return {"pct": pct, "coverage": den, "n_used": n_used, "n_total": len(stocks)}
