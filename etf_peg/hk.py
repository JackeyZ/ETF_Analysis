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
from datetime import date, datetime
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
                break
            page += 1
    except Exception as exc:                            # noqa: BLE001
        err = exc                                       # 交由下面的兜底处理

    if out:
        out.sort(key=lambda x: -(x["market_cap"] or 0))
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
def hk_fin_periods(depth: int = 8, today: Optional[date] = None) -> List[str]:
    """
    港股需要的报告期（由新到旧），只含中报 06-30 与年报 12-31。

    东财对部分港股也生成了一季报/三季报，但港股法定只要求中报和年报，
    季报是东财推算或公司自愿披露，可靠性不一致 —— 只用两期，宁可更新慢半年。
    depth 是「基准期」个数，返回时已展开各自的 TTM 依赖（每期 5 个）。
    """
    today = today or date.today()
    cands: List[str] = []
    for y in range(today.year, today.year - 12, -1):
        for m, d in ((12, 31), (6, 30)):
            if date(y, m, d) > today:
                continue
            p = f"{y}-{m:02d}-{d:02d}"
            if today >= ds.period_deadline(p):
                cands.append(p)
    cands = cands[:depth]

    need: set = set()
    for p in cands:
        need.update(ds.ttm_required_periods(p))
    return sorted(need, reverse=True)


# --------------------------------------------------------------------------- #
#  3. 财报
# --------------------------------------------------------------------------- #
def fetch_hk_financials(period: str, force: bool = False,
                        on_progress=None) -> Dict[str, dict]:
    """
    批量取某一报告期全市场港股的主要财务指标，写入港股库。

    返回 {code: {name, parent_np, np_yoy, revenue, rev_yoy, eps, roe,
                 report_date, currency, shares}}
    parent_np 为「股东应占溢利」（= 归母净利润，累计值），已换算为港币口径。
    """
    if not force:
        hit = store.HK.load_fin(period, None if ds.is_settled(period) else ds.ACTIVE_FIN_TTL)
        if hit:
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
                # 港股接口不提供披露日，PIT 会退回法定截止日（中报 8/31、年报 4/30）
                "notice_date": None,
                "currency": it.get("CURRENCY"),
                "shares": ds._num(it.get("ISSUED_COMMON_SHARES")),
            }

    collect(first)
    total_pages = int(first.get("pages") or 1)
    for p in range(2, total_pages + 1):
        if on_progress:
            on_progress(p, total_pages)
        collect(one(p))

    if result:
        store.HK.save_fin(period, result, ds.is_settled(period))
    if skipped_currency:
        print(f"  [港股] {period} 跳过 {skipped_currency} 只非港币记账的公司")
    return result


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
def ensure_hk_market_snapshot(max_lag_days: int = 2, force: bool = False) -> Optional[str]:
    """
    把「最近交易日」全市场港股的收盘价与市值写入港股库，返回该日期。
    实时分析里港股 PE = 当日市值 ÷ 当日已披露的最新 TTM 净利，靠的就是这份数据。
    """
    d = store.HK.latest_market_pe_date()
    if d and not force:
        try:
            if (date.today() - date(*map(int, d.split("-")))).days <= max_lag_days:
                return d
        except Exception:                               # noqa: BLE001
            pass

    target = latest_trade_date()
    if not target:
        return None
    lst = fetch_hk_list(force=force)
    if not lst:
        return None

    rows = [{"date": target, "code": c["code"], "pe_ttm": None, "pe_lar": None,
             "mktcap": c.get("market_cap"), "close": c.get("price")}
            for c in lst if c.get("market_cap")]
    if rows:
        store.HK.save_stock_pe(rows)
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
    periods = hk_fin_periods(depth=8)
    for p in periods:
        if cancel and cancel():
            return {"total": len(lst), "todo": 0, "newly_done": 0, "failed": 0,
                    "cancelled": True, "coverage": store.HK.pe_coverage(start)}
        fetch_hk_financials(p)

    # 只保留财报里出现过的代码：清单里的权证、牛熊证根本没有财报，
    # 拉它们的 K 线纯属浪费（约可少拉一半）
    fin_codes = set(store.HK.load_fin_subset(periods[0], list(shares_now)).keys()) \
        if periods else set()
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
    prog(done0 + pulled, total, "完成")
    return {"total": total, "todo": len(todo),
            "newly_done": pulled, "failed": failed,
            "coverage": store.HK.pe_coverage(start)}
