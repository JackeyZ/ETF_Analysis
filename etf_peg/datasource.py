# -*- coding: utf-8 -*-
"""
数据源模块 —— 全部来自免费公开接口，无需注册、无积分、无限额（注意控制频率）。

四类数据：
  1. ETF 列表        东方财富 push2       fs=b:MK0021,b:MK0023（境内 + 跨境 ETF）
  2. ETF 全持仓      天天基金 fundf10     type=jjcc    （季度更新，含权重）
  3. 个股实时 PE-TTM 东方财富 push2       clist/get f9 / stock/get f162
  4. 归母净利润      东方财富 datacenter  RPT_LICO_FN_CPD（全市场批量，含同比）

依赖：仅 Python 标准库。
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

from . import store

store.init()

ssl._create_default_https_context = ssl._create_unverified_context

# 仍在本期披露窗口内的财报，最多缓存这么久就要回源刷新
ACTIVE_FIN_TTL = 6 * 3600

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 债券/商品/货币类 ETF 没有股票持仓，批量任务（扫描 / 排行数据回填）直接跳过
SKIP_KEYWORDS = ("债", "货币", "现金", "黄金", "商品", "豆粕", "有色期",
                 "能源化工", "原油", "豆油", "棕榈", "橡胶", "铁矿", "白银")


# --------------------------------------------------------------------------- #
#  HTTP 基础
# --------------------------------------------------------------------------- #
# 东财行情有多个镜像节点，主节点偶发 502 时轮换
_PUSH_HOSTS = ["https://push2.eastmoney.com",
               "https://82.push2.eastmoney.com",
               "https://48.push2.eastmoney.com",
               "https://push2delay.eastmoney.com"]
_HOST_LOCK = threading.Lock()
_HOST_ORDER = list(_PUSH_HOSTS)      # 动态优先级：最近成功的排前面

# 全局限流：不限频会被东财掐连接，表现为请求长时间挂起，
# 叠加重试后单次扫描可拖到数小时。这里统一限速 ~9 req/s。
_RATE_LOCK = threading.Lock()
_RATE_LAST = [0.0]
_RATE_MIN_GAP = 0.11


def _throttle() -> None:
    with _RATE_LOCK:
        now = time.monotonic()
        gap = _RATE_MIN_GAP - (now - _RATE_LAST[0])
        _RATE_LAST[0] = max(now, now + gap)
        wait = max(0.0, gap)
    if wait:
        time.sleep(wait)


def _promote_host(url: str) -> None:
    """把本次成功的节点提到队首，下次优先使用（节点可用性会随时间变化）。"""
    host = url.split("/api/", 1)[0]
    with _HOST_LOCK:
        if host in _HOST_ORDER:
            _HOST_ORDER.remove(host)
            _HOST_ORDER.insert(0, host)


def http_get(url: str, headers: Optional[dict] = None, timeout: int = 20,
             retries: int = 3, decode: str = "utf-8") -> str:
    if "push2.eastmoney.com" in url:
        tail = url.split("push2.eastmoney.com", 1)[1]
        with _HOST_LOCK:
            order = list(_HOST_ORDER)
        urls = [h + tail for h in order]
    else:
        urls = [url]

    # 关键：尝试次数不能小于节点数。
    # 曾经 retries=3 配 4 个节点，导致队尾的 push2delay 永远轮不到，
    # 而主节点 502 时会一路重试到超时，表现为界面卡死数小时。
    attempts = max(retries, len(urls))
    last = None
    for i in range(attempts):
        u = urls[i % len(urls)]
        try:
            _throttle()
            req = urllib.request.Request(u, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode(decode, "ignore")
            _promote_host(u)
            return body
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(min(0.4 * (i + 1), 2.0))
    raise RuntimeError(
        f"上游接口暂时不可用（已尝试 {len(urls)} 个节点）：{type(last).__name__}: {last}。"
        f"多为短时间访问过快被限流，请稍候片刻后重试。")


def _jsonp_or_json(text: str) -> dict:
    text = text.strip()
    m = re.match(r"^[^(]+\((.*)\)\s*;?$", text, re.S)
    if m:
        text = m.group(1)
    return json.loads(text)


# --------------------------------------------------------------------------- #
#  磁盘缓存：行情类按分钟失效，财报/持仓按天失效
# --------------------------------------------------------------------------- #
def _cache_path(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
    return os.path.join(CACHE_DIR, safe + ".json")


def cache_get(key: str, ttl: int, allow_stale: bool = False) -> Optional[object]:
    """
    ttl 内直接命中；超过 ttl 时，若 allow_stale=True 且接口不可用，
    仍返回旧数据（stale-while-error），避免上游抖动导致整个工具不可用。
    """
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    stale = time.time() - os.path.getmtime(path) > ttl
    if stale and not allow_stale:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:                                   # noqa: BLE001
        return None


def cache_put(key: str, value: object) -> None:
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
    except Exception:                                   # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  1. ETF 列表
# --------------------------------------------------------------------------- #
# 关键：东财把场内基金拆成多个板块，跨境 ETF（恒生科技/港股通/中概互联/纳指等）
# 不在 b:MK0021（境内股票 ETF）里，而在 b:MK0023（跨境 ETF，约 240 只）。
# 只拉 MK0021 的话，搜索框里就搜不到港股类 ETF，只能搜到场外 QDII 基金号码。
# 货币(MK0022)/商品(MK0024)无股票持仓，本工具用不上，不拉。
_ETF_LIST_KEY = "etf_list_v2"        # v2 = 换 fs 后弃用旧缓存，避免读到旧的纯境内列表
_ETF_BACKUP = "etf_list_backup_v2"


def fetch_etf_list(force: bool = False) -> List[dict]:
    """返回 [{code, name, price, pct, market_cap}]，共 1500+ 只场内 ETF（含跨境）。"""
    key = _ETF_LIST_KEY
    if not force:
        hit = cache_get(key, 600)
        if hit:
            return hit

    # 本地库优先（1 天内有效）
    if not force:
        hit = store.load_etf_list(86400)
        if hit:
            return hit

    try:
        out = _paginate()
        if out:
            out.sort(key=lambda x: -(x["market_cap"] or 0))
            store.save_etf_list(out)
            cache_put(_ETF_BACKUP, out)     # JSON 备用，供上游临时不可用时降级
            return out
    except Exception:                                   # noqa: BLE001
        pass

    # 上游不可用时，退回本地库（放宽到 10 天）或 JSON 备份（7 天）
    fallback = (store.load_etf_list(10 * 86400)
                or cache_get(_ETF_BACKUP, 7 * 86400))
    if fallback:
        return fallback
    raise RuntimeError("ETF 列表获取失败，且无可用缓存；上游接口可能暂时不可用，请稍后重试")


def _paginate() -> List[dict]:
    out: List[dict] = []
    page, size = 1, 100
    seen: set = set()
    while True:
        params = {
            "pn": str(page), "pz": str(size), "po": "0", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f12", "fs": "b:MK0021,b:MK0023",
            "fields": "f12,f14,f2,f3,f21",
        }
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
        data = _jsonp_or_json(http_get(url))["data"]
        if not data or not data.get("diff"):
            break
        for it in data["diff"]:
            if not it.get("f12"):
                continue
            if it["f12"] in seen:          # 板块间可能有少量重叠，按代码去重
                continue
            seen.add(it["f12"])
            out.append({
                "code": it["f12"],
                "name": it["f14"],
                "price": _num(it.get("f2")),
                "pct": _num(it.get("f3")),
                "market_cap": _num(it.get("f21")),
            })
        total = data.get("total", 0)
        if page * size >= total:
            break
        page += 1
    return out


# --------------------------------------------------------------------------- #
#  1b. 全市场基金（含场外，约 2.8 万只）
# --------------------------------------------------------------------------- #
_FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"


def fetch_all_funds(force: bool = False) -> List[dict]:
    """
    全市场基金名录：[{code, name, type}]，约 2.8 万只（含场外开放式基金）。
    字段顺序：[代码, 拼音缩写, 名称, 类型, 拼音全称]
    """
    key = "fund_list"
    if not force:
        hit = cache_get(key, 7 * 86400)
        if hit:
            return hit
    try:
        # 该文件约 2.3 MB，超时给足
        js = http_get(_FUND_LIST_URL, {"Referer": "https://fund.eastmoney.com/"},
                      timeout=90, retries=4)
        m = re.search(r"var\s+r\s*=\s*(\[.*\])\s*;", js, re.S)
        if not m:
            raise RuntimeError("基金名录格式未识别")
        data = json.loads(m.group(1))
        out = [{"code": d[0], "name": d[2], "type": d[3]} for d in data if len(d) >= 4]
        if out:
            cache_put(key, out)
            return out
    except Exception:                                   # noqa: BLE001
        pass
    return cache_get(key, 0, allow_stale=True) or []


def lookup_fund(code: str) -> Optional[dict]:
    """按代码查基金名称与类型（先查全市场，再退回 ETF 列表）"""
    for f in fetch_all_funds():
        if f["code"] == code:
            return f
    for e in fetch_etf_list():
        if e["code"] == code:
            return {"code": code, "name": e["name"], "type": "ETF"}
    return None


def resolve_feeder_target(code: str, name: str = "") -> Optional[dict]:
    """
    联接基金识别与穿透。返回 dict 或 None：

    - 非联接基金 → None
    - 联接基金 → {"target": {code,name} 或 None, "tracking": 跟踪指数名 或 None}

    背景：ETF 联接基金（如 009068）本身不直接持有股票，而是拿募来的钱
    去买目标 ETF 份额。它的「股票投资明细」（fundf10 type=jjcc）只残留了
    2021 年之前过渡期的少量股票，2022 年起就不再披露——直接按 jjcc 分析
    会拿到五年前的旧持仓，算出完全失真的 PEG。

    穿透策略：先按名称匹配目标 ETF（场内 ETF 简称可能和联接基金全称不一致，
    匹配失败很常见）；匹配不到时退回「跟踪标的指数」作为提示，交由上层阻断
    并指引用户搜索对应的场内 ETF。
    """
    # 传入的是空名或纯代码时，回源查真实名称（CLI/测试里常只传 code）
    if not name or name == code or name.isdigit():
        f = lookup_fund(code)
        name = (f or {}).get("name") or name
    if "联接" not in name:
        return None
    # 目标 ETF 名称 = 名称在「联接」之前的部分（如
    # 「国泰中证新能源汽车ETF联接C」 → 「国泰中证新能源汽车ETF」）
    base = name.split("联接", 1)[0].strip()
    target: Optional[dict] = None
    if base:
        etfs = fetch_etf_list()
        for e in etfs:
            if e["name"] == base:
                target = {"code": e["code"], "name": e["name"]}
                break
        if target is None:
            # 场内 ETF 是简称（如「新能源车ETF国泰」），全称对不上时，
            # 用「去掉交易所前缀的指数核心词」做包含匹配（如「新能源汽车」→「新能源」）
            for e in etfs:
                if "联接" in e["name"]:
                    continue
                if base in e["name"] or e["name"] in base:
                    target = {"code": e["code"], "name": e["name"]}
                    break
    # 跟踪标的指数名：拿不到目标 ETF 时，用它指引用户（缓存 7 天，指数不会变）
    tracking = None
    if target is None:
        tracking = _feeder_tracking(code)
    return {"target": target, "tracking": tracking}


def _feeder_tracking(code: str) -> Optional[str]:
    """从基金概况页抓「跟踪标的」指数名（如 中证新能源汽车指数）。"""
    key = f"feeder_tracking_{code}"
    hit = cache_get(key, 7 * 86400)
    if hit is not None:
        return hit or None
    tracking = None
    try:
        html = http_get(f"https://fundf10.eastmoney.com/jbgk_{code}.html",
                        {"Referer": f"https://fund.eastmoney.com/{code}.html"},
                        timeout=20)
        m = re.search(r"跟踪标的</th>\s*<td[^>]*>([^<]+)</td>", html)
        if m:
            tracking = m.group(1).strip()
    except Exception:                                   # noqa: BLE001
        pass
    cache_put(key, tracking or "")
    return tracking


# --------------------------------------------------------------------------- #
#  2. 基金全部持仓（含权重）
# --------------------------------------------------------------------------- #
# 持仓行：同时抓「市场标识」与代码。
# 港股是 5 位（市场 116），美股 5 位（105/106/107），A 股 6 位（沪 1 / 深 0）。
# 只按 6 位匹配会漏掉港股，导致重仓港股的基金权重合计明显偏低。
# 注意 td 的 class 不能写死：境内基金用 tol/tor，QDII 基金用 toc
# （QDII 页的名称列还带 style 属性），统一用 [^>]* 兼容两种版式，
# 否则 QDII 基金（如 023034）一行都匹配不到，表现为"拿不到持仓"。
_ROW_RE = re.compile(
    r"<tr><td>\d+</td>"
    r"<td(?:\s+class='[^']*')?><a[^>]*?/r/(\d+)\.(\d+)'[^>]*>([^<]*)</a></td>"
    r"<td[^>]*><a[^>]*>([^<]+)</a></td>.*?"
    r"<td[^>]*>\s*([\d.]+)%\s*</td>"
    r"<td[^>]*>([\d,.-]+)</td>"
    r"<td[^>]*>([\d,.-]+)</td></tr>",
    re.S,
)
_SECTION_RE = re.compile(r"(\d{4})年(\d)季度股票投资明细")
_Q_END_DAY = {1: 31, 2: 30, 3: 30, 4: 31}

# 东财市场标识
_MKT_SH, _MKT_SZ = "1", "0"
_MKT_HK = "116"
_MKT_US = {"105", "106", "107"}
_A_MARKETS = {_MKT_SH, _MKT_SZ}


def market_name(market: Optional[str]) -> str:
    m = str(market)
    if m in _A_MARKETS:
        return "A股"
    if m == _MKT_HK:
        return "港股"
    if m in _MKT_US:
        return "美股"
    return "其他"


def is_a_share(market: Optional[str] = None, code: Optional[str] = None) -> bool:
    """
    只有 A 股在东财估值库里有 PE_TTM，可做 PE / PEG 计算。
    market 缺失时（升级前的老数据）按代码位数推断：A 股 6 位，港股/美股 5 位。
    """
    if market is not None and str(market) != "None":
        return str(market) in _A_MARKETS
    return bool(code) and len(str(code)) == 6


def _html(url: str, etf_code: str) -> str:
    return http_get(url, {"Referer": f"https://fundf10.eastmoney.com/ccmx_{etf_code}.html"})


def _split_sections(html: str) -> List[Tuple[Optional[str], str]]:
    """按「YYYY年Q季度股票投资明细」切块，返回 [(报告期YYYY-MM-DD, 片段)]"""
    parts = _SECTION_RE.split(html)
    out: List[Tuple[Optional[str], str]] = []
    if parts and parts[0].strip():
        out.append((None, parts[0]))
    for i in range(1, len(parts) - 2, 3):
        y, q = int(parts[i]), int(parts[i + 1])
        out.append((f"{y}-{q * 3:02d}-{_Q_END_DAY[q]:02d}", parts[i + 2]))
    return out


def _parse_rows(frag: str) -> List[dict]:
    stocks, seen = [], set()
    for market, code, _disp, name, weight, shares, mv in _ROW_RE.findall(frag):
        if code in seen:
            continue
        seen.add(code)
        stocks.append({
            "code": code,
            "name": name.strip(),
            "market": market,
            "weight": _num(weight),
            "shares": _num(shares.replace(",", "")),
            "market_value": _num(mv.replace(",", "")),   # 单位：万元
        })
    return stocks


def _is_legacy_holding(hold: Optional[dict]) -> bool:
    """升级前存的持仓没有市场标识，无法区分 A 股/港股，需要重抓一次"""
    return bool(hold and hold.get("stocks")) and \
        not any(s.get("market") for s in hold["stocks"])


def _probe_due(etf_code: str, report_date: str) -> bool:
    """
    是否需要回源查「有没有更新的报告期」。
    报告是季度发布的，离上一期报告日不足 95 天时不值得回源；
    就算要回源，两次之间也至少间隔 12 小时。
    """
    last_at, _ = store.get_probe(etf_code)
    if time.time() - last_at < store.PROBE_MIN_GAP:
        return False
    try:
        y, m, d = (int(x) for x in report_date.split("-"))
        return (date.today() - date(y, m, d)).days > store.NEXT_REPORT_DAYS
    except Exception:                                   # noqa: BLE001
        return True


def fetch_etf_holdings(etf_code: str, force: bool = False) -> dict:
    """
    返回 {report_date, full: bool, stocks:[{code,name,weight,shares,market_value}]}

    持久化策略：某只 ETF 某期报告的持仓是既成事实、不会再变，
    因此成功抓取后写入 SQLite 永久保存；之后只有在「可能出了新报告」
    时才回源探活（约每季度一次），其余时间直接读本地库，零网络请求。

    重要：监管要求基金**季报只披露前十大重仓**，半年报/年报才披露全部持仓。
    因此这里优先抓取最近一期的半年报或年报，只有实在拿不到时才退回季报前十大，
    并用 full=False 标记，交由上层给出「样本不完整」的警示。
    """
    # 1) 本地库命中且没必要探活 → 直接返回，不走网络
    if not force:
        rep = store.latest_holding_report(etf_code)
        if rep and not _probe_due(etf_code, rep):
            cached = store.load_holdings(etf_code, rep)
            if cached and not _is_legacy_holding(cached):
                return cached

    # 2) 回源
    result = _fetch_holdings_upstream(etf_code)
    if result.get("report_date") and result.get("stocks"):
        store.save_holdings(etf_code, result["report_date"],
                            bool(result.get("full")), result["stocks"])
        store.set_probe(etf_code, result["report_date"])

    # 3) 上游失败时退回本地已有数据，不要让整批扫描失败
    if not result.get("stocks"):
        rep = store.latest_holding_report(etf_code)
        if rep:
            cached = store.load_holdings(etf_code, rep)
            if cached:
                return cached
    return result


def fetch_holdings_period(etf_code: str, year: int, month: int) -> Optional[dict]:
    """
    抓取指定年月的定期报告持仓（用于历史回填）。
    month 只能是 6（半年报）或 12（年报）—— 只有这两类披露全持仓。
    已入库的期直接读本地（历史报告是既成事实，没必要每次回源）。
    """
    # (y, 6) → 06-30 半年报；(y, 12) → 12-31 年报。命中即返回，不打网络
    want = f"{year}-{month:02d}-{30 if month == 6 else 31}"
    cached = store.load_holdings(etf_code, want)
    if cached and cached.get("stocks"):
        return cached

    url = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
           f"?type=jjcc&code={etf_code}&topline=3000&year={year}&month={month}&rt=0.1")
    try:
        html = _html(url, etf_code)
    except Exception:                                   # noqa: BLE001
        return None
    best = None
    for report_date, frag in _split_sections(html):
        stocks = _parse_rows(frag)
        if not stocks:
            continue
        cand = {"report_date": report_date, "full": len(stocks) > 10, "stocks": stocks}
        if best is None or (cand["full"] and not best["full"]) or \
           (cand["full"] == best["full"] and len(stocks) > len(best["stocks"])):
            best = cand
    if best and best.get("stocks") and best.get("report_date"):
        store.save_holdings(etf_code, best["report_date"], best["full"], best["stocks"])
        return best
    return None


def _latest_pe_trade_date() -> Optional[str]:
    """探测东财估值库的最新交易日"""
    params = {"reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
              "pageNumber": "1", "pageSize": "1",
              "filter": '(SECURITY_CODE="000001")',
              "sortColumns": "TRADE_DATE", "sortTypes": "-1",
              "source": "WEB", "client": "WEB"}
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
    try:
        res = (_jsonp_or_json(http_get(url, timeout=30)).get("result") or {})
        data = res.get("data") or []
        return (data[0].get("TRADE_DATE") or "")[:10] or None
    except Exception:                                   # noqa: BLE001
        return None


_MARKET_PE_MEMO = {"date": None, "ts": 0.0}


def ensure_market_pe(max_lag_days: int = 2, force: bool = False) -> Optional[str]:
    """
    确保本地库里有最近交易日的「全市场 PE_TTM」，返回该日期。

    为什么用它而不是实时行情的「市盈率(动态)」：
    动态 PE = 市值 /（最新报告期净利润 × 年化系数），只存在于当下，没有历史序列；
    历史可得的只有 PE_TTM。要让历史曲线和当前值可比，两边必须同一个口径。
    """
    now = time.time()
    if not force and _MARKET_PE_MEMO["date"] and now - _MARKET_PE_MEMO["ts"] < 3600:
        return _MARKET_PE_MEMO["date"]

    d = store.latest_market_pe_date()
    if d and not force:
        try:
            y, m, dd = (int(x) for x in d.split("-"))
            if (date.today() - date(y, m, dd)).days <= max_lag_days:
                _MARKET_PE_MEMO.update(date=d, ts=now)
                return d
        except Exception:                               # noqa: BLE001
            pass

    target = _latest_pe_trade_date()
    if not target:
        return None

    out: List[dict] = []
    page = 1
    while page <= 40:
        params = {"reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
                  "pageNumber": str(page), "pageSize": "500",
                  "filter": f"(TRADE_DATE='{target}')",
                  "sortColumns": "SECURITY_CODE", "sortTypes": "1",
                  "source": "WEB", "client": "WEB"}
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
        res = (_jsonp_or_json(http_get(url, timeout=40)).get("result") or {})
        data = res.get("data") or []
        if not data:
            break
        for it in data:
            out.append({"date": target, "code": it.get("SECURITY_CODE"),
                        "pe_ttm": _num(it.get("PE_TTM")),
                        "pe_lar": _num(it.get("PE_LAR")),
                        "mktcap": _num(it.get("TOTAL_MARKET_CAP")),
                        "close": _num(it.get("CLOSE_PRICE")),
                        "pb": _num(it.get("PB_MRQ")),
                        "ps": _num(it.get("PS_TTM")),
                        "pcf": _num(it.get("PCF_OCF_TTM"))})
        if page * 500 >= int(res.get("count") or 0):
            break
        page += 1

    if out:
        store.save_stock_pe(out)
        _MARKET_PE_MEMO.update(date=target, ts=now)
        return target
    return None


def fetch_stock_pe(code: str, start_date: str, force: bool = False) -> List[dict]:
    """
    单只股票的每日估值历史（PE_TTM / 总市值 / 收盘价）。
    一次请求最多可返回 1000 个交易日；数据为历史既成事实，入库后不再重下。
    """
    # 判断依据是「已覆盖到 start_date 之前」，而不是「这只股票有没有数据」：
    # 否则先回填 120 天、再想回填 250 天时会被直接跳过，历史就断在 120 天处。
    if not force:
        lo, _ = store.stock_pe_range(code)
        if lo and lo <= start_date:
            return []                                   # 已覆盖该区间
        # 晚上市的股票最早 PE 天然晚于请求起点（数据没断，是它当时还没上市），
        # 只看日期范围会永远重拉。用水位判断：这个起点已经问过上游，
        # 上游返回的就是它的全部历史。
        wm = store.get_pe_fetch_log(code)
        if wm and wm <= start_date:
            return []

    # 需分页：上游一次最多 1000 条，而全历史约 2100 个交易日
    out: List[dict] = []
    page, size, total = 1, 1000, None
    while page <= 10:
        params = {
            "reportName": "RPT_VALUEANALYSIS_DET", "columns": "ALL",
            "pageNumber": str(page), "pageSize": str(size),
            "filter": f'(SECURITY_CODE="{code}")(TRADE_DATE>=\'{start_date}\')',
            "sortColumns": "TRADE_DATE", "sortTypes": "1",
            "source": "WEB", "client": "WEB",
        }
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
        res = (_jsonp_or_json(http_get(url, timeout=40)).get("result") or {})
        data = res.get("data") or []
        if not data:
            break
        if total is None:
            total = int(res.get("count") or 0)
        for it in data:
            d = (it.get("TRADE_DATE") or "")[:10]
            if not d:
                continue
            out.append({"date": d, "code": code,
                        "pe_ttm": _num(it.get("PE_TTM")),
                        "pe_lar": _num(it.get("PE_LAR")),
                        "mktcap": _num(it.get("TOTAL_MARKET_CAP")),
                        "close": _num(it.get("CLOSE_PRICE")),
                        "pb": _num(it.get("PB_MRQ")),
                        "ps": _num(it.get("PS_TTM")),
                        "pcf": _num(it.get("PCF_OCF_TTM"))})
        if len(out) >= total:
            break
        page += 1
    if out:
        store.save_stock_pe(out)
    store.set_pe_fetch_log(code, start_date)
    return out


# 个股每日 PE 的上游最早日期（实测：起始日填更早也只返回 2109 条，最早 2018-01-02）
PE_HISTORY_EARLIEST = "2018-01-02"


def pe_history_earliest() -> str:
    return PE_HISTORY_EARLIEST


def all_market_codes() -> List[str]:
    """
    全市场股票代码清单（东财估值库口径，约 5500 只 A 股；该库不含港股）。

    取「最新一个全市场快照日」的代码，而不是去重全表：
    全表里混着历次回填拉的单只成分股，数量每天都在变，
    而快照日那份就是上游当天的权威清单，还能顺带保证清单是最新的。
    """
    d = ensure_market_pe()
    if not d:
        d = store.latest_market_pe_date()
    if not d:
        return []
    codes = store.codes_on(d)
    if len(codes) >= 3000:
        return codes
    return sorted(store.known_pe_codes())          # 兜底：快照不完整时用全表去重


def init_all_stock_pe(start_date: Optional[str] = None,
                      on_progress=None, cancel=None) -> dict:
    """
    初始化：把全市场每只股票的每日 PE/市值/收盘价全部拉进本地库。

    做完之后任何 ETF 的回填都不再需要回源拉 PE —— 全区间首次回填
    从约 37 分钟降到约 5 分钟，且所有 ETF 共享这一份数据。

    支持断点续传：已拉过的（日期覆盖或水位）自动跳过，中断后重跑继续。
    on_progress(done, total, label)
    """
    start = start_date or PE_HISTORY_EARLIEST
    prog = on_progress or (lambda *a: None)

    prog(0, 0, "取全市场代码清单…")
    codes = all_market_codes()
    if not codes:
        raise RuntimeError("取不到全市场股票代码清单，请检查网络后重试")

    todo = store.pe_pending(codes, start)
    total, done0 = len(codes), len(codes) - len(todo)
    failed: List[str] = []
    for i, c in enumerate(todo, 1):
        if cancel and cancel():
            break
        prog(done0 + i - 1, total, f"拉取 {c} 的历史 PE（{done0 + i}/{total}）")
        try:
            fetch_stock_pe(c, start)
        except Exception:                           # noqa: BLE001
            failed.append(c)                        # 单只失败不影响整体，下次重跑会补
    prog(total, total, "完成")
    return {"total": total, "todo": len(todo),
            "newly_done": len(todo) - len(failed),
            "failed": len(failed),
            "coverage": store.pe_coverage(start)}


def init_all_stock_valuation(on_progress=None, cancel=None) -> dict:
    """
    估值重初始化：把存量 stock_pe 里缺 PB/PS/PCF 列的老数据重拉补齐。

    背景：一期之前入库的历史估值只有 PE/市值/收盘价，PB_MRQ / PS_TTM / PCF
    是一期起才一并采集的。老数据靠本任务一次性升级（UPSERT 只补缺列，不覆盖）。

    判定与续传：store.pb_pending 找出「有数据但 pb 为 NULL」的代码，
    逐只全历史重拉（force=True 绕过水位判断）；拉完一只就少一只，
    中断后重跑自动从断点继续。全市场约 5500 只、单只 2~3 页请求，
    限流下整体约 1.5~2 小时。on_progress(done, total, label)
    """
    prog = on_progress or (lambda *a: None)

    prog(0, 0, "取全市场代码清单…")
    codes = all_market_codes()
    if not codes:
        raise RuntimeError("取不到全市场股票代码清单，请检查网络后重试")

    todo = store.pb_pending(codes)
    total = len(codes)
    done0 = total - len(todo)
    failed: List[str] = []
    for i, c in enumerate(todo, 1):
        if cancel and cancel():
            break
        prog(done0 + i - 1, total, f"重拉 {c} 的历史估值 PB/PS（{done0 + i}/{total}）")
        try:
            fetch_stock_pe(c, PE_HISTORY_EARLIEST, force=True)
        except Exception:                           # noqa: BLE001
            failed.append(c)
    prog(total, total, "完成")
    return {"total": total, "todo": len(todo),
            "newly_done": len(todo) - len(failed),
            "failed": len(failed)}


# --------------------------------------------------------------------------- #
#  个股历史分红（股息率的数据源）
# --------------------------------------------------------------------------- #
# 东财估值库（PE/PB/PS 的来源）没有股息率列，历史股息率只能从分红明细反推：
# 个股某日股息率 = 近 12 个月累计每股现金分红 ÷ 当日股价。分红明细来自
# RPT_SHAREBONUS_DET，只取「已实施、除息日非空」的记录；每 10 股派息金额
# 从方案描述 IMPL_PLAN_PROFILE（如 "10派2.332元(含税,扣税后2.0988元)"）解析。
_DPS_RE = re.compile(r"派\s*([\d.]+)\s*元")


def _parse_dps(plan: str) -> Optional[float]:
    """从分红方案描述解析每 10 股派息（元），无现金分红返回 None"""
    if not plan:
        return None
    m = _DPS_RE.search(plan)
    if not m:
        return None
    try:
        return float(m.group(1)) / 10.0
    except (TypeError, ValueError):
        return None


def fetch_stock_div(code: str) -> List[dict]:
    """
    单只 A 股的历次现金分红，返回 [{code, ex_date, dps, report_date, plan}]。

    dps = 每股派息（元）。历史分红是既成事实，成功入库后不再重下；
    从未分红的股票接口返回空，但仍记水位（div_fetch_log），避免每次重拉。
    """
    out: List[dict] = []
    page = 1
    while page <= 10:
        params = {
            "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
            "pageNumber": str(page), "pageSize": "500",
            "filter": f'(SECURITY_CODE="{code}")',
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "1",
            "source": "WEB", "client": "WEB",
        }
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
        res = (_jsonp_or_json(http_get(url, timeout=40)).get("result") or {})
        data = res.get("data") or []
        if not data:
            break
        for it in data:
            ex = (it.get("EX_DIVIDEND_DATE") or "")[:10]
            if not ex:
                continue                        # 预案/未实施：除息日为空，不能算进 TTM
            plan = it.get("IMPL_PLAN_PROFILE") or ""
            dps = _parse_dps(plan)
            if dps is None or dps <= 0:
                continue                        # 纯送/转增，无现金分红
            out.append({"code": code, "ex_date": ex, "dps": dps,
                        "report_date": (it.get("REPORT_DATE") or "")[:10],
                        "plan": plan})
        if len(data) < 500:
            break
        page += 1
    if out:
        store.save_stock_div(out)
    store.set_div_fetch_log(code)
    return out


def init_all_stock_div(on_progress=None, cancel=None) -> dict:
    """
    初始化：把全市场 A 股（估值库快照日清单）的历次现金分红拉进 stock_div。

    完成后任意 ETF 的历史股息率回填都不再回源拉分红；单只失败不影响整体，
    中断后重跑自动续传（div_fetch_log 水位）。约 5500 只 × 1 次请求/只，
    限流 ~9 req/s 下整体约 10~20 分钟。on_progress(done, total, label)
    """
    prog = on_progress or (lambda *a: None)

    prog(0, 0, "取全市场代码清单…")
    codes = all_market_codes()
    if not codes:
        raise RuntimeError("取不到全市场股票代码清单，请检查网络后重试")

    todo = store.div_pending(codes)
    total = len(codes)
    done0 = total - len(todo)
    failed: List[str] = []
    for i, c in enumerate(todo, 1):
        if cancel and cancel():
            break
        prog(done0 + i - 1, total, f"拉取 {c} 的分红历史（{done0 + i}/{total}）")
        try:
            fetch_stock_div(c)
        except Exception:                           # noqa: BLE001
            failed.append(c)                        # 单只失败不影响整体，下次重跑会补
    prog(total, total, "完成")
    return {"total": total, "todo": len(todo),
            "newly_done": len(todo) - len(failed),
            "failed": len(failed),
            "coverage": store.div_coverage()}


def _fetch_holdings_upstream(etf_code: str) -> dict:
    base = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            f"?type=jjcc&code={etf_code}&topline=3000&year={{y}}&month={{m}}&rt=0.1")
    today = date.today()

    # 候选：今年半年报 → 去年年报 → 去年半年报 → 前年年报（只有半年报/年报有全持仓）
    # 跳过尚未到来的报告期，否则白花一次请求（每只 ETF 都要多等一个 RTT）
    tries: List[Tuple[object, object]] = []
    for y in (today.year, today.year - 1, today.year - 2):
        for m in (6, 12):
            if date(y, m, 1) > today:
                continue
            tries.append((y, m))
    tries.append(("", ""))                    # 兜底：默认返回最近一期（可能只有前十大）

    best: Optional[dict] = None
    for y, m in tries:
        if best and best["full"]:
            break
        try:
            html = _html(base.format(y=y, m=m), etf_code)
        except Exception:                      # noqa: BLE001
            continue
        for report_date, frag in _split_sections(html):
            stocks = _parse_rows(frag)
            if not stocks:
                continue
            is_full = len(stocks) > 10
            cand = {"report_date": report_date, "full": is_full, "stocks": stocks}
            if best is None or (cand["full"] and not best["full"]) or \
               (cand["full"] == best["full"] and len(stocks) > len(best["stocks"])):
                best = cand

    if best is None:
        best = {"report_date": None, "full": False, "stocks": []}

    best["fetched_at"] = _now()
    return best


# --------------------------------------------------------------------------- #
#  3. 个股实时行情（价格 / 动态PE / 总市值 / 流通市值）
# --------------------------------------------------------------------------- #
_MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _num(v):
    if v is None or v == "-" or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_realtime_quotes(force: bool = False) -> Dict[str, dict]:
    """
    返回 {代码: {name, price, pct, pe_ttm, mktcap, float_mktcap}}
    f9  = 市盈率(动态)（东财口径即 TTM）
    f20 = 总市值, f21 = 流通市值
    """
    key = "quotes"
    if not force:
        hit = cache_get(key, 120)
        if hit:
            return hit

    try:
        quotes = _fetch_quotes()
    except Exception:                                   # noqa: BLE001
        stale = cache_get(key, 0, allow_stale=True)
        if stale:
            return stale
        raise

    cache_put(key, quotes)
    return quotes


def _fetch_quotes() -> Dict[str, dict]:
    quotes: Dict[str, dict] = {}

    def one_page(page: int) -> Optional[dict]:
        params = {
            "pn": str(page), "pz": "100", "po": "0", "np": "1",
            "fltt": "2", "invt": "2", "fid": "f12", "fs": _MARKET_FS,
            "fields": "f12,f14,f2,f3,f9,f20,f21",
        }
        url = "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(params)
        return _jsonp_or_json(http_get(url, timeout=40))["data"]

    first = one_page(1)
    if not first:
        raise RuntimeError("行情接口无返回")
    total = first.get("total", 0)
    pages = (total + 99) // 100

    def collect(data: Optional[dict]):
        if not data or not data.get("diff"):
            return
        for it in data["diff"]:
            if not it.get("f12"):
                continue
            quotes[it["f12"]] = {
                "name": it.get("f14"),
                "price": _num(it.get("f2")),
                "pct": _num(it.get("f3")),
                "pe_ttm": _num(it.get("f9")),
                "mktcap": _num(it.get("f20")),
                "float_mktcap": _num(it.get("f21")),
            }

    collect(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for data in pool.map(one_page, range(2, pages + 1)):
                collect(data)
    return quotes


# --------------------------------------------------------------------------- #
#  4. 全市场业绩报表（归母净利润 / 同比增速）
# --------------------------------------------------------------------------- #
_Q_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_Q_DEADLINE = {1: (4, 30), 2: (8, 31), 3: (10, 31), 4: (4, 30)}   # 年报为次年 4/30


def candidate_periods(today: Optional[date] = None,
                      years_back: int = 10) -> List[Tuple[int, int]]:
    """
    返回「已过法定披露截止日」的报告期 (year, quarter)，由新到旧。
    一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30。
    默认回看 10 年：长区间回填（历史 PEG）需要 2018 年起的基准期。
    """
    today = today or date.today()
    out: List[Tuple[int, int]] = []
    for y in range(today.year, today.year - years_back - 1, -1):
        for q in (4, 3, 2, 1):
            m, d = _Q_END[q]
            if date(y, m, d) > today:
                continue
            dm, dd = _Q_DEADLINE[q]
            if today >= date(y + 1 if q == 4 else y, dm, dd):
                out.append((y, q))
    return out


def period_key(yq: Tuple[int, int]) -> str:
    y, q = yq
    m, d = _Q_END[q]
    return f"{y}-{m:02d}-{d:02d}"


def period_deadline(period: str) -> date:
    """该报告期的法定披露截止日"""
    y, m, _ = period.split("-")
    y, q = int(y), {3: 1, 6: 2, 9: 3, 12: 4}[int(m)]
    dm, dd = _Q_DEADLINE[q]
    return date(y + 1 if q == 4 else y, dm, dd)


def is_settled(period: str, grace_days: int = 7) -> bool:
    """
    过了披露截止日 + 宽限期，即认为该期财报已定稿。
    定稿的历史财报不会再变化，可永久存本地、永不重新下载。
    """
    return date.today() > period_deadline(period) + timedelta(days=grace_days)


def latest_report_periods(today: Optional[date] = None, limit: int = 10) -> List[str]:
    return [period_key(yq) for yq in candidate_periods(today)][:limit]


def ttm_required_periods(latest: Optional[str] = None) -> List[str]:
    """
    构造 TTM 归母净利润所需的报告期（由新到旧），共 5 期：
      本期累计 · 去年同期累计 · 上年年报 · 前年同期累计 · 前年年报
    扫描时只拉这 5 期即可，拉全部 10 期会白白多花一倍时间。
    """
    if latest is None:
        cands = latest_report_periods(limit=1)
        if not cands:
            return []
        latest = cands[0]
    y, m, _ = latest.split("-")
    y, q = int(y), {3: 1, 6: 2, 9: 3, 12: 4}[int(m)]
    return [period_key(k) for k in
            ((y, q), (y - 1, q), (y - 1, 4), (y - 2, q), (y - 2, 4))]


def fetch_financials(period: str, force: bool = False,
                     on_progress=None) -> Dict[str, dict]:
    """
    返回 {代码: {name, parent_np, np_yoy, revenue, rev_yoy, eps, roe}}
    parent_np 为"归属母公司净利润"（元），np_yoy 为其同比增长率(%)。
    on_progress(done_pages, total_pages) —— 用于让前端持续看到进展。

    持久化策略：已过披露截止日的报告期永远不会再变，写入 SQLite 后不再回源；
    仍在披露窗口内的最新一期，每 6 小时刷新一次（公司披露是陆续进行的）。
    """
    if not force:
        # 定稿的历史财报 → 永不过期；披露中的最新一期 → 6 小时内有效
        hit = store.load_fin(period, None if is_settled(period) else ACTIVE_FIN_TTL)
        # 缺真实披露日的老数据需要重拉一次（PIT 对齐依赖该字段）
        if hit and store.fin_has_notice(period):
            return hit

    result: Dict[str, dict] = {}
    size = 500

    def one(p: int) -> Optional[dict]:
        params = {
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "pageNumber": str(p), "pageSize": str(size),
            "filter": f"(REPORTDATE='{period}')",
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            "source": "WEB", "client": "WEB",
        }
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get?" + urllib.parse.urlencode(params)
        return (_jsonp_or_json(http_get(url, timeout=40)).get("result") or {})

    first = one(1)
    if not first or not first.get("data"):
        result = store.load_fin(period, None) or {}
        return result

    def collect(res: Optional[dict]):
        if not res or not res.get("data"):
            return
        for it in res["data"]:
            code = it.get("SECURITY_CODE")
            if not code:
                continue
            result[code] = {
                "name": it.get("SECURITY_NAME_ABBR"),
                "parent_np": _num(it.get("PARENT_NETPROFIT")),
                "np_yoy": _num(it.get("SJLTZ")),
                "revenue": _num(it.get("TOTAL_OPERATE_INCOME")),
                "rev_yoy": _num(it.get("YSTZ")),
                "eps": _num(it.get("EPSJB") or it.get("BASIC_EPS")),
                "roe": _num(it.get("WEIGHTAVG_ROE")),
                "report_date": (it.get("REPORTDATE") or "")[:10],
                # 该公司这份报告的「真实公告日期」——各公司早晚不同，
                # 有了它才能做 PIT（point-in-time）对齐，避免前视偏差
                "notice_date": (it.get("NOTICE_DATE") or "")[:10] or None,
            }

    collect(first)
    pages = int(first.get("pages") or 1)
    if on_progress:
        on_progress(1, pages)
    if pages > 1:
        n = 1
        with ThreadPoolExecutor(max_workers=6) as pool:
            for res in pool.map(one, range(2, pages + 1)):
                collect(res)
                n += 1
                if on_progress:
                    on_progress(n, pages)

    if result:
        store.save_fin(period, result, is_settled(period))
    return result


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
