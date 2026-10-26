# -*- coding: utf-8 -*-
"""
券商研报的盈利预测（历史全量）—— 「分析师对这只股票怎么看、怎么改」的数据底座。

数据来自东方财富研报中心（免费，可批量）：
    https://reportapi.eastmoney.com/report/list?qType=0&beginTime=...&endTime=...
每一条是一篇研报，带机构、分析师、评级、目标价，以及当年/次年/后年三个
年度的 EPS 与 PE 预测。实测全市场 2015 年起约 14.8 万篇。

为什么不用 F10 的「一致预期」：
    F10 只给每家机构**最新一版**，历史被覆盖，算不出「上次说多少、这次说多少」。
    这张表保留每一篇，才能做真正的修正（revision）与一致预期的时序。

边界：
  · 仅 A 股（qType=0 是个股研报）；港股/新加坡股没有免费等价物；
  · 2015 年之前接口不提供；
  · 部分研报（新股、无覆盖）三个预测字段为空，仍入库，只是不参与均值。

依赖：仅标准库。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Dict, List, Optional

from . import datasource as ds
from . import store

_BASE = "https://reportapi.eastmoney.com/report/list?"
_HEAD = {"Referer": "https://data.eastmoney.com/report/"}
_PAGE = 100
_EARLIEST = 2015                    # 接口最早只到 2015 年
_MAX_SEG = 2000                     # 单段最多翻多少页（防御上游异常）


def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _page(no: int, begin: str, end: str, code: Optional[str] = None) -> Optional[dict]:
    params = {
        "pageSize": str(_PAGE), "pageNo": str(no), "qType": "0",
        "fields": "", "beginTime": begin, "endTime": end,
    }
    if code:
        params["code"] = code
    # cb 必须留空：带上回调名返回的是 JSONP，解析要多一步
    url = _BASE + urllib.parse.urlencode(params)
    try:
        return json.loads(ds.http_get(url, _HEAD, timeout=30))
    except Exception:                                   # noqa: BLE001
        return None


def _parse(items: List[dict]) -> List[dict]:
    """接口原始行 → report 表结构。年份换算成绝对财年，避免「今年」漂移。"""
    out: List[dict] = []
    for it in items or []:
        code = (it.get("stockCode") or "").strip()
        pub = (it.get("publishDate") or "")[:10]
        iid = it.get("infoCode")
        if not code or not pub or not iid:
            continue
        py = _int(pub[:4])
        if not py:
            continue
        e1, e2, e3 = (ds._num(it.get("predictThisYearEps")),
                      ds._num(it.get("predictNextYearEps")),
                      ds._num(it.get("predictNextTwoYearEps")))
        out.append({
            "info_code": iid,
            "publish_date": pub,
            "stock_code": code,
            "stock_name": it.get("stockName"),
            "org_code": it.get("orgCode"),
            "org_name": it.get("orgSName") or it.get("orgName"),
            "researcher": (it.get("researcher") or "").strip() or None,
            # 有评级就用本次评级，没评级退回东财的标准评级名
            "rating": it.get("emRatingName") or it.get("sRatingName"),
            "last_rating": it.get("lastEmRatingName"),
            "rating_change": _int(it.get("ratingChange")),
            "target_price": ds._num(it.get("indvAimPriceT")),
            "eps_y1": e1, "year_1": py if e1 is not None else None,
            "eps_y2": e2, "year_2": py + 1 if e2 is not None else None,
            "eps_y3": e3, "year_3": py + 2 if e3 is not None else None,
            "eps_last": ds._num(it.get("predictLastYearEps")),
            "industry": it.get("indvInduName") or it.get("industryName"),
            "title": (it.get("title") or "")[:120],
        })
    return out


def _sync_segment(seg: str, begin: str, end: str, on_progress=None) -> int:
    """拉取 [begin, end] 区间内的全部研报，返回新增条数。"""
    first = _page(1, begin, end)
    if not first or not first.get("data"):
        return 0
    hits = int(first.get("hits") or 0)
    pages = min(_MAX_SEG, max(1, (hits + _PAGE - 1) // _PAGE))

    rows = _parse(first["data"])
    new = store.save_reports(rows)
    if on_progress:
        on_progress(seg, 1, pages, new)

    if pages > 1:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for i, res in enumerate(pool.map(lambda p: _page(p, begin, end),
                                             range(2, pages + 1)), 2):
                if not res:
                    continue
                rows = _parse(res.get("data") or [])
                if rows:
                    new += store.save_reports(rows)
                if on_progress:
                    on_progress(seg, i, pages, new)
    return new


def sync(force: bool = False, on_progress=None) -> dict:
    """
    按年分段同步研报。往年拉过一次就不再回源（历史不会变），
    当年每次重拉（新研报每天都有），靠 info_code 主键去重。

    on_progress(seg, done_pages, total_pages, new_rows)
    """
    today = date.today()
    total_new = 0
    for y in range(_EARLIEST, today.year + 1):
        seg = str(y)
        is_current = (y == today.year)
        if not force and not is_current and store.report_seg_done(seg):
            continue
        begin = f"{y}-01-01"
        end = today.isoformat() if is_current else f"{y}-12-31"
        n = _sync_segment(seg, begin, end, on_progress)
        total_new += n
        # 当年标为已完成也没关系：它会每次重拉，标记只是记录一次成功的同步
        store.report_seg_save(seg, n)
    return {"new": total_new, **store.report_stats()}


# 单只股票的检查冷却：研报不是秒级更新的，同一只 5 分钟内重复点弹窗没必要再问上游
_CHECKED: Dict[str, float] = {}
_CHECK_TTL = 300


def refresh_stock(code: str, force: bool = False) -> dict:
    """
    检查上游有没有比库里更新的研报，有就只补这一段（通常 0~2 篇、1 个请求）。

    前端点开弹窗时调用：先用库里的数据秒开，再在后台悄悄补最新几篇，
    补完前端重渲染一次。这样既能「实时」，又不会让弹窗等网络。
    """
    if not re.match(r"^\d{6}$", code):
        return {"checked": False, "new": 0, "reason": "仅 A 股有研报数据"}

    if not force and time.time() - _CHECKED.get(code, 0.0) < _CHECK_TTL:
        return {"checked": True, "new": 0, "cached": True,
                "latest": store.stock_latest_report_date(code)}

    latest = store.stock_latest_report_date(code)
    begin = latest or f"{_EARLIEST}-01-01"
    end = date.today().isoformat()

    first = _page(1, begin, end, code)
    if first is None:
        return {"checked": False, "new": 0, "reason": "上游接口暂时不可用"}
    hits = int(first.get("hits") or 0)
    pages = min(20, max(1, (hits + _PAGE - 1) // _PAGE))

    new = store.save_reports(_parse(first.get("data") or []))
    if pages > 1:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for res in pool.map(lambda p: _page(p, begin, end, code),
                                range(2, pages + 1)):
                if res:
                    new += store.save_reports(_parse(res.get("data") or []))

    _CHECKED[code] = time.time()
    return {"checked": True, "new": new, "from": begin, "to": end,
            "pages": pages, "latest": store.stock_latest_report_date(code)}


# --------------------------------------------------------------------------- #
#  单只股票：一致预期时序 + 修正统计
# --------------------------------------------------------------------------- #
def _expand(r: dict) -> Dict[int, float]:
    """一篇研报 → {财年: 预测EPS}，只保留非零预测。"""
    out: Dict[int, float] = {}
    for i in (1, 2, 3):
        y, e = r.get(f"year_{i}"), r.get(f"eps_y{i}")
        if y and e is not None and e != 0:
            out[int(y)] = float(e)
    return out


def _consensus_series(rows: List[dict], targets: List[int]) -> List[dict]:
    """
    按月的一致预期：同一机构在同一个月内只取最新一篇，再对机构取算术平均。

    注意不能直接对所有研报求平均——机构发研报的频率差别很大，
    频次高的会系统性主导均值；先按机构归并才是一致预期的通行做法。
    """
    best: Dict[tuple, tuple] = {}
    for r in rows:
        m = r["publish_date"][:7]
        org = r.get("org_code") or r.get("org_name") or r["info_code"]
        cur = best.get((m, org))
        if not cur or r["publish_date"] > cur[0]:
            best[(m, org)] = (r["publish_date"], _expand(r))

    agg: Dict[tuple, List[float]] = defaultdict(list)
    for (m, _org), (_d, ex) in best.items():
        for y, e in ex.items():
            if y in targets:
                agg[(m, y)].append(e)

    out: List[dict] = []
    for m in sorted({k[0] for k in agg}):
        item: dict = {"m": m, "v": {}, "n": {}}
        for y in targets:
            vals = agg.get((m, y)) or []
            if vals:
                item["v"][str(y)] = round(sum(vals) / len(vals), 3)
                item["n"][str(y)] = len(vals)
        if item["v"]:
            out.append(item)
    return out


def _revision(rows: List[dict], targets: List[int], days: int) -> Dict[str, dict]:
    """
    近 N 天的预测修正：每家机构比较「最后一篇」与「它上一篇」对同一财年的 EPS。

    阈值 0.5%：低于这个幅度视为维持，避免把四舍五入当成调价。
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    by_org: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_org[r.get("org_code") or r.get("org_name") or r["info_code"]].append(r)

    out: Dict[str, dict] = {}
    for y in targets:
        up = down = flat = 0
        for _org, rs in by_org.items():
            rs = sorted(rs, key=lambda x: (x["publish_date"], x["info_code"]))
            if rs[-1]["publish_date"] < cutoff:
                continue                       # 最近一篇不在窗口内，不算修正
            cur = _expand(rs[-1]).get(y)
            if cur is None:
                continue
            prev = None
            for r in reversed(rs[:-1]):
                prev = _expand(r).get(y)
                if prev is not None:
                    break
            if prev is None or prev == 0:
                continue
            chg = (cur - prev) / abs(prev)
            if chg > 0.005:
                up += 1
            elif chg < -0.005:
                down += 1
            else:
                flat += 1
        out[str(y)] = {"up": up, "down": down, "flat": flat}
    return out


def stock_forecast(code: str, detail_limit: int = 200) -> dict:
    """
    单只股票的历史预测全景，供前端弹窗使用。

    返回 {} 之外的字段都是前端直接渲染的：targets / series / revision / detail。
    只统计「至少 3 篇研报覆盖」的财年，避免一两家机构的口径被当成一致预期。
    """
    rows = store.load_stock_reports(code)
    if not rows:
        # 只有空结果才需要全库统计：COUNT(*) 扫 14 万行，批量调用时不能每只都算一次
        stats = store.report_stats()
        return {"code": code, "empty": True,
                "msg": "该股暂无研报数据。先执行 `python run.py --sync-reports` 拉取。",
                "db_rows": stats["rows"], "db_stocks": stats["stocks"]}

    cnt: Dict[int, int] = defaultdict(int)
    for r in rows:
        for y in _expand(r):
            cnt[y] += 1
    # 取**最近**的财年：报告的默认视角是「今年/明年/后年」，
    # 老年度（2020E 之类）虽然研报更多，但对当下没有意义
    targets = [y for y in sorted(cnt, reverse=True) if cnt[y] >= 3][:3]
    if not targets:                            # 覆盖太少也至少给一个年度
        targets = sorted(cnt, reverse=True)[:1]
    targets.sort()

    series = _consensus_series(rows, targets)
    rev30 = _revision(rows, targets, 30)
    rev90 = _revision(rows, targets, 90)

    # 每个财年各自的一致预期时序：研报普遍只给「今年/明年」两年预测，
    # 第三年（后年）填的人很少，所以 2026E 的曲线要等到 2025 年才出现。
    # 想看得更早，只能切到 2025E / 2024E 这些当年被预测过的财年 —— 前端靠这份数据切换。
    years_all = sorted([y for y, n in cnt.items() if n >= 3], reverse=True)
    series_by_year: Dict[str, List[dict]] = {}
    for y in years_all:
        pts = _consensus_series(rows, [y])
        series_by_year[str(y)] = [
            {"m": x["m"], "v": x["v"][str(y)], "n": x["n"].get(str(y), 0)}
            for x in pts if str(y) in x["v"]
        ]

    detail: List[dict] = []
    for r in reversed(rows):
        ex = _expand(r)
        detail.append({
            "date": r["publish_date"],
            "org": r.get("org_name"),
            "researcher": r.get("researcher"),
            "rating": r.get("rating"),
            "last_rating": r.get("last_rating"),
            "target": r.get("target_price"),
            "eps": {str(k): v for k, v in ex.items()},
            "title": r.get("title"),
        })
        if detail_limit and len(detail) >= detail_limit:
            break

    # 逐机构的完整预测序列（弹窗里「按机构看走势」用）：每家机构历次研报
    # 对同一财年的预测并排看，就能看出它是在持续上调还是掉头下修
    by_org: Dict[str, dict] = {}
    for r in rows:
        key = r.get("org_code") or r.get("org_name") or r["info_code"]
        ex = _expand(r)
        if not ex:
            continue                    # 只给评级不给预测的研报，画不出走势
        item = by_org.setdefault(key, {"key": key,
                                       "name": r.get("org_name") or key,
                                       "rows": []})
        item["rows"].append({
            "d": r["publish_date"],
            "e": {str(k): v for k, v in ex.items()},
            "r": r.get("rating"),
            "t": r.get("target_price"),
        })
    # 最近还在更新的机构排前面
    orgs = sorted(by_org.values(), key=lambda x: x["rows"][-1]["d"], reverse=True)

    # 两个口径分开：有些研报只给评级/目标价、没填分年度预测（银行股尤其多），
    # 把它们算进「有预测的机构数」会让覆盖度虚高
    def _org(r):
        return r.get("org_code") or r.get("org_name") or r["info_code"]
    orgs_all = {_org(r) for r in rows}
    orgs_fc = {_org(r) for r in rows if _expand(r)}
    return {
        "code": code,
        "name": rows[-1].get("stock_name") or code,
        "total": len(rows),
        "n_org": len(orgs_all),
        "n_org_fc": len(orgs_fc),
        "start": rows[0]["publish_date"],
        "end": rows[-1]["publish_date"],
        "targets": targets,
        "years_all": years_all,
        "series": series,
        "series_by_year": series_by_year,
        "revision": {"d30": rev30, "d90": rev90},
        "detail": detail,
        "orgs": orgs,
        "industry": rows[-1].get("industry"),
    }


def forecast_panel(codes: List[str], per_org: int = 4, months: int = 36,
                   limit: int = 40) -> List[dict]:
    """
    多只股票的**逐机构历史预测**，不做任何聚合。

    「复制给 AI」用：平均值会把分歧抹掉 —— 20 家机构里 12 家给 30 元、
    8 家给 18 元，均值 25 元看着一致，实际是两派。所以按机构分组、
    每家保留最近 per_org 篇，AI 才能看出「谁一直在上调、谁在连续下修」。

    months 是时间窗口：三四年前的预测对当下判断没什么价值，却会把报告撑到读不完。
    """
    cutoff = (date.today() - timedelta(days=int(months * 30.5))).isoformat()
    out: List[dict] = []
    for code in codes[:limit]:
        if not re.match(r"^\d{6}$", code):
            continue                       # 港股/美股没有研报数据
        rows = store.load_stock_reports(code)
        if not rows:
            continue

        by_org: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            if r["publish_date"] >= cutoff:
                by_org[r.get("org_code") or r.get("org_name") or r["info_code"]].append(r)

        orgs: List[dict] = []
        years: set = set()
        for key, rs in by_org.items():
            rs.sort(key=lambda x: (x["publish_date"], x["info_code"]))
            idx = [i for i in range(len(rs)) if _expand(rs[i])]
            if not idx:
                continue                   # 只给评级、没填 EPS 的研报对预测分析没意义

            # 1) 最近 per_org 篇：看这家机构当下在说什么
            chosen = idx[-per_org:]
            # 2) 补齐旧财年：某些财年的预测只出现在更早的研报里，只看最近几篇
            #    会让整列都是「—」，AI 就看不到「2024E 当年被预测成多少」
            have: set = set()
            for i in chosen:
                have.update(_expand(rs[i]))
            org_years = sorted({y for i in idx for y in _expand(rs[i])}, reverse=True)[:5]
            for y in org_years:
                if y in have:
                    continue
                for i in reversed(idx):        # 该财年最后一篇
                    if y in _expand(rs[i]):
                        if i not in chosen:
                            chosen.append(i)
                        have.update(_expand(rs[i]))
                        break

            picked: List[dict] = []
            for i in sorted(set(chosen), reverse=True):
                cur = _expand(rs[i])
                prev: Dict[int, float] = {}
                for j in range(i - 1, -1, -1):
                    prev = _expand(rs[j])
                    if prev:
                        break
                chg: Dict[str, float] = {}
                for y, v in cur.items():
                    p = prev.get(y)
                    if p:
                        chg[str(y)] = round((v - p) / abs(p) * 100, 1)
                years.update(cur)
                picked.append({
                    "d": rs[i]["publish_date"],
                    "r": rs[i].get("rating"),
                    "t": rs[i].get("target_price"),
                    "e": {str(k): v for k, v in cur.items()},
                    "chg": chg,
                })
            if picked:
                orgs.append({"name": rs[-1].get("org_name") or key, "rows": picked})

        if not orgs:
            continue
        orgs.sort(key=lambda o: o["rows"][0]["d"], reverse=True)

        # 财年列：保留所有「足够多行都给过预测」的年度，旧财年也一并列出 ——
        # AI 需要看到「2024E 当时被预测成多少、后来砍到多少」这种跨年对比
        n_rows = sum(len(o["rows"]) for o in orgs)
        need = max(2, int(n_rows * 0.1))
        ys = sorted(y for y in years
                    if sum(1 for o in orgs for it in o["rows"] if str(y) in it["e"]) >= need)

        out.append({
            "code": code,
            "name": rows[-1].get("stock_name") or code,
            "total": len(rows),
            "n_org": len(by_org),
            "years": ys,
            "orgs": orgs,
        })
    return out


def batch_forecast(codes: List[str], limit: int = 40) -> Dict[str, dict]:
    """
    多只股票的预测摘要（不含明细与序列），供「复制给 AI」的报告用。

    当期一致预期取「最近一个至少 3 家机构出预测的月份」：只看最后一个月
    常常只有 1 家更新，那一个数字的代表性太差，会误导判断。
    """
    out: Dict[str, dict] = {}
    for code in codes[:limit]:
        if not re.match(r"^\d{6}$", code):
            continue                       # 港股/美股没有研报数据，直接跳过
        d = stock_forecast(code, detail_limit=0)
        if d.get("empty"):
            continue
        series = d.get("series") or []
        pick = None
        for s in reversed(series):
            if any(int(v) >= 3 for v in (s.get("n") or {}).values()):
                pick = s
                break
        if pick is None and series:
            pick = series[-1]
        out[code] = {
            "name": d.get("name"),
            "total": d.get("total"),
            "n_org": d.get("n_org"),
            "n_org_fc": d.get("n_org_fc"),
            "targets": d.get("targets"),
            "month": (pick or {}).get("m"),
            "latest": (pick or {}).get("v") or {},
            "latest_n": (pick or {}).get("n") or {},
            "rev90": (d.get("revision") or {}).get("d90") or {},
            "last_date": d.get("end"),
            "industry": d.get("industry"),
        }
    return out
