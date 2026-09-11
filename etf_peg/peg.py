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
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import datasource as ds
from . import hk
from . import store


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
    g_ttm: Optional[float] = None         # TTM 同比 %
    g_latest: Optional[float] = None      # 最新报告期累计同比 %（东财口径）
    peg: Optional[float] = None
    # 本行计算依据：用的是哪一期财报（含"2026 年半年报"这类可读名）、何时披露
    fin_period: Optional[str] = None
    fin_label: Optional[str] = None
    fin_date: Optional[str] = None
    market: Optional[str] = None      # 市场标识：1/0=A股，116=港股，105/106/107=美股
    # 近若干期的增速（与 fin_basis.growth_series 的期别一一对应）
    g_series: Optional[List[Optional[float]]] = None
    status: str = "ok"                    # ok / no_quote / no_pe / no_growth / no_data


@dataclass
class ETFResult:
    code: str
    name: str
    report_date: Optional[str]
    fin_period: Optional[str]
    holdings_full: bool = True      # False = 只拿到季报前十大，样本不完整
    pe_date: Optional[str] = None   # PE 数据日期（PE-TTM 口径，滞后 1~2 个交易日）
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
    mktcap_sum: Optional[float] = None
    warn: str = ""
    stocks: List[dict] = field(default_factory=list)
    updated_at: str = ""


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


def build_pit_timelines(fin: Dict[str, Dict[str, dict]],
                        codes: List[str]) -> Dict[str, List[tuple]]:
    """
    为每只股票构建 TTM 时间线，实现 PIT（point-in-time）对齐。

    返回 {code: [(生效日, np_ttm, np_ttm_prev, g, 基准期), ...]}（按生效日升序）

    「生效日」= 构造这个 TTM 所需全部报告的**最晚真实披露日**。
    例如某公司 7/28 就披露了半年报，那它的 TTM 从 7/28 起就可用，
    不必等到 8/31 的法定截止日；反之拖到 8/28 披露的，就按 8/28。

    这样每家公司各自对齐，不会出现「用 A 公司 7 月就公开的数据，
    却等到 8/31 才计入」这种失真，也不会有前视偏差。
    """
    periods = sorted(fin.keys(), reverse=True)
    out: Dict[str, List[tuple]] = {}

    for c in codes:
        tl: List[tuple] = []
        for p in periods:
            ly, lq = _parse_period(p)
            deps = ds.ttm_required_periods(p)
            vals, dates, ok = {}, [], True
            for dp in deps:
                row = (fin.get(dp) or {}).get(c)
                if not row or row.get("parent_np") is None:
                    ok = False
                    break
                dy, dq = _parse_period(dp)
                vals[(dy, dq)] = row["parent_np"]
                # 缺披露日时退回法定截止日（保守）
                dates.append(row.get("notice_date") or str(ds.period_deadline(dp)))
            if not ok:
                continue

            if lq == 4:                       # 年报本身即 TTM
                ttm, ttm_prev = vals[(ly, 4)], vals[(ly - 1, 4)]
            else:
                ttm = vals[(ly, lq)] + vals[(ly - 1, 4)] - vals[(ly - 1, lq)]
                ttm_prev = vals[(ly - 1, lq)] + vals[(ly - 2, 4)] - vals[(ly - 2, lq)]
            if ttm is None or ttm_prev is None:
                continue
            g = (ttm - ttm_prev) / abs(ttm_prev) * 100.0 if ttm_prev else None
            tl.append((max(dates), ttm, ttm_prev, g, p))

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


def _ttm_for(fin: Dict[str, Dict[str, dict]], code: str,
             base: str) -> Tuple[Optional[float], Optional[float]]:
    """某只股票在指定基准期上的 (TTM归母净利, 去年同期TTM归母净利)；缺数据返回 (None, None)"""
    ly, lq = _parse_period(base)
    vals: Dict[Tuple[int, int], float] = {}
    for dp in ds.ttm_required_periods(base):
        row = (fin.get(dp) or {}).get(code)
        if not row or row.get("parent_np") is None:
            return None, None
        vals[_parse_period(dp)] = row["parent_np"]
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


def quarterly_growth(codes: List[str], bases: List[str],
                     with_stocks: bool = False):
    """
    按报告期逐期计算组合 TTM 同比增速。

    返回 (series, per_stock)
      series    = [{period, label, g, used, total, pe?}]  组合口径（整体法）
      per_stock = {code: [g_or_None, ...]}                每只股票的逐期增速
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
        merged = store.load_fin_subset(p, a_codes)
        if hk_codes and p.endswith(("-06-30", "-12-31")):
            # 港股只有中报/年报，一季报/三季报期直接跳过，别白打请求。
            # fetch_hk_financials 自带本地库缓存：已入库的期零网络开销，
            # 缺失的期自动补拉入库（与 A 股 ensure_series_periods 同一思路）。
            try:
                got = hk.fetch_hk_financials(p)
            except Exception:                               # noqa: BLE001
                got = None
            if got:
                merged.update({c: got[c] for c in hk_codes if c in got})
        fin[p] = merged

    series, per = [], {c: [] for c in codes} if with_stocks else None
    for p in bases:
        now = pre = 0.0            # 全样本：含亏损与负增长
        vnow = vpre = 0.0          # 正增长样本：只留增速 > 0 的
        used = vused = 0
        for c in codes:
            ttm, ttm_prev = _ttm_for(fin, c, p)
            g = ((ttm - ttm_prev) / abs(ttm_prev) * 100.0
                 if (ttm is not None and ttm_prev) else None)
            if with_stocks:
                per[c].append(g)
            if ttm is None or ttm_prev is None:
                continue
            now += ttm
            pre += ttm_prev
            used += 1
            if g is not None and g > 0:
                vnow += ttm
                vpre += ttm_prev
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
    return series, per


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
            if len(d) >= 1000:                         # 条目过少视为尚未披露
                out[p] = d
        if on_stage:
            on_stage(f"拉取全市场财报 {snapshot[0]}/{len(periods)} 期（{snapshot[1]} 页）…")
        return d

    if on_stage:
        on_stage(f"拉取全市场财报 0/{len(periods)} 期…")
    with cf.ThreadPoolExecutor(max_workers=min(4, len(periods))) as pool:
        list(pool.map(one, periods))
    return out


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
                series_quarters: int = 9) -> ETFResult:
    """
    growth_mode: 'ttm'（TTM 归母净利同比，稳健）| 'latest'（最新报告期累计同比，灵敏）
    as_of: 计算基准日，默认今天。每只股票只采用该日**之前已披露**的财报。
    series_quarters: 额外计算最近多少个报告期的组合增速（0 表示不算）。
    """
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

    # 近 N 个报告期各自的组合增速 + 每只股票的逐期增速
    per_stock_series: Optional[Dict[str, List[Optional[float]]]] = None
    if series_quarters > 0:
        try:
            ensure_series_periods(series_quarters)
            bases = series_bases(series_quarters)
            sers, per_stock_series = quarterly_growth(
                [s["code"] for s in stocks], bases, with_stocks=True)
            res.growth_series = sers
            basis["series_bases"] = bases
        except Exception:                               # noqa: BLE001
            res.growth_series, per_stock_series = None, None

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
            for p in hk.hk_fin_periods(depth=4):
                got = hk.fetch_hk_financials(p)
                if got:
                    fin.setdefault(p, {}).update(got)   # 5 位与 6 位代码不会撞
        except Exception:                               # noqa: BLE001
            pass                                        # 港股不可用时退化为「只算 A 股」
        try:
            hk_d = hk.ensure_hk_market_snapshot()
            if hk_d:
                m = store.HK.load_stock_pe(hk_codes, hk_d, hk_d)
                # cell = (pe_ttm, mktcap, close)。close 一并取出：
                # 港股不在 A 股实时行情里，不补的话股价一栏是空的
                hk_mc = {c: ((m.get(c) or {}).get(hk_d) or (None,) * 3)[1] for c in m}
                hk_close = {c: ((m.get(c) or {}).get(hk_d) or (None,) * 3)[2]
                            for c in m}
        except Exception:                               # noqa: BLE001
            hk_mc = {}
            hk_close = {}

    # PE 统一用 PE-TTM（东财估值库）：历史序列只有这个口径，
    # 实时行情的「市盈率(动态)」是年化值、无历史，两边混用会导致曲线断裂。
    pe_date = None
    pe_src: Dict[str, Optional[float]] = {}
    try:
        pe_date = ds.ensure_market_pe()
        if pe_date:
            m = store.load_stock_pe([s["code"] for s in stocks
                                     if s["code"] not in set(hk_codes)], pe_date, pe_date)
            pe_src = {c: (m.get(c) or {}).get(pe_date, (None,) * 3)[0] for c in m}
    except Exception:                                   # noqa: BLE001
        pe_date = None
    res.pe_date = pe_date

    # 按每家公司真实披露日逐只对齐（PIT）：
    # 只用 as_of 之前已公开的数据，早披露的公司提前计入，晚披露的延后计入。
    as_of_date = as_of or date.today().isoformat()
    timelines = build_pit_timelines(fin, [s["code"] for s in stocks])
    used_periods: Dict[str, int] = {}

    rows: List[StockRow] = []
    for s in stocks:
        code = s["code"]
        q = quotes.get(code) or {}
        hit = pit_as_of(timelines.get(code), as_of_date)
        if hit:
            _, np_ttm, np_ttm_prev, g_ttm, base_p = hit
            fin_row = (fin.get(base_p) or {}).get(code) or {}
            g_latest = fin_row.get("np_yoy")
            fin_date = fin_row.get("notice_date")
            used_periods[base_p] = used_periods.get(base_p, 0) + 1
        else:
            np_ttm = np_ttm_prev = g_ttm = base_p = None
            g_latest = fin_date = None

        # PE = 实时总市值 ÷ 该股「最新已披露」的 TTM 归母净利。
        # 与增速共用同一期财报，保证同源同基准；实测与东财 PE_TTM 完全一致。
        # 港股的 mc 来自港股库的当日快照，其余逻辑与 A 股完全相同。
        mc = q.get("mktcap") or hk_mc.get(code)
        pe = (mc / np_ttm) if (mc and np_ttm and np_ttm > 0) else None
        if pe is None or pe <= 0:                 # 兜底：东财估值库 → 行情动态 PE
            pe = pe_src.get(code) or q.get("pe_ttm")

        row = StockRow(
            code=code,
            name=s.get("name") or q.get("name") or code,
            weight=s.get("weight"),
            price=q.get("price") or hk_close.get(code),
            pe_ttm=pe,
            mktcap=mc,
            np_ttm=np_ttm,
            np_ttm_prev=np_ttm_prev,
            g_ttm=g_ttm,
            g_latest=g_latest,
            fin_period=base_p,
            fin_label=report_label(base_p) if base_p else None,
            fin_date=fin_date,
            market=s.get("market"),
            g_series=(per_stock_series or {}).get(code),
        )

        g = row.g_ttm if growth_mode == "ttm" else row.g_latest
        if store.market_of(s.get("market"), code) is store.HK:
            # 港股能算：缺的只是数据（未初始化 / 该股没入库），不是"不能算"
            if row.pe_ttm is None or row.pe_ttm <= 0:
                row.status = "no_pe"
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

    if not valid:
        res.warn = "没有可用于计算 PEG 的有效成分股"
        res.stocks = [asdict(r) for r in rows]
        return res

    # ---- 整体法 PE：按持仓权重加权 ---------------------------------------
    #   PE = Σw / Σ(w / PE_i)   （w = 基金披露的占净值比例）
    # 不能改用「总市值」加权：A 股总市值含大量非流通国有股，
    # 会把银行等低 PE 大市值股权重放大，得到的其实是「全市场口径」PE，
    # 与 ETF 真正持有的那一篮子不是一回事（实测会低估 40% 以上）。
    num = den = 0.0
    for r in valid:
        w = r.weight
        if w and w > 0 and r.pe_ttm and r.pe_ttm > 0:
            num += w
            den += w / r.pe_ttm
    if den > 0:
        res.pe = num / den

    # ---- 整体法增速：Σ TTM 归母净利 同比 --------------------------------
    ttm_sum = sum(r.np_ttm for r in valid if r.np_ttm is not None)
    ttm_prev_sum = sum(r.np_ttm_prev for r in valid if r.np_ttm_prev is not None)
    lt_sum = 0.0
    lt_base = 0.0
    for r in valid:
        if r.np_ttm is not None and r.g_latest is not None and r.g_latest != -100:
            base = r.np_ttm / (1 + r.g_latest / 100.0)
            lt_sum += r.np_ttm
            lt_base += base
    res.np_ttm_sum = ttm_sum
    res.np_ttm_prev_sum = ttm_prev_sum
    if ttm_prev_sum and ttm_prev_sum != 0:
        res.g_ttm = (ttm_sum - ttm_prev_sum) / abs(ttm_prev_sum) * 100.0
    if lt_base > 0:
        res.g_latest = (lt_sum - lt_base) / lt_base * 100.0

    g_used = res.g_ttm if growth_mode == "ttm" else res.g_latest
    if res.pe and g_used and g_used > 0:
        res.peg = res.pe / g_used

    # ---- 全样本口径：含负增长成分股，用于衡量「只留正增长」的选择性偏差 ----
    full = [r for r in rows if r.np_ttm is not None and r.np_ttm_prev is not None]
    if full:
        s_now = sum(r.np_ttm for r in full)
        s_pre = sum(r.np_ttm_prev for r in full)
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

    # ---- 警示 ------------------------------------------------------------
    warns = []
    if not res.holdings_full:
        warns.append("⚠ 仅获取到季报前十大重仓（该 ETF 未披露半年报/年报全持仓），"
                     "样本不完整，PEG 仅供粗略参考")
    if res.coverage < 50:
        warns.append(f"有效样本权重覆盖率仅 {res.coverage:.0f}%，结果代表性不足")
    if g_used is not None and g_used > 100:
        warns.append("组合增速异常高（>100%），PEG 会被显著低估，请结合绝对增速判断")
    if g_used is not None and 0 < g_used < 5:
        warns.append("组合增速低于 5%，PEG 对增速极度敏感，参考价值有限")
    if res.g_all is not None and g_used is not None and (g_used - res.g_all) > 5:
        warns.append(f"已剔除亏损/负增长成分股，含它们的全样本增速为 {res.g_all:.1f}%，"
                     f"有效样本增速存在正向选择性偏差，PEG 可能偏乐观")
    if res.report_date:
        warns.append(f"持仓为 {res.report_date} 定期报告数据，非实时")

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

    rows.sort(key=lambda r: (-(r.weight or 0)))
    res.stocks = [asdict(r) for r in rows]

    if record and res.peg is not None:
        try:
            d = asdict(res)
            d.pop("stocks", None)
            store.record_daily([d])
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
                   "n_valid": r.get("n_valid"), "n_total": r.get("n_total"),
                   "report_date": r.get("report_date"), "days": 1,
                   "pe_lo": r.get("pe"), "pe_hi": r.get("pe"),
                   "peg_lo": r.get("peg"), "peg_hi": r.get("peg")}
        else:
            cur["date"] = r["date"]
            for f in ("pe", "g_ttm", "g_all", "peg", "coverage"):
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

    # 早于「最旧一期持仓披露截止日」的日期根本无持仓可用，永远算不出结果。
    # 不把起点推到那里的话，每次重跑都要白算几百天，
    # 而且 todo_dates 永远非空 —— 增量回填就退化成每次都全算（实测 8.6s 而非 0.2s）。
    if rep_dates:
        first_dl = min(ds.period_deadline(r) for r in rep_dates)
        if start < first_dl <= today:
            start = first_dl
            start_s = start.strftime("%Y-%m-%d")
            # 区间变短，需要的报告期也变少，重算一次
            n_quarters = max(6, int((today - start).days // 91) + 3)
            needed = financial_periods_for(start, depth=n_quarters) \
                or ds.ttm_required_periods()

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
    # 港股只有中报/年报，报告期集合与 A 股不同，两份都要覆盖到
    hk_periods = set(hk.hk_fin_periods(depth=max(6, n_quarters))) if hk_codes else set()
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
    for p in all_periods:
        if p not in fin:
            fin[p] = store.load_fin_subset(p, a_codes)
        if p in hk_periods:
            got = hk.fetch_hk_financials(p)
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
    have: set = set()
    if not force:
        have = {r["date"] for r in store.get_history(etf_code, 6000)}
    dates = store.pe_dates(start_s, today_s)
    todo_dates = dates
    if dates and not force:
        todo_dates = [d for d in dates if d not in have]
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
    if todo_hk:
        try:
            shares_now = {c["code"]: c.get("shares")
                          for c in hk.fetch_hk_list() if c.get("shares")}
        except Exception:                               # noqa: BLE001
            shares_now = {}
        n_hk = len(todo_hk)
        for i, c in enumerate(todo_hk, 1):
            if cancel and cancel():
                return 0
            tell(70 + 15.0 * (i - 1) / n_hk, f"拉取港股历史行情 {i}/{n_hk}…")
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
    todo_dates = dates if force else [d for d in dates if d not in have]
    if not todo_dates:
        return len(dates)

    tell(86, f"载入 {len(codes)} 只成分股的历史估值…")
    pe_map = store.load_stock_pe(a_codes, start_s, today_s)
    if hk_codes:
        pe_map.update(store.HK.load_stock_pe(hk_codes, start_s, today_s))

    # ---- 5. 构建每只股票的 TTM 时间线（按真实披露日生效） ------------------
    tell(88, "按各公司披露日构建 TTM 时间线…")
    timelines = build_pit_timelines(fin, codes)

    # ---- 6. 逐交易日计算 --------------------------------------------------
    etf_name = etf_code
    try:
        etf_name = next((e["name"] for e in ds.fetch_etf_list() if e["code"] == etf_code),
                        etf_code)
    except Exception:                                   # noqa: BLE001
        pass

    n_dates = len(todo_dates)
    out: List[dict] = []
    for _i, d in enumerate(todo_dates, 1):
        if cancel and cancel():
            break
        # 逐日上报太密（2109 次），每 25 天报一次足够撑起进度条
        if _i % 25 == 1 or _i == n_dates:
            tell(88 + 12.0 * _i / n_dates, f"计算每日 PEG {_i}/{n_dates}…")
        # 持仓：基金定期报告没有公开的披露日，仍按法定截止日（保守）。
        # 早于「最旧一期持仓的披露截止日」的日期无法无偏计算，直接跳过，
        # 否则会用未来才知道的持仓（前视偏差）。
        rep = _latest_known(rep_dates, d, ds.period_deadline)
        if not rep:
            continue
        wmap = weights_by_rep.get(rep)
        if not wmap:
            continue

        # 财报：逐只按真实披露日对齐（PIT），谁先披露谁先进组合
        # 样本 —— 只保留 PE>0 且增速>0 的成分股
        # PE   —— Σw / Σ(w / PE_i)，w 为基金披露的持仓权重
        w_sum = den = np_now = np_pre = 0.0
        n_valid = 0
        for c, w in wmap.items():
            if not w or w <= 0:
                continue
            hit = pit_as_of(timelines.get(c), d)
            if not hit:
                continue
            _, np_ttm, np_ttm_prev, g, _base = hit
            if g is None or g <= 0:
                continue
            # 与实时分析同一口径：当日总市值 ÷ 当日已披露的最新 TTM 归母净利
            cell = (pe_map.get(c) or {}).get(d)
            pe = None
            if cell and cell[1] and np_ttm and np_ttm > 0:
                pe = cell[1] / np_ttm            # cell = (pe_ttm, mktcap, close)
            if pe is None or pe <= 0:            # 兜底：东财当日 PE_TTM
                pe = cell[0] if cell else None
            if pe is None or pe <= 0:
                continue
            w_sum += w
            den += w / pe
            n_valid += 1
            np_now += np_ttm
            np_pre += np_ttm_prev

        if den <= 0 or not w_sum:
            continue
        pe_etf = w_sum / den
        g = ((np_now - np_pre) / abs(np_pre) * 100.0) if np_pre else None
        # 分母只算「本工具能估值」的持仓（A 股 + 港股）。
        # 把美股等根本拿不到数据的权重算进去，覆盖率会被系统性低估。
        w_avail = sum(w for c, w in wmap.items()
                      if w and w > 0 and store.is_valuable(market_by_code.get(c), c))
        out.append({
            "code": etf_code, "name": etf_name, "date": d,
            "pe": pe_etf, "g_ttm": g, "g_all": None,
            "peg": (pe_etf / g) if (g and g > 0) else None,
            "coverage": (w_sum / w_avail * 100.0) if w_avail else 0.0,
            "n_valid": n_valid, "n_total": len(wmap), "report_date": rep,
        })

    if out:
        store.record_daily(out)
    tell(100, f"完成，写入 {len(out)} 个交易日")
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
                 workers: int = 5) -> List[dict]:
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
                                                         "full": False})
        except Exception as exc:                       # noqa: BLE001
            r = ETFResult(code=etf["code"], name=etf.get("name", ""),
                          warn=f"计算失败：{exc}", report_date=None, fin_period=None)
        d = asdict(r)
        d.pop("stocks", None)
        d["market_cap"] = etf.get("market_cap")
        d["price"] = etf.get("price")
        d["pct"] = etf.get("pct")
        out.append(d)

    # 批量写入当日快照（逐只写会产生 N 次提交）
    try:
        store.record_daily([r for r in out if r.get("peg") is not None])
    except Exception:                                   # noqa: BLE001
        pass
    return out
