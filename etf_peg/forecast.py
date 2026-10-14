# -*- coding: utf-8 -*-
"""
机构一致预期（盈利预测）——前瞻 PE / 前瞻 PEG 的数据源。

数据来自东方财富 F10 盈利预测接口（免费，逐只请求）：
    https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519

关键字段（实测）：
    yctj_chart: [{YEAR: 2026, YEAR_MARK: 'E', EPS, PE,
                  PARENT_NETPROFIT, PARENT_NETPROFIT_RATIO, ...}, ...]
      —— 各年度的「近六月机构平均」一致预期净利润（E=预测，A=实际）
    jgyc[0].RATING_ORG_NUM: 出具预测的机构数

边界：
  · 仅 A 股有此数据（港股三种代码格式实测均返回空），前瞻指标只覆盖 A 股成分；
  · 逐只请求、限流下约 9 req/s：首次分析一只 ETF 约多花 成分股数/9 秒，
    结果入 SQLite（TTL 3 天，预测随研报滚动更新），之后零网络；
  · 进程内 memo 防止批量扫描时重复打库。

依赖：仅标准库。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date
from typing import Dict, List, Optional

from . import datasource as ds
from . import store

_FORECAST_TTL = 3 * 86400          # 一致预期 3 天过期（研报在滚动更新）
_MEMO: Dict[str, float] = {}       # code -> 上次确认时间（命中缓存/失败都记，防打爆）
_MEMO_LOCK = threading.Lock()
_MEMO_RETRY = 600                  # 失败/无数据的代码，10 分钟内不再试

_URL = ("https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax"
        "?code={sec}")
_HEADERS = {"Referer": "https://emweb.securities.eastmoney.com/pc_hsf10/pages/index.html"}


def _sec(code: str) -> Optional[str]:
    """6 位 A 股代码 → SH/SZ 前缀；北交所（4/8 开头）等无预测数据，返回 None"""
    if code.startswith("6"):
        return "SH" + code
    if code[:1] in ("0", "3"):
        return "SZ" + code
    return None


def fetch_forecast(code: str, force: bool = False) -> Optional[dict]:
    """
    单只 A 股的一致预期。
    返回 {years: {2026: {mark,np,np_ratio,eps,pe}, ...}, n_org} 或 None。
    """
    now = time.time()
    with _MEMO_LOCK:
        last = _MEMO.get(code, 0.0)
    if not force and now - last < _MEMO_RETRY:
        return store.load_forecast(code, _FORECAST_TTL)
    with _MEMO_LOCK:
        _MEMO[code] = now

    if not force:
        hit = store.load_forecast(code, _FORECAST_TTL)
        if hit:
            return hit

    sec = _sec(code)
    if not sec:
        return None
    try:
        body = ds.http_get(_URL.format(sec=sec), _HEADERS, timeout=20)
        d = json.loads(body)
        chart = d.get("yctj_chart") or []
        jgyc = d.get("jgyc") or []
        n_org = (jgyc[0] or {}).get("RATING_ORG_NUM") if jgyc else None
        years: Dict[int, dict] = {}
        for it in chart:
            try:
                y = int(it.get("YEAR"))
            except (TypeError, ValueError):
                continue
            years[y] = {
                "mark": it.get("YEAR_MARK"),
                "np": ds._num(it.get("PARENT_NETPROFIT")),
                "np_ratio": ds._num(it.get("PARENT_NETPROFIT_RATIO")),
                "eps": ds._num(it.get("EPS")),
                "pe": ds._num(it.get("PE")),
            }
        if years:
            store.save_forecast(code, years, n_org)
            return {"years": years, "n_org": n_org}
        return None
    except Exception:                                   # noqa: BLE001
        return None


def forward_valuation(rows: List, a_weight_total: Optional[float] = None,
                      all_weight: Optional[float] = None) -> Optional[dict]:
    """
    组合前瞻估值（整体法，口径与 PE 一致：Σw / Σ(w / 前瞻PE_i)）。

    rows: 持仓里的 A 股 StockRow（需 weight / mktcap）。
    基准年 Y1 = 当前年份（2026 年 9 月时即 2026E，预测已充分铺开）；
    CAGR 用 Y1 与 Y1+2（2028E）两年复合，只统计两年预测都有的样本。

    返回 {pe_f, peg_f, cagr, cons_weight, a_weight, n_used, y1, y3} 或 None。
    """
    if not rows:
        return None
    y1 = date.today().year
    y3 = y1 + 2

    # 先筛出有市值、有权重、能参与前瞻计算的 A 股
    targets = [r for r in rows
               if r.weight and r.weight > 0 and r.mktcap and r.mktcap > 0]
    if not targets:
        return None

    # 并发抓取一致预期：emweb 单次响应约 2~4 秒，串行时成分股多的基金
    # 首次分析会线性拖慢到几分钟（001579 96 只成分股串行实测 ≈ 4 分钟）。
    # 与行情/财报的并发抓取保持一致；fetch_forecast 内部自带 SQLite 缓存
    # 与失败降级（返回 None），并发不会放大失败影响。
    import concurrent.futures as cf
    forecasts: Dict[str, Optional[dict]] = {}
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(fetch_forecast, r.code): r.code for r in targets}
        for fu in cf.as_completed(futs):
            code = futs[fu]
            try:
                forecasts[code] = fu.result()
            except Exception:                           # noqa: BLE001
                forecasts[code] = None

    num = den = 0.0
    np_y1 = np_y3 = 0.0
    used_w = 0.0
    n_used = 0
    for r in targets:
        w = r.weight
        f = forecasts.get(r.code)
        if not f:
            continue
        np_e = (f["years"].get(y1) or {}).get("np")
        if not np_e or np_e <= 0:
            continue
        pe_f = r.mktcap / np_e
        num += w
        den += w / pe_f
        used_w += w
        n_used += 1
        np_l = (f["years"].get(y3) or {}).get("np")
        if np_l and np_l > 0:
            np_y1 += np_e
            np_y3 += np_l

    if den <= 0 or used_w <= 0:
        return None
    pe_f = num / den
    cagr = (((np_y3 / np_y1) ** 0.5 - 1.0) * 100.0
            if np_y1 > 0 and np_y3 > 0 else None)
    a_w = a_weight_total if a_weight_total is not None else used_w
    return {
        "pe_f": pe_f,
        "peg_f": (pe_f / cagr) if (pe_f and cagr and cagr > 0) else None,
        "cagr": cagr,
        # 有一致预期的 A 股权重占「全部持仓权重」的比例：
        # 半导体 ETF 应接近 100%；含大量港股/美股的基金会明显偏低，需警示
        "cons_weight": (used_w / all_weight * 100.0) if all_weight else None,
        "a_weight": a_w,
        "n_used": n_used,
        "y1": y1, "y3": y3,
    }
