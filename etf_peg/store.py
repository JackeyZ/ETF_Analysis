# -*- coding: utf-8 -*-
"""
本地持久层（SQLite）。

设计原则：**按数据的可变性分层存放**。

  永久保存（写一次，永不过期）
    fin       财报   —— 过了披露截止日 + 宽限期后，历史财报不会再变
    holding   持仓   —— 某只 ETF 某期定期报告的持仓是既成事实
    etf       ETF 名录

  定期探活（报告是季度发布的，没必要每次都回源）
    holding_probe  记录上次去上游查询「最新报告期」的时间

  每日追加（这才是真正的历史序列）
    etf_daily      ETF 每天的 PE / 增速 / PEG 快照，用于看趋势

  不入库（实时且高频变化）
    行情价格、PE-TTM —— 仍走 2 分钟 TTL 的内存/文件缓存

多市场：A 股与港股各用一个独立库文件（见 A / HK）。
  同一个 Store 类实例化两次，表结构完全相同，只有数据和「全市场快照阈值」不同。
  港股库只用市场级的表（stock_pe / fin / pe_fetch_log / fin_period），
  不建也不写 holding / etf_daily / etf —— 基金层面的数据统一放 A 股库，
  一只基金的 PEG 可能同时含 A 股与港股，拆开存反而要跨库 join。

向后兼容：模块级 `store.xxx(...)` 通过 PEP 562 `__getattr__` 自动委派到 A 股库，
因此几十个既有调用点一行都不用改。

依赖：仅标准库 sqlite3。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

DB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "etf_peg.db")
HK_DB_PATH = os.path.join(DB_DIR, "etf_peg_hk.db")

_WRITE_LOCK = threading.Lock()

# 持仓探活间隔：报告是季度发布的，没必要天天回源
PROBE_MIN_GAP = 12 * 3600
# 距报告期末多少天后，才值得去上游看看有没有更新的报告
NEXT_REPORT_DAYS = 95

# 东财市场标识（与 datasource 保持一致，这里不 import datasource 以避免循环依赖）
_A_MARKETS = {"1", "0"}
_HK_MARKETS = {"116"}
_US_MARKETS = {"105", "106", "107"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """
    一个市场一个实例。所有方法与原模块级函数同名同签名，只是多包了一层库选择。

    full_market_min_rows：判定「某天是全市场快照」的条数下限。
    A 股全市场约 5500 只，港股约 4600 条清单（含非股票）但实际入库更少，
    两边的阈值不能共用，否则港股永远认不出快照日。
    """

    def __init__(self, db_path: str, label: str, full_market_min_rows: int = 3000):
        self.db_path = db_path
        self.label = label
        self.full_market_min_rows = full_market_min_rows
        self._local = threading.local()
        self._ready = False

    # ------------------------------------------------------------------ #
    #  连接与建表
    # ------------------------------------------------------------------ #
    def _conn(self) -> sqlite3.Connection:
        """每个线程一个连接（SQLite 连接不能跨线程共享）。"""
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            # 默认缓存只有 2MB（cache_size=-2000），对动辄 1GB 的库来说，
            # 回填时批量读几十万行几乎全走磁盘。放大缓存并开 mmap 后
            # 同一段回溯读取能快几倍——读多写少的库吃这两个参数最明显。
            c.execute("PRAGMA cache_size=-32768")        # 32 MB
            c.execute("PRAGMA mmap_size=134217728")      # 128 MB
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = c
        return c

    def init(self) -> None:
        # 建表脚本每次调用都跑一遍代价不小（几乎每个方法都会调 init），
        # 建过一次就跳过；_WRITE_LOCK 保证多线程下只建一次。
        if self._ready:
            return
        with _WRITE_LOCK:
            if self._ready:
                return
            c = self._conn()
            c.executescript("""
            CREATE TABLE IF NOT EXISTS fin (
                period TEXT NOT NULL, code TEXT NOT NULL,
                name TEXT, parent_np REAL, np_yoy REAL, revenue REAL, rev_yoy REAL,
                eps REAL, roe REAL, report_date TEXT,
                currency TEXT, shares REAL,
                PRIMARY KEY (period, code)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_fin_period ON fin(period);

            CREATE TABLE IF NOT EXISTS fin_period (
                period TEXT PRIMARY KEY,
                rows INTEGER, settled INTEGER DEFAULT 0, fetched_at TEXT
            );

            CREATE TABLE IF NOT EXISTS holding (
                etf_code TEXT NOT NULL, report_date TEXT NOT NULL, stock_code TEXT NOT NULL,
                stock_name TEXT, weight REAL, shares REAL, market_value REAL,
                PRIMARY KEY (etf_code, report_date, stock_code)
            ) WITHOUT ROWID;
            -- 老库升级：补上市场标识（A股/港股/美股）
            CREATE TABLE IF NOT EXISTS _mkt_placeholder (x INTEGER);
            CREATE INDEX IF NOT EXISTS ix_hold_etf ON holding(etf_code, report_date);

            CREATE TABLE IF NOT EXISTS holding_meta (
                etf_code TEXT NOT NULL, report_date TEXT NOT NULL,
                full INTEGER, n_stocks INTEGER, fetched_at TEXT,
                PRIMARY KEY (etf_code, report_date)
            );

            CREATE TABLE IF NOT EXISTS holding_probe (
                etf_code TEXT PRIMARY KEY, last_probe_at REAL, latest_report TEXT
            );

            -- 老库升级：补上真实披露日字段
            CREATE TABLE IF NOT EXISTS etf (
                code TEXT PRIMARY KEY, name TEXT, market_cap REAL, updated_at REAL
            );

            -- 个股每日估值（PE_TTM / 总市值）—— 历史数据，永久保存
            CREATE TABLE IF NOT EXISTS stock_pe (
                trade_date TEXT NOT NULL, code TEXT NOT NULL,
                pe_ttm REAL, pe_lar REAL, mktcap REAL, close REAL,
                PRIMARY KEY (trade_date, code)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_pe_code ON stock_pe(code, trade_date);
            CREATE INDEX IF NOT EXISTS ix_pe_date ON stock_pe(trade_date);

            -- 交易日历：从 stock_pe 里抽出来的去重交易日。
        -- 直接对 stock_pe 做 SELECT DISTINCT 要扫上千万条索引（实测 1.6s），
        -- 而每次回填都要拿日历，所以单独存一份、只增量补新日期。
        CREATE TABLE IF NOT EXISTS trade_calendar (
            trade_date TEXT PRIMARY KEY
        );

        -- PE 抓取水位：记录「最早从哪天开始请求过」。
            -- 晚上市的股票最早 PE 天然晚于请求起点，靠日期范围判断会永远重拉；
            -- 用水位才能知道「这个起点已经问过上游，返回的就是全部」。
            CREATE TABLE IF NOT EXISTS pe_fetch_log (
                code TEXT PRIMARY KEY,
                earliest_requested TEXT
            );

            CREATE TABLE IF NOT EXISTS etf_daily (
                d TEXT NOT NULL, etf_code TEXT NOT NULL,
                name TEXT, pe REAL, g_ttm REAL, g_all REAL, peg REAL, coverage REAL,
                n_valid INTEGER, n_total INTEGER, report_date TEXT,
                PRIMARY KEY (d, etf_code)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_daily_etf ON etf_daily(etf_code, d);
            """)
            cols = {r[1] for r in c.execute("PRAGMA table_info(fin)")}
            if "notice_date" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN notice_date TEXT")
            # 港股：PE 要求市值与净利同币种，股本用于按报告期还原历史市值
            if "currency" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN currency TEXT")
            if "shares" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN shares REAL")
            if "market" not in {r[1] for r in c.execute("PRAGMA table_info(holding)")}:
                c.execute("ALTER TABLE holding ADD COLUMN market TEXT")
            c.execute("DROP TABLE IF EXISTS _mkt_placeholder")
            c.commit()
            self._ready = True

    # ------------------------------------------------------------------ #
    #  财报
    # ------------------------------------------------------------------ #
    def load_fin(self, period: str, max_age: Optional[float] = None) -> Optional[Dict[str, dict]]:
        """
        max_age=None  → 该期已定稿，只要库里有就直接返回，永不过期
        max_age=秒数  → 该期仍在披露中，超过该时长才回源刷新
        """
        self.init()
        c = self._conn()
        row = c.execute("SELECT rows, settled, fetched_at FROM fin_period WHERE period=?",
                        (period,)).fetchone()
        if not row:
            return None
        n, settled, fetched_at = row
        if not settled and max_age is not None:
            try:
                if time.time() - time.mktime(time.strptime(fetched_at, "%Y-%m-%d %H:%M:%S")) > max_age:
                    return None
            except Exception:                            # noqa: BLE001
                return None
        out: Dict[str, dict] = {}
        for code, name, pnp, yoy, rev, ryoy, eps, roe, rd, nd, cur, sh in c.execute(
                "SELECT code,name,parent_np,np_yoy,revenue,rev_yoy,eps,roe,report_date,"
                "notice_date,currency,shares "
                "FROM fin WHERE period=?", (period,)):
            out[code] = {"name": name, "parent_np": pnp, "np_yoy": yoy, "revenue": rev,
                         "rev_yoy": ryoy, "eps": eps, "roe": roe, "report_date": rd,
                         "notice_date": nd, "currency": cur, "shares": sh}
        return out if len(out) >= max(1, (n or 0) // 2) else None

    def fin_has_notice(self, period: str) -> bool:
        """该期是否已存有真实披露日。老数据缺这个字段，需要重新拉一次。"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? AND notice_date IS NOT NULL",
            (period,)).fetchone()
        return bool(row and row[0] > 0)

    def has_fin(self, period: str) -> bool:
        """该期是否已在库（用于判断要不要回源补拉，比 load_fin 便宜得多）"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? LIMIT 1", (period,)).fetchone()
        return bool(row and row[0] > 0)

    def load_fin_subset(self, period: str, codes: List[str]) -> Dict[str, dict]:
        """只取指定股票在某期的净利润 / 同比 / 披露日——算增速序列时没必要读全市场"""
        self.init()
        out: Dict[str, dict] = {}
        if not codes:
            return out
        B = 400
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for code, pnp, yoy, nd, cur, sh in self._conn().execute(
                    f"SELECT code,parent_np,np_yoy,notice_date,currency,shares FROM fin "
                    f"WHERE period=? AND code IN ({ph})", (period, *chunk)):
                out[code] = {"parent_np": pnp, "np_yoy": yoy, "notice_date": nd,
                             "currency": cur, "shares": sh}
        return out

    def fin_shares(self, code: str) -> List[Tuple[str, float]]:
        """
        (报告期, 已发行股本) 升序。

        港股用：历史市值 = 历史收盘价 × 当时股本。
        只拿当前股本会让有大额回购/增发的公司历史市值失真，
        财报里每期都带 ISSUED_COMMON_SHARES，按报告期做阶梯插值即可。
        """
        self.init()
        return [(r[0], r[1]) for r in self._conn().execute(
            "SELECT report_date, shares FROM fin WHERE code=? AND shares IS NOT NULL "
            "ORDER BY report_date", (code,))]

    def save_fin(self, period: str, data: Dict[str, dict], settled: bool) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute("DELETE FROM fin WHERE period=?", (period,))
            c.executemany(
                "INSERT OR REPLACE INTO fin VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(period, code, v.get("name"), v.get("parent_np"), v.get("np_yoy"),
                  v.get("revenue"), v.get("rev_yoy"), v.get("eps"), v.get("roe"),
                  v.get("report_date"), v.get("notice_date"),
                  v.get("currency"), v.get("shares")) for code, v in data.items()])
            c.execute("INSERT OR REPLACE INTO fin_period VALUES (?,?,?,?)",
                      (period, len(data), 1 if settled else 0, _now()))
            c.commit()

    # ------------------------------------------------------------------ #
    #  持仓
    # ------------------------------------------------------------------ #
    def latest_holding_report(self, etf_code: str) -> Optional[str]:
        self.init()
        row = self._conn().execute(
            "SELECT report_date FROM holding_meta WHERE etf_code=? "
            "ORDER BY report_date DESC LIMIT 1", (etf_code,)).fetchone()
        return row[0] if row else None

    def load_holdings(self, etf_code: str, report_date: str) -> Optional[dict]:
        self.init()
        c = self._conn()
        m = c.execute("SELECT full, n_stocks, fetched_at FROM holding_meta "
                      "WHERE etf_code=? AND report_date=?", (etf_code, report_date)).fetchone()
        if not m:
            return None
        rows = c.execute(
            "SELECT stock_code, stock_name, weight, shares, market_value, market FROM holding "
            "WHERE etf_code=? AND report_date=?", (etf_code, report_date)).fetchall()
        return {
            "report_date": report_date,
            "full": bool(m[0]),
            "stocks": [{"code": r[0], "name": r[1], "weight": r[2],
                        "shares": r[3], "market_value": r[4], "market": r[5]} for r in rows],
            "fetched_at": m[2],
        }

    def save_holdings(self, etf_code: str, report_date: str, full: bool,
                      stocks: List[dict]) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute("DELETE FROM holding WHERE etf_code=? AND report_date=?",
                      (etf_code, report_date))
            c.executemany(
                "INSERT OR REPLACE INTO holding VALUES (?,?,?,?,?,?,?,?)",
                [(etf_code, report_date, s["code"], s.get("name"), s.get("weight"),
                  s.get("shares"), s.get("market_value"), s.get("market")) for s in stocks])
            c.execute("INSERT OR REPLACE INTO holding_meta VALUES (?,?,?,?,?)",
                      (etf_code, report_date, 1 if full else 0, len(stocks), _now()))
            c.commit()

    def get_probe(self, etf_code: str) -> Tuple[float, Optional[str]]:
        self.init()
        row = self._conn().execute("SELECT last_probe_at, latest_report FROM holding_probe "
                                   "WHERE etf_code=?", (etf_code,)).fetchone()
        return (row[0], row[1]) if row else (0.0, None)

    def set_probe(self, etf_code: str, latest_report: Optional[str]) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute("INSERT OR REPLACE INTO holding_probe VALUES (?,?,?)",
                      (etf_code, time.time(), latest_report))
            c.commit()

    # ------------------------------------------------------------------ #
    #  ETF 名录
    # ------------------------------------------------------------------ #
    def load_etf_list(self, max_age: float = 86400) -> Optional[List[dict]]:
        self.init()
        rows = self._conn().execute(
            "SELECT code, name, market_cap, updated_at FROM etf "
            "ORDER BY COALESCE(market_cap, 0) DESC").fetchall()
        if not rows:
            return None
        if time.time() - (rows[0][3] or 0) > max_age:
            return None
        return [{"code": r[0], "name": r[1], "market_cap": r[2], "price": None, "pct": None}
                for r in rows]

    def save_etf_list(self, items: List[dict]) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            now = time.time()
            c.executemany("INSERT OR REPLACE INTO etf VALUES (?,?,?,?)",
                          [(i["code"], i.get("name"), i.get("market_cap"), now) for i in items])
            c.commit()

    # ------------------------------------------------------------------ #
    #  个股每日估值（历史 PE，永久保存）
    # ------------------------------------------------------------------ #
    def save_stock_pe(self, rows: List[dict]) -> None:
        self.init()
        with _WRITE_LOCK:
            self._conn().executemany(
                "INSERT OR REPLACE INTO stock_pe VALUES (?,?,?,?,?,?)",
                [(r["date"], r["code"], r.get("pe_ttm"), r.get("pe_lar"),
                  r.get("mktcap"), r.get("close")) for r in rows])
            self._conn().commit()

    def load_stock_pe(self, codes: List[str], start: str, end: str) -> Dict[str, Dict[str, tuple]]:
        """
        返回 {code: {date: (pe_ttm, mktcap, close)}}。
        用元组而非字典：长区间回填会载入约 200 万格，小字典的分配开销会成为耗时大头。
        """
        self.init()
        out: Dict[str, Dict[str, tuple]] = {c: {} for c in codes}
        if not codes:
            return out
        B = 400                                   # SQLite 变量上限保护
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for d, c, pe, mc, cl in self._conn().execute(
                    f"SELECT trade_date, code, pe_ttm, mktcap, close FROM stock_pe "
                    f"WHERE code IN ({ph}) AND trade_date>=? AND trade_date<=?",
                    (*chunk, start, end)):
                if c in out:
                    out[c][d] = (pe, mc, cl)
        return out

    def _sync_calendar(self) -> None:
        """
        把 stock_pe 里新出现的交易日补进日历表。

        只补「比日历最大日期更新」的部分：日常就是几条，
        只有建表后第一次会全表扫一遍（约 1.6s，一次性）。
        """
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            row = c.execute("SELECT COALESCE(MAX(trade_date), '') "
                            "FROM trade_calendar").fetchone()
            since = row[0] if row else ""
            cur = c.execute(
                "INSERT OR IGNORE INTO trade_calendar(trade_date) "
                "SELECT DISTINCT trade_date FROM stock_pe WHERE trade_date > ?",
                (since,))
            if cur.rowcount:
                c.commit()

    def pe_dates(self, start: str, end: str) -> List[str]:
        """已有 PE 数据的交易日列表（当作交易日历用）"""
        self.init()
        self._sync_calendar()
        return [r[0] for r in self._conn().execute(
            "SELECT trade_date FROM trade_calendar WHERE trade_date>=? AND trade_date<=? "
            "ORDER BY trade_date", (start, end))]

    def latest_market_pe_date(self) -> Optional[str]:
        """
        本地库里最近一个「全市场」PE 快照的日期。

        不能用固定的条数阈值：初始化进行中时，最近一天可能只写进了部分股票
        （比如今天已经 3800 只、昨天是完整的 5560 只），固定阈值会把半成品那天
        当成全市场快照，于是覆盖度的分母和代码清单都会跟着错。

        改成「最近 30 个交易日内，条数不低于其中最大值的 80% 的最近一天」——
        既避开半成品，也不需要为 A 股 / 港股分别配阈值。
        """
        self.init()
        since = (date.today() - timedelta(days=45)).isoformat()
        row = self._conn().execute(
            "WITH recent AS ("
            "  SELECT trade_date, COUNT(*) c FROM stock_pe "
            "  WHERE trade_date >= ? GROUP BY trade_date) "
            "SELECT trade_date FROM recent "
            "WHERE c >= (SELECT MAX(c) FROM recent) * 0.8 "
            "ORDER BY trade_date DESC LIMIT 1", (since,)).fetchone()
        return row[0] if row else None

    def known_pe_codes(self) -> set:
        self.init()
        return {r[0] for r in self._conn().execute("SELECT DISTINCT code FROM stock_pe")}

    def codes_on(self, trade_date: str) -> List[str]:
        """某个交易日有估值数据的全部代码（拿最新快照日即得全市场清单）"""
        self.init()
        return [r[0] for r in self._conn().execute(
            "SELECT DISTINCT code FROM stock_pe WHERE trade_date=? ORDER BY code",
            (trade_date,))]

    def pe_pending(self, codes: List[str], start: str) -> List[str]:
        """
        从 codes 里挑出**还需要回源拉取**的。

        两条免拉依据（与 fetch_stock_pe 的判断一致）：
          · 已有数据的最早日期 <= start  → 区间已覆盖
          · 抓取水位 <= start            → 这个起点问过上游，返回的已是全部
            （晚上市的股票最早 PE 天然晚于 start，只看日期会永远重拉）

        一次性批量算完：逐只查询在 5000+ 只的规模下会有上万次往返。
        """
        self.init()
        c = self._conn()
        ok: set = set()
        B = 500                                     # SQLite 变量上限保护
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for (x,) in c.execute(
                    f"SELECT code FROM stock_pe WHERE code IN ({ph}) "
                    f"GROUP BY code HAVING MIN(trade_date)<=?", (*chunk, start)):
                ok.add(x)
            for (x,) in c.execute(
                    f"SELECT code FROM pe_fetch_log WHERE code IN ({ph}) "
                    f"AND earliest_requested<=?", (*chunk, start)):
                ok.add(x)
        return [x for x in codes if x not in ok]

    def pe_coverage(self, start: str) -> dict:
        """
        初始化进度：全市场多少只、其中多少只已经拉到 start 之前。

        total 优先取最近一个「全市场快照日」的代码数；
        还没有快照时（港股初始化早期）退回「最新一期财报的代码数」——
        那正是初始化实际要拉的清单，用它当分母进度才不会一直显示 0。
        """
        self.init()
        c = self._conn()
        snap = self.latest_market_pe_date()
        total = 0
        if snap:
            total = c.execute("SELECT COUNT(DISTINCT code) FROM stock_pe WHERE trade_date=?",
                              (snap,)).fetchone()[0]
        else:
            row = c.execute("SELECT period FROM fin_period ORDER BY period DESC LIMIT 1").fetchone()
            if row:
                total = c.execute("SELECT COUNT(DISTINCT code) FROM fin WHERE period=?",
                                  (row[0],)).fetchone()[0]
        # done 只统计快照清单内的代码：全表里还有已退市的股票，
        # 把它们算进来会出现「1379/5560」这种分子大于分母的怪值。
        where = "WHERE code IN (SELECT code FROM stock_pe WHERE trade_date=?)" if snap else ""
        args = (start, start) + ((snap,) if snap else ())
        done = c.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT code FROM stock_pe GROUP BY code HAVING MIN(trade_date)<=? "
            "  UNION "
            "  SELECT code FROM pe_fetch_log WHERE earliest_requested<=? "
            f") {where}", args).fetchone()[0]
        return {"snapshot_date": snap, "total": total, "done": done,
                "rows": c.execute("SELECT COUNT(*) FROM stock_pe").fetchone()[0]}

    def stock_pe_range(self, code: str) -> Tuple[Optional[str], Optional[str]]:
        """该股票已有 PE 数据的日期区间"""
        self.init()
        row = self._conn().execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM stock_pe WHERE code=?", (code,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def get_pe_fetch_log(self, code: str) -> Optional[str]:
        self.init()
        row = self._conn().execute("SELECT earliest_requested FROM pe_fetch_log WHERE code=?",
                                   (code,)).fetchone()
        return row[0] if row else None

    def set_pe_fetch_log(self, code: str, start_date: str) -> None:
        """记录水位：只下压，不上抬（保留最早问过的起点）"""
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            cur = c.execute("SELECT earliest_requested FROM pe_fetch_log WHERE code=?",
                            (code,)).fetchone()
            if cur and cur[0] and cur[0] <= start_date:
                return
            c.execute("INSERT OR REPLACE INTO pe_fetch_log VALUES (?,?)", (code, start_date))
            c.commit()

    # ------------------------------------------------------------------ #
    #  每日 PEG 快照（真正的历史序列）
    # ------------------------------------------------------------------ #
    def record_daily(self, rows: List[dict], d: Optional[str] = None) -> None:
        """
        写入每日快照。行内带 date 字段时按该日期写（历史回填用），
        否则统一用 d / 今天（日常分析用）。
        """
        self.init()
        default_d = d or time.strftime("%Y-%m-%d")
        with _WRITE_LOCK:
            c = self._conn()
            c.executemany(
                "INSERT OR REPLACE INTO etf_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [(r.get("date") or default_d, r.get("code"), r.get("name"), r.get("pe"),
                  r.get("g_ttm"), r.get("g_all"), r.get("peg"), r.get("coverage"),
                  r.get("n_valid"), r.get("n_total"), r.get("report_date"))
                 for r in rows if r.get("code")])
            c.commit()

    def clear_daily(self, etf_code: Optional[str] = None) -> int:
        """清空每日快照（口径变更时用）；不传代码则清空全部。"""
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            if etf_code:
                c.execute("DELETE FROM etf_daily WHERE etf_code=?", (etf_code,))
            else:
                c.execute("DELETE FROM etf_daily")
            n = c.total_changes
            c.commit()
        return n

    def get_history(self, etf_code: str, days: int = 180) -> List[dict]:
        self.init()
        rows = self._conn().execute(
            "SELECT d, name, pe, g_ttm, g_all, peg, coverage, n_valid, n_total, report_date "
            "FROM etf_daily WHERE etf_code=? ORDER BY d DESC LIMIT ?", (etf_code, days)).fetchall()
        return [{"date": r[0], "name": r[1], "pe": r[2], "g_ttm": r[3], "g_all": r[4],
                 "peg": r[5], "coverage": r[6], "n_valid": r[7], "n_total": r[8],
                 "report_date": r[9]} for r in rows]

    # ------------------------------------------------------------------ #
    #  状态
    # ------------------------------------------------------------------ #
    def stats(self) -> dict:
        self.init()
        c = self._conn()
        q = lambda s, *a: c.execute(s, a).fetchone()[0]      # noqa: E731
        size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        daily_range = c.execute("SELECT MIN(d), MAX(d) FROM etf_daily").fetchone()
        return {
            "label": self.label,
            "db_path": self.db_path,
            "db_size_mb": round(size / 1048576, 2),
            "fin_periods": q("SELECT COUNT(*) FROM fin_period"),
            "fin_rows": q("SELECT COUNT(*) FROM fin"),
            "fin_settled": q("SELECT COUNT(*) FROM fin_period WHERE settled=1"),
            "fin_period_list": [r[0] for r in c.execute(
                "SELECT period FROM fin_period ORDER BY period DESC")],
            "holdings_etf": q("SELECT COUNT(DISTINCT etf_code) FROM holding_meta"),
            "holdings_reports": q("SELECT COUNT(*) FROM holding_meta"),
            "holdings_rows": q("SELECT COUNT(*) FROM holding"),
            "etf_list_rows": q("SELECT COUNT(*) FROM etf"),
            "daily_rows": q("SELECT COUNT(*) FROM etf_daily"),
            "daily_from": daily_range[0],
            "daily_to": daily_range[1],
            "stock_pe_rows": q("SELECT COUNT(*) FROM stock_pe"),
            "stock_pe_codes": q("SELECT COUNT(DISTINCT code) FROM stock_pe"),
            "stock_pe_range": c.execute(
                "SELECT MIN(trade_date), MAX(trade_date) FROM stock_pe").fetchone(),
        }


# --------------------------------------------------------------------------- #
#  两个市场各一个实例
# --------------------------------------------------------------------------- #
A = Store(DB_PATH, "A股", full_market_min_rows=3000)
HK = Store(HK_DB_PATH, "港股", full_market_min_rows=800)


def init() -> None:
    """初始化全部库。datasource 在 import 时会调一次。"""
    A.init()
    HK.init()


def market_of(market: Optional[str] = None, code: Optional[str] = None) -> Store:
    """
    按持仓条目的市场标识选择库。
    market 缺失时（升级前的老数据）按代码位数推断：A 股 6 位，港股 5 位。
    美股（105/106/107）落到 A 股库——那边查不到数据，效果等同于跳过。
    """
    if market is not None and str(market) != "None":
        m = str(market)
        if m in _HK_MARKETS:
            return HK
        if m in _A_MARKETS or m in _US_MARKETS:
            return A
    return HK if (code and len(str(code)) == 5) else A


def is_valuable(market: Optional[str] = None, code: Optional[str] = None) -> bool:
    """
    这只股票本工具能不能估值：A 股与港股可以，美股等其他市场目前没有数据源。

    覆盖率的分母要用它来排除「根本不可能参与计算」的持仓，
    否则含美股的基金会显示一个被系统性低估的覆盖率。
    """
    if market is not None and str(market) != "None":
        return str(market) in _A_MARKETS or str(market) in _HK_MARKETS
    if not code:
        return False
    s = str(code)
    return s.isdigit() and len(s) in (5, 6)


def stats_all() -> dict:
    """两个库的统计（A 股在前，保持与旧 stats() 的字段名一致）"""
    a, hk = A.stats(), HK.stats()
    return {**a, "hk": hk}


def __getattr__(name: str):
    """
    PEP 562：模块级属性找不到时自动委派到 A 股库。

    这样 store.load_fin(...) / store.get_history(...) 等几十个既有调用点
    一行都不用改，也就不存在 A 股路径的回归风险。
    """
    if name.startswith("__"):
        raise AttributeError(name)
    try:
        return getattr(A, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
