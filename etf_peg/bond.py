# -*- coding: utf-8 -*-
"""
无风险利率（中国 10 年期国债收益率）与股债性价比（ERP）。

数据：东财行情 secid=171.CN10Y（中国10年期国债）。
历史序列走 push2his 日 K——该域名 HTTPS 会被上游掐连接，须退回 HTTP
（与 hk.py 的 _kline_request 同一套路，直接复用其实现）。

ERP（股权风险溢价）= 盈利收益率 1/PE − 10Y 国债收益率
  · 分子是「持有这只 ETF 每天赚到的盈利回报」
  · 差值越高 = 股票相对债券越便宜；历史分位给出当前风险补偿的位置
序列不需要单独存：etf_daily 已有逐日 PE，与 bond_yield 逐日相减即得。

依赖：仅标准库。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import datasource as ds
from . import hk
from . import store

_SECID = "171.CN10Y"
_Y10_EARLIEST = "2018-01-02"       # 与 PE 历史对齐


def fetch_y10_realtime() -> Optional[Tuple[str, float]]:
    """
    实时 10Y 收益率（push2 stock/get，与日 K 域名不同，可用性更高）。
    返回 (今日日期, 收益率%)。push2his 被限流时的降级数据源：
    当天值先入库，历史序列等日 K 接口恢复后由增量拉取自动补齐。
    """
    from datetime import datetime
    url = (f"https://push2.eastmoney.com/api/qt/stock/get?secid={_SECID}"
           "&fields=f43,f57,f58,f60")
    try:
        d = json.loads(ds.http_get(url, timeout=15))
        data = d.get("data") or {}
        v = data.get("f43")
        if v is None:
            return None
        # f43 为放大 10000 倍的整数（16911 → 1.6911%）
        y10 = v / 10000.0 if v > 100 else float(v)
        return date.today().isoformat(), y10
    except Exception:                                   # noqa: BLE001
        return None


def fetch_y10_kline(start: str) -> List[dict]:
    """10Y 国债收益率日 K，返回 [{date, y10}]（单位 %，如 1.69）"""
    params = {
        "secid": _SECID, "klt": "101", "fqt": "0",
        "beg": start.replace("-", ""), "end": "20500101",
        "fields1": "f1,f2,f3",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
    }
    data = hk._kline_request(params).get("data") or {}
    out: List[dict] = []
    for line in data.get("klines") or []:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        d, close = parts[0], ds._num(parts[2])
        if d and close:
            out.append({"date": d, "y10": close})
    return out


def ensure_y10(max_lag_days: int = 2, force: bool = False) -> Optional[str]:
    """
    把 10Y 收益率历史增量拉到最新，返回最新日期。
    增量起点 = 库里最新日期的次日（已有数据只拉缺口），空库从 2018 拉全。
    """
    latest_d, _ = latest_y10()
    if latest_d and not force:
        try:
            if (date.today() - date(*map(int, latest_d.split("-")))).days <= max_lag_days:
                return latest_d
        except Exception:                               # noqa: BLE001
            pass
    start = _Y10_EARLIEST
    if latest_d:
        y, m, dd = (int(x) for x in latest_d.split("-"))
        start = (date(y, m, dd) + timedelta(days=1)).isoformat()
    try:
        rows = fetch_y10_kline(start)
        if rows:
            store.save_bond_yields(rows)
            return rows[-1]["date"]
    except Exception:                                   # noqa: BLE001
        pass                    # push2his 被限流时降级到实时接口，别让分析失败
    # 降级：日 K 不可用时用实时接口先落当天值（历史序列等接口恢复自动补齐）
    rt = fetch_y10_realtime()
    if rt:
        store.save_bond_yields([{"date": rt[0], "y10": rt[1]}])
        return rt[0]
    return latest_d


def latest_y10() -> Tuple[Optional[str], Optional[float]]:
    """库里最新的 (日期, 10Y 收益率%)"""
    return store.latest_bond_yield()


def load_y10_series(start: str, end: str) -> Dict[str, float]:
    """{date: y10%}，供 ERP 序列逐日对齐"""
    return store.load_bond_yields(start, end)
