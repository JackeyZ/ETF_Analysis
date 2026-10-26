# -*- coding: utf-8 -*-
"""内置 Web 服务（仅标准库 http.server），提供 JSON API 与静态页面。"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.parse
from dataclasses import asdict
from datetime import date
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List

from . import datasource as ds
from . import hk
from . import peg
from . import reports
from . import store

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# --------------------------------------------------------------------------- #
#  后台扫描任务
# --------------------------------------------------------------------------- #
_SCAN: Dict[str, object] = {
    "running": False, "cancel": False,
    "done": 0, "total": 0, "current": "", "stage": "",
    "results": [], "error": "", "started_at": 0.0, "mode": "ttm",
}
_SCAN_LOCK = threading.Lock()

# 债券/商品/货币类 ETF 没有股票持仓，直接跳过，省下大量无效请求
_SKIP_KW = ds.SKIP_KEYWORDS


def _eligible_etfs(limit: int) -> List[dict]:
    etfs = ds.fetch_etf_list()
    out = [e for e in etfs if not any(k in e["name"] for k in _SKIP_KW)]
    return out[:limit]


def _scan_worker(limit: int, mode: str, pe_mode: str = "mixed") -> None:
    def cancelled() -> bool:
        with _SCAN_LOCK:
            return bool(_SCAN["cancel"])

    def stage(text: str) -> None:
        with _SCAN_LOCK:
            _SCAN["stage"] = text

    def progress(i: int, total: int, name: str) -> None:
        with _SCAN_LOCK:
            _SCAN["done"] = i
            _SCAN["total"] = total
            _SCAN["current"] = name
            _SCAN["stage"] = ""

    try:
        stage("读取 ETF 列表…")
        etfs = _eligible_etfs(limit)
        with _SCAN_LOCK:
            _SCAN["total"] = len(etfs)
            _SCAN["done"] = 0
        if cancelled():
            raise _Cancelled()

        results = peg.analyze_many(etfs, mode,
                                   on_progress=progress,
                                   on_stage=stage,
                                   cancel=cancelled,
                                   pe_mode=pe_mode)
        # analyze_many 被取消时会提前返回空列表，这里统一走取消分支，
        # 避免界面显示成「完成」却没有结果
        if cancelled():
            raise _Cancelled()
        with _SCAN_LOCK:
            _SCAN["results"] = results
            _SCAN["running"] = False
            _SCAN["cancel"] = False
            _SCAN["current"] = "完成"
            _SCAN["stage"] = ""
    except _Cancelled:
        with _SCAN_LOCK:
            _SCAN["running"] = False
            _SCAN["cancel"] = False
            _SCAN["stage"] = "已取消"
            _SCAN["results"] = []
    except Exception as exc:                            # noqa: BLE001
        with _SCAN_LOCK:
            _SCAN["error"] = f"{type(exc).__name__}: {exc}"
            _SCAN["running"] = False
            _SCAN["cancel"] = False
            _SCAN["stage"] = ""
            traceback.print_exc()


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #
#  后台长任务：历史回填 / 全市场 PE 初始化
# --------------------------------------------------------------------------- #
# 这两件事都要跑很久（分钟到小时级），必须异步 + 可轮询 + 可取消，
# 否则 HTTP 请求会一直挂着，界面只剩一个转圈。
_JOB: Dict[str, object] = {
    "running": False, "cancel": False, "kind": "", "pct": 0.0,
    "stage": "", "done": 0, "total": 0,
    "result": None, "error": "", "started_at": 0.0,
}
_JOB_LOCK = threading.Lock()

# 分析后的「自动历史回填」独立状态：不占用 _JOB（避免和初始化/回填任务互斥），
# 后台线程跑，前端轮询 /api/autobf/status 感知完成后刷新历史图表。
_AUTOBF: Dict[str, object] = {"running": False, "code": "", "done": False, "error": ""}
_AUTOBF_LOCK = threading.Lock()

# PE 百分位排行的进程内缓存：数据一天最多变一次（收盘后回填/分析更新），
# 排行计算是全表扫描，30 分钟缓存足够，秒级刷新没有意义。
_PE_RANK_MEMO: Dict[str, object] = {"key": None, "ts": 0.0, "data": None}


def _job_worker(kind: str, **kw) -> None:
    def cancelled() -> bool:
        with _JOB_LOCK:
            return bool(_JOB["cancel"])

    def prog(pct, label) -> None:
        """回填用：直接给总百分比。取 max 保证单调不回退。"""
        with _JOB_LOCK:
            _JOB["pct"] = round(max(float(_JOB["pct"]), float(pct)), 1)
            _JOB["stage"] = label

    def prog_count(done: int, total: int, label: str) -> None:
        """初始化用：按只数计，百分比由这里算。"""
        with _JOB_LOCK:
            _JOB["done"], _JOB["total"] = done, total
            _JOB["stage"] = label
            _JOB["pct"] = round(done / total * 100, 1) if total else 0.0

    result = None
    try:
        if kind == "backfill":
            code, days, force = kw["code"], kw["days"], kw["force"]
            n = peg.backfill_etf(code, days=days, force=force,
                                 on_progress=prog, cancel=cancelled)
            daily = store.get_history(code, days + 10)
            result = {
                "code": code, "days": days, "rows": n, "daily": daily,
                "by_year": peg.aggregate_history(daily, "year"),
                "by_quarter": peg.aggregate_history(daily, "quarter"),
                "by_month": peg.aggregate_history(daily, "month"),
                "earliest": ds.pe_history_earliest(),
            }
        elif kind == "initpe":
            result = ds.init_all_stock_pe(on_progress=prog_count, cancel=cancelled)
        elif kind == "inithk":
            result = hk.init_all_hk(on_progress=prog_count, cancel=cancelled)
        elif kind == "initvaluation":
            result = ds.init_all_stock_valuation(
                on_progress=prog_count, cancel=cancelled)
        elif kind == "initdiv":
            result = ds.init_all_stock_div(on_progress=prog_count, cancel=cancelled)
        elif kind == "batchrank":
            result = peg.batch_backfill_rank(top=int(kw.get("top", 160)),
                                             on_progress=prog_count,
                                             cancel=cancelled)
        elif kind == "reports":
            # 研报按「年段」翻页，单段进度会反复归零，这里换算成跨年度的总进度
            years_total = max(1, date.today().year - reports._EARLIEST + 1)
            seg_no: Dict[str, int] = {}

            def prog_reports(seg, done, total, new):
                if seg not in seg_no:
                    seg_no[seg] = len(seg_no) + 1
                base = (seg_no[seg] - 1) / years_total * 100
                step = (done / total * 100 / years_total) if total else 0
                with _JOB_LOCK:
                    _JOB["pct"] = round(min(99.9, base + step), 1)
                    _JOB["stage"] = f"{seg} 年 {done}/{total} 页 · 新增 {new} 篇"
                    _JOB["done"], _JOB["total"] = done, total

            result = reports.sync(force=bool(kw.get("force")),
                                  on_progress=prog_reports)
        else:
            raise ValueError(f"未知任务类型：{kind}")

        canc = cancelled()
        with _JOB_LOCK:
            _JOB["result"] = result
            _JOB["running"] = False
            _JOB["cancel"] = False
            _JOB["stage"] = "已取消" if canc else "完成"
            _JOB["pct"] = 100.0
    except Exception as exc:                            # noqa: BLE001
        traceback.print_exc()
        with _JOB_LOCK:
            _JOB["error"] = f"{type(exc).__name__}: {exc}"
            _JOB["running"] = False
            _JOB["cancel"] = False
            _JOB["stage"] = ""


def _auto_backfill_history(code: str) -> None:
    """
    分析后自动补齐该 ETF 的历史估值序列（PE/PB/PS 逐日快照与历史分位）。

    回填本身是增量判定（已算过的日期秒回），满足任一条件即触发：
    - 历史缺失或不足约 3 个月交易日 —— 首次全量回填；
    - 股息率列尚未补算 —— 老库升级的一次性补算；
    - 历史最新日期落后最近交易日超过 3 个自然日 —— 每次分析自动把历史补到最新。
      港股历史尤其靠这一步推进：中间的空档（工具没开的那几天）由
      backfill_etf 的 detect_kline_gaps 逐只补拉。

    失败静默降级，不影响本次分析。
    """
    try:
        # v10：两套口径各自检查（fixed 为主，pit 也需补齐）
        hist_fixed = store.get_history(code, 99999, "fixed")
        hist_pit = store.get_history(code, 99999, "pit")
        version_old = (store.get_calc_version(code, "fixed") < peg.CALC_VERSION
                       or store.get_calc_version(code, "pit") < peg.CALC_VERSION)
        need = (not hist_fixed or len(hist_fixed) < 60
                or not hist_pit
                or not all(r.get("div_calc") for r in hist_fixed)
                or not all(r.get("div_calc") for r in hist_pit)
                or version_old)
        if not need:
            # 历史完整且股息率已补：仅当落后最近交易日超过 3 个自然日才增量回填
            latest = hist_fixed[0]["date"]             # get_history 降序，hist[0] 最新
            y, m, d = (int(x) for x in latest.split("-"))
            if (date.today() - date(y, m, d)).days < 3:
                return
    except Exception:                                   # noqa: BLE001
        return
    with _AUTOBF_LOCK:
        if _AUTOBF["running"]:
            return                                    # 已有回填在跑，别叠请求
        _AUTOBF.update(running=True, code=code, done=False, error="")

    def worker() -> None:
        try:
            # 先补齐最近 35 天缺失的全市场 PE：工具没开的日子会留下空档，
            # 导致「最近一月逐日」缺天（只有 10 来天）。只在缺天时回源。
            try:
                ds.backfill_recent_market_pe(35)
            except Exception:                       # noqa: BLE001
                pass
            # 全历史（个股每日 PE 上游最早 2018-01-02，约 8.6 年）。
            # 口径版本落后时必须 force：backfill_etf 的增量判定只看「日期在不在
            # 库里」，不 force 会直接判定"已算满"返回，新增列永远补不出来。
            peg.backfill_etf(code, days=3650, force=version_old)
        except Exception as exc:                       # noqa: BLE001
            with _AUTOBF_LOCK:
                _AUTOBF["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with _AUTOBF_LOCK:
                _AUTOBF["running"] = False
                _AUTOBF["done"] = True

    threading.Thread(target=worker, daemon=True).start()


# --------------------------------------------------------------------------- #
#  请求处理
# --------------------------------------------------------------------------- #
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    # ---------- utils ----------
    def end_headers(self):
        # 允许跨域：页面可能用 127.0.0.1 打开、却要探测 localhost（或反之），
        # 没有 CORS 时浏览器会直接拦掉这个探测请求。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        # 静态页与 API 一律禁缓存：整个前端就是单文件 index.html（无外部资源），
        # 不禁的话浏览器会拿旧 HTML，改版后「界面上没有新功能」多半是它。
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, msg: str, status: int = 500):
        self._json({"ok": False, "error": msg}, status)

    def log_message(self, fmt, *args):                  # 静音访问日志
        pass

    # ---------- GET ----------
    def do_GET(self):
        # 安全网：任何未被捕获的异常也必须返回 JSON，
        # 否则前端 res.json() 会拿到非 JSON 正文，报出难以理解的解析错误。
        try:
            return self._handle_get()
        except Exception as exc:                        # noqa: BLE001
            traceback.print_exc()
            try:
                self._json({"ok": False, "error": f"服务器内部错误：{type(exc).__name__}: {exc}"}, 500)
            except Exception:                           # noqa: BLE001
                pass

    def do_POST(self):
        self._json({"ok": False, "error": "仅支持 GET 请求"}, 405)

    def _handle_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if not path.startswith("/api/"):
            return super().do_GET()

        try:
            if path == "/api/etfs":
                etfs = ds.fetch_etf_list()
                kw = (qs.get("kw") or [""])[0].strip()
                if kw:
                    etfs = [e for e in etfs
                            if kw.lower() in e["name"].lower() or kw in e["code"]]
                return self._json({"ok": True, "data": etfs[: int((qs.get("limit") or [200])[0])]})

            if path == "/api/funds":
                # 全市场基金（含场外）搜索，约 2.8 万只；服务端过滤，避免前端拉全量
                kw = (qs.get("kw") or [""])[0].strip()
                limit = int((qs.get("limit") or [40])[0])
                funds = ds.fetch_all_funds()
                if kw:
                    k = kw.lower()
                    hit_code = [f for f in funds if f["code"].startswith(kw)]
                    hit_name = [f for f in funds if k in f["name"].lower()]
                    merged, seen = [], set()
                    for f in hit_code + hit_name:
                        if f["code"] not in seen:
                            seen.add(f["code"])
                            merged.append(f)
                        if len(merged) >= limit:
                            break
                    funds = merged
                else:
                    funds = funds[:limit]
                return self._json({"ok": True, "data": funds})

            if path == "/api/analyze":
                code = (qs.get("code") or [""])[0].strip()
                mode = (qs.get("mode") or ["ttm"])[0]
                # 财报口径：mixed（各股用自己最新已披露期）| uniform（全组合同一期）
                pe_mode = "uniform" if (qs.get("pemode") or ["mixed"])[0] == "uniform" \
                    else "mixed"
                if not code:
                    return self._fail("缺少 code 参数", 400)
                # 名称先查全市场基金（含场外），再退回 ETF 列表；
                # analyze_etf 内部也会再兜底一次
                f = ds.lookup_fund(code)
                name = (f or {}).get("name") or code
                # 行情与财报由 analyze_etf 内部按需载入（各自带缓存 / 本地库），
                # 不要在这里预拉，否则会多下载一倍用不到的报告期
                r = peg.analyze_etf(code, name, mode, series_quarters=20,
                                    pe_mode=pe_mode)
                # 历史缺失时自动回填，让「历史月度估值 / PB·PS 历史分位」完整
                # （首次约 30~60 秒，之后命中增量判断秒回；失败静默降级）
                _auto_backfill_history(code)
                d = asdict(r)
                # 场外基金（非场内 ETF 代码）：按最新持仓成分股当日涨跌加权，估算当天涨跌
                if not code.startswith(("51", "56", "58", "15")):
                    d["est_pct"] = peg.estimate_day_pct(r.stocks)
                    # 场内 ETF 的规模来自 clist 名录；场外基金不在名录里，
                    # 从基金档案补（A/C 份额各自独立，如 013816=2.37 亿 / 013817=5.02 亿）
                    if d.get("market_cap") is None:
                        try:
                            d["market_cap"] = ds.fetch_fund_scale(code)
                        except Exception:               # noqa: BLE001
                            pass
                return self._json({"ok": True, "data": d})

            if path == "/api/stock/forecast/refresh":
                # 弹窗打开后异步调用：查上游有没有新研报，有就增量拉回来
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    return self._fail("缺少 code 参数", 400)
                try:
                    return self._json({"ok": True,
                                       "data": reports.refresh_stock(code)})
                except Exception as exc:                       # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})

            if path == "/api/stock/forecast/panel":
                # 逐机构预测明细（「复制给 AI」用，不做聚合）
                raw = (qs.get("codes") or [""])[0]
                codes = [c.strip() for c in raw.split(",") if c.strip()]
                if not codes:
                    return self._fail("缺少 codes 参数", 400)
                n = int((qs.get("n") or [4])[0])            # 每家机构保留几篇
                m = int((qs.get("months") or [36])[0])      # 时间窗口（月）
                try:
                    return self._json({"ok": True,
                                       "data": reports.forecast_panel(
                                           codes, per_org=n, months=m)})
                except Exception as exc:                       # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})

            if path == "/api/stock/forecast/batch":
                # 多只股票的预测摘要（「复制给 AI」的报告里一次取前二十大）
                raw = (qs.get("codes") or [""])[0]
                codes = [c.strip() for c in raw.split(",") if c.strip()]
                if not codes:
                    return self._fail("缺少 codes 参数", 400)
                try:
                    return self._json({"ok": True,
                                       "data": reports.batch_forecast(codes)})
                except Exception as exc:                       # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})

            if path == "/api/stock/forecast":
                # 单只股票的历史分析师预测（点击成分股弹窗用）
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    return self._fail("缺少 code 参数", 400)
                try:
                    return self._json({"ok": True,
                                       "data": reports.stock_forecast(code)})
                except Exception as exc:                       # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})

            if path == "/api/scan/start":
                # 关键：这里必须立即返回。以前在此处同步拉 ETF 列表，
                # 前端 await 不到响应就不会启动轮询，界面永远停在「初始化」。
                with _SCAN_LOCK:
                    if _SCAN["running"]:
                        return self._json({"ok": True, "msg": "扫描进行中"})
                    _SCAN.update(running=True, cancel=False, done=0, total=0,
                                 current="", stage="排队中", results=[], error="",
                                 started_at=time.time())
                limit = int((qs.get("limit") or [60])[0])
                mode = (qs.get("mode") or ["ttm"])[0]
                pe_mode = "uniform" if (qs.get("pemode") or ["mixed"])[0] == "uniform" \
                    else "mixed"
                threading.Thread(target=_scan_worker, args=(limit, mode, pe_mode),
                                 daemon=True).start()
                return self._json({"ok": True, "msg": "已启动"})

            if path == "/api/scan/cancel":
                with _SCAN_LOCK:
                    if _SCAN["running"]:
                        _SCAN["cancel"] = True
                return self._json({"ok": True})

            if path == "/api/scan/status":
                with _SCAN_LOCK:
                    snap = dict(_SCAN)
                return self._json({"ok": True, "data": snap})

            if path == "/api/pe-rank":
                # 全市场 ETF 的 PE 历史百分位排行（windows=5/3/0 年，0=成立以来）
                years = float((qs.get("years") or [0])[0] or 0)
                top = int((qs.get("top") or [40])[0])
                key = (years, top)
                now = time.time()
                if _PE_RANK_MEMO["key"] != key or now - _PE_RANK_MEMO["ts"] > 1800:
                    _PE_RANK_MEMO["data"] = store.pe_ranking(years=years or None,
                                                             top=top)
                    _PE_RANK_MEMO["key"], _PE_RANK_MEMO["ts"] = key, now
                return self._json({"ok": True, "data": _PE_RANK_MEMO["data"]})

            if path == "/api/ping":
                return self._json({"ok": True, "data": {"pong": True, "ts": time.time()}})

            if path == "/api/store":
                return self._json({"ok": True, "data": store.stats_all()})

            if path == "/api/job/start":
                kind = (qs.get("kind") or [""])[0]
                kw: Dict[str, object] = {}
                if kind == "backfill":
                    code = (qs.get("code") or [""])[0].strip()
                    if not code:
                        return self._fail("缺少 code 参数", 400)
                    years = float((qs.get("years") or [0])[0])
                    days = int(years * 365) if years else int((qs.get("days") or [180])[0])
                    force = (qs.get("force") or [""])[0] in ("1", "true")
                    kw = {"code": code, "days": days, "force": force}
                elif kind == "batchrank":
                    kw = {"top": int((qs.get("top") or [160])[0])}
                elif kind == "reports":
                    kw = {"force": (qs.get("force") or [""])[0] in ("1", "true", "yes")}
                elif kind not in ("initpe", "inithk", "initvaluation", "initdiv"):
                    return self._fail("未知任务类型", 400)

                # 关键：立即返回，不要在这儿干等；否则前端 await 不到响应，
                # 轮询起不来，界面会一直停在「初始化」。
                #
                # 已经有任务在跑时必须报错（409）而不是回 ok:true：
                # 以前回 ok:true，前端以为自己启动成功了就挂上轮询，
                # 结果把上一个任务的进度显示到了本次任务的进度条上
                # ——「点了港股初始化，里面跑的却是 A 股」就是这么来的。
                with _JOB_LOCK:
                    if _JOB["running"]:
                        return self._fail(
                            f"已有任务在进行（{_JOB['kind']}），"
                            f"请等它结束或先点「取消」", 409)
                    _JOB.update(running=True, cancel=False, kind=kind, pct=0.0,
                                stage="排队中", done=0, total=0, result=None,
                                error="", started_at=time.time())
                threading.Thread(target=_job_worker, args=(kind,), kwargs=kw,
                                 daemon=True).start()
                return self._json({"ok": True, "msg": "已启动"})

            if path == "/api/job/cancel":
                with _JOB_LOCK:
                    if _JOB["running"]:
                        _JOB["cancel"] = True
                return self._json({"ok": True})

            if path == "/api/job/status":
                with _JOB_LOCK:
                    snap = dict(_JOB)
                return self._json({"ok": True, "data": snap})

            if path == "/api/autobf/status":
                with _AUTOBF_LOCK:
                    snap = dict(_AUTOBF)
                return self._json({"ok": True, "data": snap})

            if path == "/api/pe-coverage":
                return self._json({"ok": True,
                                   "data": store.pe_coverage(ds.pe_history_earliest())})

            if path == "/api/hk-coverage":
                return self._json({"ok": True,
                                   "data": store.HK.pe_coverage(hk.HK_EARLIEST)})

            if path == "/api/pb-coverage":
                codes = store.known_pe_codes()
                todo = store.pb_pending(sorted(codes))
                return self._json({"ok": True, "data": {
                    "total": len(codes), "done": len(codes) - len(todo)}})

            if path == "/api/div-coverage":
                return self._json({"ok": True, "data": store.div_coverage()})

            if path == "/api/history":
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    return self._fail("缺少 code 参数", 400)
                years = float((qs.get("years") or [0])[0])
                days = int(years * 365) if years else int((qs.get("days") or [180])[0])
                by = (qs.get("by") or ["day"])[0]
                # v10：口径。fixed=最新持仓回溯（默认），pit=当时已知持仓
                basis = (qs.get("basis") or ["fixed"])[0]
                if basis not in ("fixed", "pit"):
                    basis = "fixed"
                rows = store.get_history(code, days, basis)
                if by in ("year", "quarter", "month"):
                    return self._json({"ok": True, "data": peg.aggregate_history(rows, by),
                                       "by": by, "daily_days": len(rows)})
                # by=all：一次返回日线 + 三种聚合，供前端切换视图（只读，不回填）
                if by == "all":
                    return self._json({"ok": True, "data": {
                        "daily": rows,
                        "by_year": peg.aggregate_history(rows, "year"),
                        "by_quarter": peg.aggregate_history(rows, "quarter"),
                        "by_month": peg.aggregate_history(rows, "month"),
                    }})
                return self._json({"ok": True, "data": rows, "by": "day"})

            if path == "/api/kline":
                # ETF/场外基金 历史股价/净值 K 线：按代码前缀分流
                code = (qs.get("code") or [""])[0].strip()
                if not code:
                    return self._fail("缺少 code 参数", 400)
                try:
                    if code.startswith(("51", "56", "58", "15")):
                        data = ds.fetch_etf_kline(code)
                    else:
                        data = ds.fetch_fund_nav(code)
                    return self._json({"ok": True, "data": data})
                except Exception as exc:                       # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})

            if path == "/api/report-stats":
                return self._json({"ok": True, "data": store.report_stats()})

            if path == "/api/meta":
                periods = ds.latest_report_periods()
                return self._json({"ok": True, "data": {
                    "report_periods": periods,
                    "cache_dir": ds.CACHE_DIR,
                }})

            if path == "/api/refresh":
                kind = (qs.get("kind") or ["quotes"])[0]
                if kind == "quotes":
                    ds.fetch_realtime_quotes(force=True)
                elif kind == "etfs":
                    ds.fetch_etf_list(force=True)
                elif kind == "holdings":
                    code = (qs.get("code") or [""])[0]
                    ds.fetch_etf_holdings(code, force=True)
                elif kind == "fin":
                    # 已定稿的历史财报不会再变，没必要强制重下
                    for p in ds.latest_report_periods():
                        if not ds.is_settled(p):
                            ds.fetch_financials(p, force=True)
                return self._json({"ok": True})

            return self._fail("未知接口", 404)
        except Exception as exc:                        # noqa: BLE001
            traceback.print_exc()
            return self._fail(f"{type(exc).__name__}: {exc}")


def _default(o):
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server(host: str = "127.0.0.1", port: int = 8770, open_browser: bool = True):
    srv = Server((host, port), Handler)
    print("=" * 62)
    print("  ETF PEG 实时分析工具")
    print(f"  访问地址: http://127.0.0.1:{port}")
    if host in ("127.0.0.1", "localhost"):
        print(f"            http://localhost:{port}   （127.0.0.1 打不开时换这个）")
    else:
        print(f"            http://{host}:{port}")
    print("=" * 62)
    print("  数据源：东方财富（行情/ETF持仓/业绩报表），全部免费")
    print("  首次分析需拉取全市场行情与财报，约 10~60 秒，之后走本地缓存。")
    print("  若页面报 Unauthorized / 无法连接：多半是浏览器代理或安全软件")
    print("  拦截了本地地址，请在代理设置里放行 127.0.0.1 与 localhost。")
    print("  本窗口不能关闭，关闭即停止服务。Ctrl+C 退出\n")

    if open_browser:
        def _open():
            try:
                import webbrowser
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:                           # noqa: BLE001
                pass
        threading.Timer(1.2, _open).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
        srv.shutdown()
    return srv
