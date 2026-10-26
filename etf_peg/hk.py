# -*- coding: utf-8 -*-
"""
港股数据源（东方财富），与 A 股 datasource.py 物理隔离。

为什么要单独一个文件：
  · 港股**没有现成的历史 PE**——东财估值库 RPT_VALUEANALYSIS_DET 完全不含港股，
    PE 只能自己算：当日总市值 ÷ 当日已披露的最新 TTM「股东应占溢利」
  · 报告期只有中报(06-30)与年报(12-31)，不是 A 股的四季
  · 市场标识(116)、secid 前缀、报表名、披露日规则全都不同

HTTP 基建（全局限流 / 节点轮换 / 磁盘缓存）直接复用 datasource，不重复实现。
接口细节与实测样本见 docs/hk_api.md。

依赖：仅标准库。
"""
from __future__ import annotations

import json
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import datasource as ds
from . import store

HK_MARKET = "116"
HK_EARLIEST = "2018-01-02"          # 与 A 股统一截断，避免两边区间对不齐

# 港股主要财务指标（宽表，一期一行）——见 docs/hk_api.md 2.4
_FIN_REPORT = "RPT_HKF10_FN_MAININDICATOR"
_DC = "https://datacenter.eastmoney.com/securities/api/data/v1/get?"
_CLIST = "https://push2.eastmoney.com/api/qt/clist/get?"
# push2his 的 HTTPS 会被上游直接掐连接（RemoteDisconnected），换 HTTP 就正常。
# 两个都试一遍，别让单个协议的问题拖垮整次初始化。
_KLINE_HOSTS = ("https://push2his.eastmoney.com", "http://push2his.eastmoney.com")
_FS_HK = "m:128+t:1,m:128+t:2,m:128+t:3,m:128+t:4"

# 只认港币记账：市值按港币算，净利必须同币种，否则 PE 会整体错位
_OK_CURRENCY = "HKD"

_HK_LIST_KEY = "hk_list"


def is_hk_code(code: Optional[str]) -> bool:
    """港股代码是 5 位数字（A 股 6 位，美股是字母）"""
    return bool(code) and str(code).isdigit() and len(str(code)) == 5


def secid(code: str) -> str:
    """东财 secid：港股 116.00700"""
    return f"{HK_MARKET}.{code}"


def _get_json(url: str, timeout: int = 30) -> dict:
    return json.loads(ds.http_get(url, timeout=timeout))


def _kline_request(params: dict, timeout: int = 40, retries: int = 2) -> dict:
    """
    日 K 请求：HTTPS 不通时退回 HTTP，仍不通就退避重试。

    push2his 是这次调研里最容易被限流的域名（比 datacenter 敏感得多），
    硬顶只会让封禁时间更长，所以这里宁可慢一点。
    """
    last = None
    for attempt in range(retries + 1):
        for host in _KLINE_HOSTS:
            try:
                return _get_json(host + "/api/qt/stock/kline/get?"
                                 + urllib.parse.urlencode(params), timeout=timeout)
            except Exception as exc:                    # noqa: BLE001
                last = exc
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"港股日 K 获取失败（{len(_KLINE_HOSTS)} 个协议 × "
                       f"{retries + 1} 次）：{type(last).__name__}: {last}")


# --------------------------------------------------------------------------- #
#  1. 全市场港股清单
# --------------------------------------------------------------------------- #
def fetch_hk_list(force: bool = False) -> List[dict]:
    """
    返回 [{code, name, price, market_cap, shares}]，约 4685 条（含权证等非股票，
    真正有财报的约 2200~2600 只，调用方按需过滤）。

    shares 由 市值/现价 反推，省掉逐只再查一次股本。
    """
    if not force:
        hit = ds.cache_get(_HK_LIST_KEY, 6 * 3600)
        if hit:
            return hit

    # 单页上限就是 100：传 pz=500 它也只回 100 条，却仍按 500 计算偏移量，
    # 结果每页之间会漏掉 400 只。必须按 100 翻页，并以「实际返回不足一页」收尾。
    out: List[dict] = []
    page, size = 1, 100
    err = None
    complete = False                # 是否翻完全部分页（残缺清单不能当全市场用）
    try:
        while page <= 200:
            params = {
                "pn": str(page), "pz": str(size), "po": "0", "np": "1",
                "fltt": "2", "invt": "2", "fid": "f12", "fs": _FS_HK,
                "fields": "f12,f14,f2,f3,f20,f21",
            }
            data = _get_json(_CLIST + urllib.parse.urlencode(params)).get("data") or {}
            diff = data.get("diff") or []
            if not diff:
                complete = True                  # 正常翻到空页
                break
            for it in diff:
                code = it.get("f12")
                if not is_hk_code(code):
                    continue
                price = ds._num(it.get("f2"))
                mc = ds._num(it.get("f20")) or ds._num(it.get("f21"))
                shares = (mc / price) if (mc and price) else None
                out.append({"code": code, "name": it.get("f14"), "price": price,
                            "market_cap": mc, "shares": shares})
            if len(diff) < size:
                complete = True                  # 最后一页
                break
            page += 1
        else:
            complete = True                      # 到达翻页上限，视为完整
    except Exception as exc:                            # noqa: BLE001
        err = exc                                       # 交由下面的兜底处理

    if out:
        out.sort(key=lambda x: -(x["market_cap"] or 0))
        # 只有完整清单才进缓存：上游限流时常常第一页成功、第二页被掐，
        # 这份 100 只的残缺清单一旦写进 6 小时缓存，全市场快照就只剩 100 只，
        # 其余港股全部变成「无行情」——比多花几次翻页请求糟得多
        if complete:
            ds.cache_put(_HK_LIST_KEY, out)
        return out

    # 上游不可用时退回旧缓存（stale-while-error）：清单短时间内不会变，
    # 用它继续初始化远比整个任务失败强。
    fallback = ds.cache_get(_HK_LIST_KEY, 7 * 86400, allow_stale=True)
    if fallback:
        return fallback
    raise RuntimeError(f"港股清单获取失败且无可用缓存：{type(err).__name__}: {err}")


# --------------------------------------------------------------------------- #
#  2. 报告期
# --------------------------------------------------------------------------- #
def hk_fin_periods(depth: int = 8, today: Optional[date] = None,
                   min_deadline: Optional[date] = None) -> List[str]:
    """
    港股需要的报告期（由新到旧），覆盖全部四个季度末。

    港股公司财年截止日不统一（12 月、3 月、6 月、9 月年结都有）：
    12 月年结公司的中报/年报落在 06-30 / 12-31，而 3 月年结公司的
    年报/中报落在 03-31 / 09-30——只拉两期会把非 12 月年结的公司
    （实测约 625 只）的财报整个漏掉。四个季度末都拉才能全覆盖：
    每个 REPORT_DATE 里混着不同财年公司的报告（如 03-31 期 =
    12 月年结公司的自愿一季报 + 3 月年结公司的法定年报），由
    build_hk_ttm_timelines 按各家公司的 REPORT_TYPE 自行甄别。
    depth 是「基准期」个数，返回时已展开各自的 TTM 依赖（每期 5 个）。

    min_deadline：基准期披露截止日下限，与 A 股 financial_periods_for 的
    「deadline >= start - 110 天」同一裁剪逻辑。比它早的基准期构造出的 TTM
    生效日必然早于回填区间起点，逐日循环根本用不到——不过滤的话（005889
    实测）会去逐期拉 2012~2018 年的全市场港股财报，每期 1~2 分钟且无进度
    反馈，进度条在 15% 停半小时起步，看起来像卡死。
    """
    today = today or date.today()
    cands: List[str] = []
    for y in range(today.year, today.year - 12, -1):
        for m, d in ((12, 31), (9, 30), (6, 30), (3, 31)):
            if date(y, m, d) > today:
                continue
            p = f"{y}-{m:02d}-{d:02d}"
            # 候选门槛用「期终 + 3 个月」：覆盖各财年在该期的主要法定披露
            # （如 06-30 期的 12 月年结中报 8 月末、03-31 期的 3 月年结年报
            # 7 月末）。不能用 hk_is_settled 的保守混财年期限做候选——那会把
            # 最近两个季度整个排除，损失时效；未定稿的期由 fetch 层的
            # ACTIVE_FIN_TTL 每 6 小时自动刷新兜底。
            if today >= _shift_end(date(y, m, d), 3):
                if min_deadline is None or hk_period_deadline(p) >= min_deadline:
                    cands.append(p)
    cands = cands[:depth]

    need: set = set()
    for p in cands:
        need.update(ds.ttm_required_periods(p))
    return sorted(need, reverse=True)


def hk_period_deadline(period: str) -> date:
    """
    港股某个报告期的「最晚法定披露日」（保守口径）。

    一个 REPORT_DATE 混着多家公司不同财年的报告，按其中最晚的披露期限取：
      12-31 期：12 月年结年报（次年 4/30）+ 6 月年结中报（约 2/28）→ 次年 5/7
      03-31 期：3 月年结年报（当年 7/31）+ 12 月年结一季报（约 5 月）  → 当年 8/31
      06-30 期：6 月年结年报（当年 10/31）+ 12 月年结中报（8/31）      → 当年 11/7
      09-30 期：9 月年结年报（次年 1/31）+ 3 月年结中报（11/30）       → 次年 2/7
    用于：报告期候选过滤（hk_fin_periods）与财报定稿判定（hk_is_settled）。
    """
    y, m, _ = period.split("-")
    y = int(y)
    if m == "12":
        return date(y + 1, 5, 7)
    if m == "03":
        return date(y, 8, 31)
    if m == "06":
        return date(y, 11, 7)
    return date(y + 1, 2, 7)                    # 09-30


def hk_is_settled(period: str, grace_days: int = 7) -> bool:
    """过了最晚披露期限 + 宽限期即认为该期港股财报已定稿（不再回源刷新）"""
    return date.today() > hk_period_deadline(period) + timedelta(days=grace_days)


# --------------------------------------------------------------------------- #
#  3. 财报
# --------------------------------------------------------------------------- #
def fetch_hk_financials(period: str, force: bool = False,
                        on_progress=None) -> Dict[str, dict]:
    """
    批量取某一报告期全市场港股的主要财务指标，写入港股库。

    返回 {code: {name, parent_np, np_yoy, revenue, rev_yoy, eps, roe,
                 report_date, currency, shares, report_type, fiscal_year,
                 bps, equity, dividend_rate, dps, total_liab, end_cash, op_profit}}
    parent_np 为「股东应占溢利」（= 归母净利润，累计值），已换算为港币口径。
    report_type（年报/中报/一季报/三季报）与 fiscal_year（如 12-31/03-31）
    是按公司财年构造 TTM 的依据——同一个 REPORT_DATE 里混着不同财年公司
    的报告，靠这两个字段甄别。
    """
    if not force:
        hit = store.HK.load_fin(period, None if hk_is_settled(period) else ds.ACTIVE_FIN_TTL)
        # 旧数据没有扩展列（v2=0）或没有财报类型/财年（v3 前的老数据），
        # 需要重拉一次升级；重拉成功后不再重复
        if hit and store.HK.fin_v2(period) and store.HK.fin_has_report_type(period):
            return hit

    result: Dict[str, dict] = {}
    skipped_currency = 0
    size = 500

    def one(p: int) -> Optional[dict]:
        params = {
            "reportName": _FIN_REPORT, "columns": "ALL",
            "pageNumber": str(p), "pageSize": str(size),
            "filter": f"(REPORT_DATE='{period}')",
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "source": "WEB", "client": "PC",
        }
        return (_get_json(_DC + urllib.parse.urlencode(params), timeout=40)
                .get("result") or {})

    first = one(1)
    if not first or not first.get("data"):
        return store.HK.load_fin(period, None) or {}

    def collect(res: Optional[dict]) -> None:
        nonlocal skipped_currency
        for it in (res or {}).get("data") or []:
            code = it.get("SECURITY_CODE")
            if not is_hk_code(code):
                continue
            if (it.get("CURRENCY") or _OK_CURRENCY) != _OK_CURRENCY:
                skipped_currency += 1          # 非港币记账：市值(HKD)÷净利(外币)会错
                continue
            result[code] = {
                "name": it.get("SECURITY_NAME_ABBR"),
                "parent_np": ds._num(it.get("HOLDER_PROFIT")),
                "np_yoy": ds._num(it.get("HOLDER_PROFIT_YOY")),
                "revenue": ds._num(it.get("OPERATE_INCOME")),
                "rev_yoy": ds._num(it.get("OPERATE_INCOME_YOY")),
                "eps": ds._num(it.get("BASIC_EPS")),
                "roe": ds._num(it.get("ROE_AVG")),
                "report_date": (it.get("REPORT_DATE") or "")[:10] or period,
                # 港股接口不提供披露日，PIT 由 build_hk_ttm_timelines 按
                # 财报类型定（年报 +4 个月、中报 +3 个月、季报 +2 个月）
                "notice_date": None,
                "currency": it.get("CURRENCY"),
                "shares": ds._num(it.get("ISSUED_COMMON_SHARES")),
                # 财报类型与财年截止：按公司财年构造 TTM 的依据（v3 起）
                "report_type": it.get("REPORT_TYPE"),
                "fiscal_year": it.get("FISCAL_YEAR"),
                # 估值扩展列（PB / 股息率 / EV 材料）
                "bps": ds._num(it.get("BPS")),
                "equity": ds._num(it.get("TOTAL_PARENT_EQUITY")),
                "dividend_rate": ds._num(it.get("DIVIDEND_RATE")),
                "dps": ds._num(it.get("DPS_HKD")),
                "total_liab": ds._num(it.get("TOTAL_LIABILITIES")),
                "end_cash": ds._num(it.get("END_CASH")),
                "op_profit": ds._num(it.get("OPERATE_PROFIT")),
            }

    collect(first)
    total_pages = int(first.get("pages") or 1)
    for p in range(2, total_pages + 1):
        if on_progress:
            on_progress(p, total_pages)
        collect(one(p))

    if result:
        store.HK.save_fin(period, result, hk_is_settled(period))
    if skipped_currency:
        print(f"  [港股] {period} 跳过 {skipped_currency} 只非港币记账的公司")
    return result


# --------------------------------------------------------------------------- #
#  3b. 按公司财年构造 TTM 时间线（港股专用）
# --------------------------------------------------------------------------- #
# 港股公司财年截止日不统一（12 月、3 月、6 月、9 月年结都有），不能用 A 股
# 那套「日历季度」公式。这里用统一公式，对任意财年、任意报告频率都成立：
#
#     TTM(期终 e, 跨度 s) = cum(e) + cum(年报@e−s) − cum(同跨度@e−12m)
#
# 其中跨度 s 由财报类型决定（年报 12 / 中报 6 / 一季报 3 / 三季报 9），
# 「年报@e−s」指期终恰为 e−s 那个月月末的年报，「同跨度@e−12m」指期终
# 恰为 12 个月前、跨度相同的报告。A 股日历公式是该公式在 12 月年结下的特例。
# 例（3 月年结公司）：中报 2025-09-30 的 TTM = cum(2025-09-30)
#   + cum(年报 2025-03-31) − cum(中报 2024-09-30)。

# 财报类型 → 报告跨度（月）；「年报」判断放最后，避免与"季报"等前缀混淆
_SPAN_OF_TYPE = (("一季报", 3), ("三季报", 9), ("中报", 6), ("半年报", 6),
                 ("中期报告", 6), ("年报", 12), ("年度报告", 12), ("全年业绩", 12))
# 跨度 → 法定披露期限（月，保守口径）：年报 4（HKEX）/ 中报 3 / 季报 2
_DISCLOSE_MONTHS = {12: 4, 6: 3, 3: 2, 9: 2}


def _span_of(report_type: Optional[str]) -> Optional[int]:
    """财报类型（如「2025年年报」）→ 报告跨度（月）；无法识别返回 None"""
    if not report_type:
        return None
    for suffix, span in _SPAN_OF_TYPE:
        if report_type.endswith(suffix):
            return span
    return None


def _month_end(y: int, m: int) -> date:
    """该月最后一天（y/m 可为任意整数，会正规化）"""
    if m == 12:
        return date(y + 1, 1, 1) - timedelta(days=1)
    return date(y, m + 1, 1) - timedelta(days=1)


def _shift_end(d: date, months: int) -> date:
    """期终日期平移 months 个月后所在月的月末（期终都是月末，平移后仍是月末）"""
    y, m = d.year, d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return _month_end(y, m)


def _ttm_at(reports_by_date: Dict[str, dict], e: date, span: int,
            field: str) -> Optional[float]:
    """
    统一 TTM 公式在字段 field（parent_np / revenue / op_profit）上的取值。

    e/span：目标报告的期终与跨度。依赖任一缺失（历史不够 / 该期没披露）
    返回 None——与 A 股 build_pit_timelines 的跳过行为一致。
    """
    r = reports_by_date.get(e.isoformat())
    if r is None or _span_of(r.get("report_type")) != span:
        return None                    # 目标报告本身的跨度必须与待构造的一致
    cur = r.get(field)
    if cur is None:
        return None
    fy = reports_by_date.get(_shift_end(e, -span).isoformat())     # 年报@e−s
    if fy is None or _span_of(fy.get("report_type")) != 12:
        return None
    prev = reports_by_date.get(_shift_end(e, -12).isoformat())     # 同跨度@e−12m
    if prev is None or _span_of(prev.get("report_type")) != span:
        return None                    # 公司报告频率变化时不误配（如去年没发季报）
    a, b = fy.get(field), prev.get(field)
    if a is None or b is None:
        return None
    return cur + a - b


def detect_kline_gaps(codes: List[str], start: str, end: str,
                      min_holes: int = 3) -> List[str]:
    """
    找出「区间内部有日历空洞」的港股——需要增量补拉 K 线的股票。

    背景：每日全市场快照（ensure_hk_market_snapshot）只写「当天」一行，
    而逐只 K 线历史止于各自的拉取时刻。两者之间的日子（工具没打开 +
    K 线没续拉）就是空洞：日历表里有这些日期（别的公司有），该公司缺行，
    回填时这些天该股被静默跳过（实测 513180：9-22~10-07 覆盖率掉到 2%）。

    检测口径：只看该公司**首行与末行之间**的内部空洞（IPO 之前的缺失
    是正常的，不能触发重拉），且至少缺 min_holes 个交易日才算。
    """
    from bisect import bisect_left, bisect_right
    cal = store.HK.pe_dates(start, end)
    if not cal:
        return []
    rows = store.HK.load_stock_pe(codes, start, end)
    out: List[str] = []
    for c in codes:
        days = rows.get(c) or {}
        if len(days) < 2:
            continue                       # 无行/单行谈不上内部空洞
        ks = sorted(days)
        lo, hi = ks[0], ks[-1]
        i, j = bisect_left(cal, lo), bisect_right(cal, hi)
        internal = j - i                   # 首末行之间应有的交易日数
        if internal - len(ks) >= min_holes:
            out.append(c)
    return out


def build_hk_ttm_timelines(hk_codes: List[str]) -> Dict[str, List[tuple]]:
    """
    为每只港股构建按其自身财年周期的 TTM 时间线。

    返回 {code: [(生效日, np_ttm, np_ttm_prev, g, 基准期, rev_ttm, op_ttm,
                  deduct_ttm, deduct_ttm_prev), …]}（按生效日升序）——
    元组结构与 A 股 build_pit_timelines 完全一致，pit_as_of 与下游计算
    无需任何改动。港股没有「扣非」概念（非经常性损益不单独披露），
    末两位固定为 None。

    生效日 = 报告期终 + 法定披露期限（年报 +4 个月、中报 +3 个月、
    季报 +2 个月，保守口径）。营收 / 营业利润的 TTM 用同一组报告、同一
    公式构造，保证 PE/PS/EV·EBIT 同源同基准。依赖不足的报告跳过
    （单只失败不影响其他）。
    """
    out: Dict[str, List[tuple]] = {}
    if not hk_codes:
        return out
    rows_by_code = store.HK.load_fin_rows(hk_codes)
    for code, rows in rows_by_code.items():
        # 按期终索引该公司的全部报告；期终即 period（REPORT_DATE）
        by_date: Dict[str, dict] = {}
        for r in rows:
            if r.get("parent_np") is not None and _span_of(r.get("report_type")):
                by_date[r["period"]] = r
        if not by_date:
            continue
        tl: List[tuple] = []
        for r in rows:
            span = _span_of(r.get("report_type"))
            if span is None or r.get("parent_np") is None:
                continue                     # 老数据无财报类型（v3 重拉前），跳过
            e = date(*map(int, r["period"].split("-")))
            ttm = _ttm_at(by_date, e, span, "parent_np")
            ttm_prev = _ttm_at(by_date, _shift_end(e, -12), span, "parent_np")
            if ttm is None or ttm_prev is None:
                continue
            g = (ttm - ttm_prev) / abs(ttm_prev) * 100.0 if ttm_prev else None
            rev_ttm = _ttm_at(by_date, e, span, "revenue")
            op_ttm = _ttm_at(by_date, e, span, "op_profit")
            eff = _shift_end(e, _DISCLOSE_MONTHS[span])            # 法定披露日
            tl.append((eff.isoformat(), ttm, ttm_prev, g,
                       r["period"], rev_ttm, op_ttm, None, None))
        if tl:
            tl.sort(key=lambda x: x[0])
            out[code] = tl
    return out


# --------------------------------------------------------------------------- #
#  4. 日 K 与市值
# --------------------------------------------------------------------------- #
def fetch_hk_kline(code: str, start_date: str = HK_EARLIEST) -> List[dict]:
    """
    单只港股日线，返回 [{date, close}]（不复权）。
    市值不在这里算——需要结合按报告期变化的股本，见 attach_mktcap。
    """
    params = {
        "secid": secid(code), "klt": "101", "fqt": "0",
        "beg": start_date.replace("-", ""), "end": "20500101",
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    data = _kline_request(params).get("data") or {}
    out: List[dict] = []
    for line in data.get("klines") or []:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        d, close = parts[0], ds._num(parts[2])
        if d and close:
            out.append({"date": d, "close": close})
    return out


def latest_trade_date(probe_code: str = "00700") -> Optional[str]:
    """
    港股最近一个交易日。港股没有「估值库最新交易日」可探测，
    用一只流动性最好的票的日 K 最后一根代替（1 次请求）。
    """
    params = {
        "secid": secid(probe_code), "klt": "101", "fqt": "0",
        "beg": (date.today().replace(day=1)).strftime("%Y%m%d"), "end": "20500101",
        "fields1": "f1", "fields2": "f51",
    }
    try:
        data = _kline_request(params, timeout=30).get("data") or {}
        ks = data.get("klines") or []
        return ks[-1].split(",")[0] if ks else None
    except Exception:                                   # noqa: BLE001
        return None


def _shares_at(tl: List[Tuple[str, float]], d: str, default: Optional[float]) -> Optional[float]:
    """股本阶梯：取不晚于 d 的最新一期；都没有就用当前股本兜底"""
    cur = default
    for rep, sh in tl:
        if rep <= d:
            cur = sh
        else:
            break
    return cur


def attach_mktcap(code: str, closes: List[dict],
                   default_shares: Optional[float]) -> List[dict]:
    """把收盘价 × 股本 变成 stock_pe 行（pe_ttm 留空，计算时由 mktcap ÷ TTM 净利得出）"""
    tl = store.HK.fin_shares(code)
    rows = []
    for r in closes:
        sh = _shares_at(tl, r["date"], default_shares)
        rows.append({"date": r["date"], "code": code, "pe_ttm": None, "pe_lar": None,
                     "mktcap": (r["close"] * sh) if sh else None,
                     "close": r["close"]})
    return rows


# --------------------------------------------------------------------------- #
#  5. 当日全市场快照（实时分析用）
# --------------------------------------------------------------------------- #
# 快照拉取失败后的冷却：上游掐连接时 newest 节点要重试 4 次 × 超时，
# 不冷却的话每次分析都要白白卡上一分钟
_SNAP_FAIL_AT: Dict[str, float] = {}
_SNAP_FAIL_TTL = 900


def ensure_hk_market_snapshot(max_lag_days: int = 2, force: bool = False) -> Optional[str]:
    """
    把「最近交易日」全市场港股的收盘价与市值写入港股库，返回该日期。
    实时分析里港股 PE = 当日市值 ÷ 当日已披露的最新 TTM 净利，靠的就是这份数据。

    拉不到新快照时**退回库里已有的旧快照**，而不是返回 None：
    港股行情挂了就把现价和 PE 全变成「—」，等于整块港股估值消失，
    用几天前的收盘价算出来的 PE 只是略有滞后，仍然可用。
    """
    d = store.HK.latest_market_pe_date()
    if d and not force:
        try:
            if (date.today() - date(*map(int, d.split("-")))).days <= max_lag_days:
                return d
        except Exception:                               # noqa: BLE001
            pass

    last = _SNAP_FAIL_AT.get("snap")
    if last and not force and time.time() - last < _SNAP_FAIL_TTL:
        return d                                        # 刚失败过，直接用旧快照

    target = latest_trade_date()
    if not target:
        _SNAP_FAIL_AT["snap"] = time.time()
        return d                                        # 探测不到交易日 → 退回旧快照
    lst = fetch_hk_list(force=force)
    # 清单残缺（上游翻页被掐）时宁可不写：只覆盖 100 只的「全市场快照」
    # 会让其余港股统统变成无行情，比用几天前的完整快照更糟
    if not lst or len(lst) < 1000:
        _SNAP_FAIL_AT["snap"] = time.time()
        return d

    rows = [{"date": target, "code": c["code"], "pe_ttm": None, "pe_lar": None,
             "mktcap": c.get("market_cap"), "close": c.get("price")}
            for c in lst if c.get("market_cap")]
    if rows:
        store.HK.save_stock_pe(rows)
        _SNAP_FAIL_AT.pop("snap", None)
    return target


# --------------------------------------------------------------------------- #
#  6. 初始化（一次性拉全历史）
# --------------------------------------------------------------------------- #
def init_all_hk(start_date: Optional[str] = None,
                on_progress=None, cancel=None) -> dict:
    """
    把全市场港股的每日收盘价/市值 + 各期财报拉进港股库。

    做完之后含港股的基金才能真正把港股算进 PEG。
    支持断点续传：已拉过的（日期覆盖或水位）自动跳过，中断后重跑继续。
    on_progress(done, total, label)
    """
    start = start_date or HK_EARLIEST
    prog = on_progress or (lambda *a: None)

    prog(0, 0, "取港股全市场清单…")
    lst = fetch_hk_list()
    if not lst:
        raise RuntimeError("取不到港股清单，请检查网络后重试")
    shares_now = {c["code"]: c.get("shares") for c in lst}

    prog(0, 0, "载入港股财报…")
    # depth=46（约 11.5 年 × 4 个季度基准期）：K 线上游最早 2018-01-02，
    # 而该日期上构造 TTM（含 prev）最深要追溯到 2015-09 的报告；
    # 四季期口径保证 3/6/9/12 月年结公司的财报全部覆盖。
    # 老库已入库的期（v2=1 但无财报类型）会触发 v3 重拉升级，一次性。
    periods = hk_fin_periods(depth=46)
    for p in periods:
        if cancel and cancel():
            return {"total": len(lst), "todo": 0, "newly_done": 0, "failed": 0,
                    "cancelled": True, "coverage": store.HK.pe_coverage(start)}
        fetch_hk_financials(p)

    # 只保留财报里出现过的代码：清单里的权证、牛熊证根本没有财报，
    # 拉它们的 K 线纯属浪费（约可少拉一半）。
    # 必须用「fin 表所有期并集」而非最新一期：港股财年截止日不统一，
    # 3 月/9 月年结的公司财报落在 03-31/09-30 期，用最新半年报期过滤
    # 会漏掉它们（实测漏 625 只，K 线历史没拉）。
    fin_codes = store.HK.fin_codes()
    codes = [c["code"] for c in lst if c["code"] in fin_codes] or [c["code"] for c in lst]

    todo = store.HK.pe_pending(codes, start)
    total, done0 = len(codes), len(codes) - len(todo)
    failed = 0
    pulled = 0
    for i, c in enumerate(todo, 1):
        if cancel and cancel():
            break
        prog(done0 + i - 1, total, f"拉取 {c} 的历史行情（{done0 + i}/{total}）")
        try:
            closes = fetch_hk_kline(c, start)
            if closes:
                store.HK.save_stock_pe(attach_mktcap(c, closes, shares_now.get(c)))
            store.HK.set_pe_fetch_log(c, start)
            pulled += 1                                 # 只数真正拉到的，取消时不能报全量
        except Exception:                               # noqa: BLE001
            failed += 1                                 # 单只失败不影响整体，重跑会补

    # 内部日历空洞补拉：K 线拉取时刻与每日快照之间的缺口（如工具几天没开，
    # 那几天的 K 线就缺）。逐只重拉全历史（约 3 页/只）覆盖空洞；
    # 补完后不再有空洞，下次初始化自动跳过。
    try:
        gaps = detect_kline_gaps(codes, start, date.today().isoformat())
    except Exception:                                   # noqa: BLE001
        gaps = []
    for i, c in enumerate(gaps, 1):
        if cancel and cancel():
            break
        prog(done0 + pulled + i - 1, total, f"补拉空洞 {c} 的行情（{i}/{len(gaps)}）")
        try:
            closes = fetch_hk_kline(c, start)
            if closes:
                store.HK.save_stock_pe(attach_mktcap(c, closes, shares_now.get(c)))
        except Exception:                               # noqa: BLE001
            pass                                        # 下次初始化会再试
    prog(total, total, "完成")
    return {"total": total, "todo": len(todo),
            "newly_done": pulled, "failed": failed, "gap_filled": len(gaps),
            "coverage": store.HK.pe_coverage(start)}
