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
                -- 估值扩展列（主要港股用；A 股源接口没有这些字段，保持 NULL）
                bps REAL, equity REAL, dividend_rate REAL, dps REAL,
                total_liab REAL, end_cash REAL, op_profit REAL,
                -- 港股财报类型（年报/中报/一季报/三季报）与财年截止（如 12-31/03-31），
                -- 按公司财年构造 TTM 的依据；A 股源接口没有这两个字段，保持 NULL
                report_type TEXT, fiscal_year TEXT,
                -- 扣非归母净利润（元）：A 股来自利润表接口，港股无此概念保持 NULL
                deduct_np REAL,
                PRIMARY KEY (period, code)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_fin_period ON fin(period);

            -- v2=1 表示该期财报已按扩展列重拉过（用于触发旧数据一次性升级）
            CREATE TABLE IF NOT EXISTS fin_period (
                period TEXT PRIMARY KEY,
                rows INTEGER, settled INTEGER DEFAULT 0, fetched_at TEXT,
                v2 INTEGER DEFAULT 0
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
            -- pb/ps/pcf 与 PE 同源于东财估值库 RPT_VALUEANALYSIS_DET（一期起一并入库）
            CREATE TABLE IF NOT EXISTS stock_pe (
                trade_date TEXT NOT NULL, code TEXT NOT NULL,
                pe_ttm REAL, pe_lar REAL, mktcap REAL, close REAL,
                pb REAL, ps REAL, pcf REAL,
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

            -- 机构一致预期（盈利预测，仅 A 股；TTL 由调用方按 fetched_at 控制）
            CREATE TABLE IF NOT EXISTS cons_forecast (
                code TEXT NOT NULL, year INTEGER NOT NULL,
                mark TEXT, np REAL, np_ratio REAL, eps REAL, pe REAL,
                n_org INTEGER, fetched_at TEXT,
                PRIMARY KEY (code, year)
            ) WITHOUT ROWID;

            -- 中国 10 年期国债收益率（%）日序列（ERP 计算用）
            CREATE TABLE IF NOT EXISTS bond_yield (
                d TEXT PRIMARY KEY, y10 REAL
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS etf_daily (
                d TEXT NOT NULL, etf_code TEXT NOT NULL,
                name TEXT, pe REAL, g_ttm REAL, g_all REAL, peg REAL, coverage REAL,
                n_valid INTEGER, n_total INTEGER, report_date TEXT,
                -- 一期起：多指标与历史分位（历史序列由回填增量填充）
                pb REAL, ps REAL, div_yield REAL,
                pe_pct REAL, pb_pct REAL, ps_pct REAL,
                -- div_calc=1 表示该日股息率已按分红数据算过（纯成长股组合可能
                -- 合法地为 NULL，不能用 div_yield 是否非空当完成标志）
                div_calc INTEGER DEFAULT 0,
                -- TTM 扣非归母净利润合计（元）。港股不披露扣非，该部分用归母顶上，
                -- 与成分股表的 † 口径一致；与 np 用同一批样本（PE>0 且增速>0）
                deduct_ttm REAL,
                -- v9 起：归母净利合计（全口径加权，元）与扣非 PEG 三件套。
                -- np_ttm 与 deduct_ttm 完全对称；g_deduct 只在 >0 时 deduct_peg 有值
                np_ttm REAL,
                deduct_pe REAL,
                g_deduct REAL,
                deduct_peg REAL,
                -- v10：口径维度。fixed=最新持仓回溯（默认，柱子间可比），
                -- pit=每期用当时已知持仓（无前视，但含调仓噪音）
                basis TEXT NOT NULL DEFAULT 'pit',
                PRIMARY KEY (d, etf_code, basis)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_daily_etf ON etf_daily(etf_code, basis, d);

            -- 个股历史分红（每股派息，元）：除息日 + 派息额。既成事实，永久保存。
            CREATE TABLE IF NOT EXISTS stock_div (
                code TEXT NOT NULL, ex_date TEXT NOT NULL, dps REAL,
                report_date TEXT, plan TEXT,
                PRIMARY KEY (code, ex_date)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_div_code ON stock_div(code, ex_date);

            -- 分红拉取水位：该股已问过上游即记录，避免从未分红的股票每次重拉
            CREATE TABLE IF NOT EXISTS div_fetch_log (
                code TEXT PRIMARY KEY, requested_at TEXT
            );

            -- 计算口径版本：PE/PEG 等算法不兼容变更时 +1；
            -- 回填检测到该 ETF 版本落后会自动 force 重算全部历史。
            -- v10：按 basis（fixed/pit）分开记录，两套序列独立增量
            CREATE TABLE IF NOT EXISTS etf_calc_version (
                etf_code TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'pit',
                version INTEGER, updated_at TEXT,
                PRIMARY KEY (etf_code, basis)
            ) WITHOUT ROWID;

            -- 券商研报的盈利预测（每篇一行，全历史）。
            -- 与 cons_forecast（每家机构最新一版）互补：那张表只存当下，
            -- 这张表存时序，才能算「某家机构上次说多少、这次说多少」。
            -- year_1/2/3 是绝对财年：接口的「今年」随发布年漂移，
            -- 不换算成绝对年份就做不了跨年对比。
            CREATE TABLE IF NOT EXISTS report (
                info_code TEXT PRIMARY KEY,
                publish_date TEXT NOT NULL,
                stock_code TEXT NOT NULL, stock_name TEXT,
                org_code TEXT, org_name TEXT, researcher TEXT,
                rating TEXT, last_rating TEXT, rating_change INTEGER,
                target_price REAL,
                eps_y1 REAL, year_1 INTEGER,
                eps_y2 REAL, year_2 INTEGER,
                eps_y3 REAL, year_3 INTEGER,
                eps_last REAL,
                industry TEXT, title TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_rpt_stock ON report(stock_code, publish_date);
            CREATE INDEX IF NOT EXISTS ix_rpt_org ON report(stock_code, org_code, publish_date);
            CREATE INDEX IF NOT EXISTS ix_rpt_date ON report(publish_date);

            -- 研报同步水位：按「年」分段，记住哪一段已经拉过
            CREATE TABLE IF NOT EXISTS report_sync (
                seg TEXT PRIMARY KEY, rows INTEGER, fetched_at TEXT
            );
            """)
            cols = {r[1] for r in c.execute("PRAGMA table_info(fin)")}
            if "notice_date" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN notice_date TEXT")
            # 上游入库时间戳（东财 EITIME）：盘后披露的财报公告日会记成次日，
            # 用它与 notice_date 取较早者做 PIT，才不至于当晚仍用上一期报告
            if "notice_ts" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN notice_ts TEXT")
            # 估值扩展列：老库一次性补齐（历史行 NULL，由重拉增量填充）
            for _col in ("bps", "equity", "dividend_rate", "dps",
                         "total_liab", "end_cash", "op_profit"):
                if _col not in cols:
                    c.execute(f"ALTER TABLE fin ADD COLUMN {_col} REAL")
            _d_cols = {r[1] for r in c.execute("PRAGMA table_info(etf_daily)")}
            if "deduct_ttm" not in _d_cols:
                c.execute("ALTER TABLE etf_daily ADD COLUMN deduct_ttm REAL")
            # v9：归母净利合计与扣非 PEG 三件套（老库补列，历史行 NULL，
            # 由 CALC_VERSION 升版后的强制重算补齐）
            for _col in ("np_ttm", "deduct_pe", "g_deduct", "deduct_peg"):
                if _col not in _d_cols:
                    c.execute(f"ALTER TABLE etf_daily ADD COLUMN {_col} REAL")
            _pe_cols = {r[1] for r in c.execute("PRAGMA table_info(stock_pe)")}
            for _col in ("pb", "ps", "pcf"):
                if _col not in _pe_cols:
                    c.execute(f"ALTER TABLE stock_pe ADD COLUMN {_col} REAL")
            _fp_cols = {r[1] for r in c.execute("PRAGMA table_info(fin_period)")}
            if "v2" not in _fp_cols:
                c.execute("ALTER TABLE fin_period ADD COLUMN v2 INTEGER DEFAULT 0")
            _ed_cols = {r[1] for r in c.execute("PRAGMA table_info(etf_daily)")}
            for _col in ("pb", "ps", "div_yield", "pe_pct", "pb_pct", "ps_pct"):
                if _col not in _ed_cols:
                    c.execute(f"ALTER TABLE etf_daily ADD COLUMN {_col} REAL")
            if "div_calc" not in _ed_cols:
                c.execute("ALTER TABLE etf_daily ADD COLUMN div_calc INTEGER DEFAULT 0")
            # v10：etf_daily 主键加 basis（WITHOUT ROWID 不能改主键，只能重建）。
            # 老数据全部迁为 basis='pit'（它们本来就是 PIT 口径算出来的）。
            _ed_cols = {r[1] for r in c.execute("PRAGMA table_info(etf_daily)")}
            if _ed_cols and "basis" not in _ed_cols:
                c.execute("""CREATE TABLE etf_daily_v10 (
                    d TEXT NOT NULL, etf_code TEXT NOT NULL, name TEXT,
                    pe REAL, g_ttm REAL, g_all REAL, peg REAL, coverage REAL,
                    n_valid INTEGER, n_total INTEGER, report_date TEXT,
                    pb REAL, ps REAL, div_yield REAL,
                    pe_pct REAL, pb_pct REAL, ps_pct REAL,
                    div_calc INTEGER DEFAULT 0, deduct_ttm REAL,
                    np_ttm REAL, deduct_pe REAL, g_deduct REAL, deduct_peg REAL,
                    basis TEXT NOT NULL DEFAULT 'pit',
                    PRIMARY KEY (d, etf_code, basis)
                ) WITHOUT ROWID""")
                c.execute("""INSERT INTO etf_daily_v10
                    SELECT d, etf_code, name, pe, g_ttm, g_all, peg, coverage,
                           n_valid, n_total, report_date, pb, ps, div_yield,
                           pe_pct, pb_pct, ps_pct, div_calc, deduct_ttm,
                           np_ttm, deduct_pe, g_deduct, deduct_peg, 'pit'
                    FROM etf_daily""")
                c.execute("DROP TABLE etf_daily")
                c.execute("ALTER TABLE etf_daily_v10 RENAME TO etf_daily")
                c.execute("CREATE INDEX IF NOT EXISTS ix_daily_etf "
                          "ON etf_daily(etf_code, basis, d)")
            # v10：口径版本也按 basis 拆分（两套序列独立增量）
            _cv_cols = {r[1] for r in c.execute("PRAGMA table_info(etf_calc_version)")}
            if _cv_cols and "basis" not in _cv_cols:
                c.execute("""CREATE TABLE etf_calc_version_v10 (
                    etf_code TEXT NOT NULL, basis TEXT NOT NULL DEFAULT 'pit',
                    version INTEGER, updated_at TEXT,
                    PRIMARY KEY (etf_code, basis)
                ) WITHOUT ROWID""")
                c.execute("""INSERT INTO etf_calc_version_v10
                    SELECT etf_code, 'pit', version, updated_at FROM etf_calc_version""")
                c.execute("DROP TABLE etf_calc_version")
                c.execute("ALTER TABLE etf_calc_version_v10 RENAME TO etf_calc_version")
            # 港股：PE 要求市值与净利同币种，股本用于按报告期还原历史市值
            if "currency" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN currency TEXT")
            if "shares" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN shares REAL")
            # 港股财报类型与财年截止（v3：按公司财年构造 TTM 需要）
            if "report_type" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN report_type TEXT")
            if "fiscal_year" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN fiscal_year TEXT")
            # 扣非归母净利润（v4：成分股明细要展示 TTM 扣非及其上年同期）
            if "deduct_np" not in cols:
                c.execute("ALTER TABLE fin ADD COLUMN deduct_np REAL")
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
        for code, name, pnp, yoy, rev, ryoy, eps, roe, rd, nd, nts, cur, sh, \
                bps, eq, divr, dps, tliab, cash, op, rt, fy, dnp in c.execute(
                "SELECT code,name,parent_np,np_yoy,revenue,rev_yoy,eps,roe,report_date,"
                "notice_date,notice_ts,currency,shares,"
                "bps,equity,dividend_rate,dps,total_liab,end_cash,op_profit,"
                "report_type,fiscal_year,deduct_np "
                "FROM fin WHERE period=?", (period,)):
            out[code] = {"name": name, "parent_np": pnp, "np_yoy": yoy, "revenue": rev,
                         "rev_yoy": ryoy, "eps": eps, "roe": roe, "report_date": rd,
                         "notice_date": nd, "notice_ts": nts,
                         "currency": cur, "shares": sh,
                         "bps": bps, "equity": eq, "dividend_rate": divr, "dps": dps,
                         "total_liab": tliab, "end_cash": cash, "op_profit": op,
                         "report_type": rt, "fiscal_year": fy, "deduct_np": dnp}
        return out if len(out) >= max(1, (n or 0) // 2) else None

    def fin_has_notice(self, period: str) -> bool:
        """该期是否已存有真实披露日。老数据缺这个字段，需要重新拉一次。"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? AND notice_date IS NOT NULL",
            (period,)).fetchone()
        return bool(row and row[0] > 0)

    def fin_has_notice_ts(self, period: str) -> bool:
        """该期是否已存上游入库时间戳（PIT 用它修正盘后披露的次日公告日）"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? AND notice_ts IS NOT NULL",
            (period,)).fetchone()
        return bool(row and row[0] > 0)

    def has_fin(self, period: str) -> bool:
        """该期是否已在库（用于判断要不要回源补拉，比 load_fin 便宜得多）"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? LIMIT 1", (period,)).fetchone()
        return bool(row and row[0] > 0)

    def load_fin_subset(self, period: str, codes: List[str]) -> Dict[str, dict]:
        """只取指定股票在某期的净利/营收/同比/披露日/净资产/EPS/ROE 等——算增速与估值序列用"""
        self.init()
        out: Dict[str, dict] = {}
        if not codes:
            return out
        B = 400
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for code, pnp, yoy, nd, cur, sh, rev, bps, eq, divr, dps, tliab, cash, op, eps, roe, dnp \
                    in self._conn().execute(
                    f"SELECT code,parent_np,np_yoy,notice_date,currency,shares,"
                    f"revenue,bps,equity,dividend_rate,dps,total_liab,end_cash,op_profit,"
                    f"eps,roe,deduct_np "
                    f"FROM fin WHERE period=? AND code IN ({ph})", (period, *chunk)):
                out[code] = {"parent_np": pnp, "np_yoy": yoy, "notice_date": nd,
                             "currency": cur, "shares": sh, "revenue": rev,
                             "bps": bps, "equity": eq, "dividend_rate": divr, "dps": dps,
                             "total_liab": tliab, "end_cash": cash, "op_profit": op,
                             "eps": eps, "roe": roe, "deduct_np": dnp}
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
                "INSERT OR REPLACE INTO fin (period,code,name,parent_np,np_yoy,revenue,"
                "rev_yoy,eps,roe,report_date,notice_date,notice_ts,currency,shares,"
                "bps,equity,dividend_rate,dps,total_liab,end_cash,op_profit,"
                "report_type,fiscal_year,deduct_np) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(period, code, v.get("name"), v.get("parent_np"), v.get("np_yoy"),
                  v.get("revenue"), v.get("rev_yoy"), v.get("eps"), v.get("roe"),
                  v.get("report_date"), v.get("notice_date"), v.get("notice_ts"),
                  v.get("currency"), v.get("shares"),
                  v.get("bps"), v.get("equity"), v.get("dividend_rate"), v.get("dps"),
                  v.get("total_liab"), v.get("end_cash"), v.get("op_profit"),
                  v.get("report_type"), v.get("fiscal_year"), v.get("deduct_np"))
                 for code, v in data.items()])
            c.execute("INSERT OR REPLACE INTO fin_period VALUES (?,?,?,?,1)",
                      (period, len(data), 1 if settled else 0, _now()))
            c.commit()

    def fin_v2(self, period: str) -> bool:
        """该期财报是否已按扩展列（bps/净资产/股息等）重拉过。
        港股旧数据靠它触发一次性升级重拉；重拉成功后 save_fin 自动置 v2=1。"""
        self.init()
        row = self._conn().execute(
            "SELECT v2 FROM fin_period WHERE period=?", (period,)).fetchone()
        return bool(row and row[0])

    def fin_has_report_type(self, period: str) -> bool:
        """该期是否已存有财报类型/财年截止（v3 字段）。老数据缺这两个字段，
        需要重拉一次升级——fetch_hk_financials 的缓存命中条件之一。"""
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? AND report_type IS NOT NULL",
            (period,)).fetchone()
        return bool(row and row[0] > 0)

    def fin_has_deduct(self, period: str) -> bool:
        """该期是否已存有扣非归母净利润。

        扣非来自另一张报表接口（利润表 RPT_DMSK_FN_INCOME），与主财报分开拉取：
        老库的 fin 行里该列为 NULL，命中缓存时要靠它判断是否需要补拉一次。
        """
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*) FROM fin WHERE period=? AND deduct_np IS NOT NULL",
            (period,)).fetchone()
        return bool(row and row[0] > 0)

    def save_deduct(self, period: str, data: Dict[str, Optional[float]]) -> int:
        """把扣非归母净利润回填进已入库的 fin 行（只 UPDATE 这一列）。

        返回实际更新的行数。fin 行不存在时 UPDATE 自然不生效，
        所以调用方要先 save_fin 再 save_deduct。
        """
        self.init()
        rows = [(v, period, code) for code, v in data.items() if v is not None]
        if not rows:
            return 0
        with _WRITE_LOCK:
            c = self._conn()
            c.executemany("UPDATE fin SET deduct_np=? WHERE period=? AND code=?", rows)
            c.commit()
        return len(rows)

    def load_fin_rows(self, codes: List[str]) -> Dict[str, List[dict]]:
        """
        按公司读取其全部报告期的财报行（升序），供港股按公司财年构造 TTM。

        返回 {code: [{period, parent_np, revenue, op_profit, report_type,
                     fiscal_year, np_yoy, dps, equity, report_date}, ...]}。
        与 load_fin_subset（按期取全市场）互补：TTM 构造需要看单家公司的
        完整报告序列（年报/中报/季报的跨度各不相同）。
        """
        self.init()
        out: Dict[str, List[dict]] = {c: [] for c in codes}
        if not codes:
            return out
        B = 400                                   # SQLite 变量上限保护
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for code, period, pnp, rev, op, rt, fy, yoy, dps, eq, rd, dnp in \
                    self._conn().execute(
                    f"SELECT code, period, parent_np, revenue, op_profit, report_type, "
                    f"fiscal_year, np_yoy, dps, equity, report_date, deduct_np FROM fin "
                    f"WHERE code IN ({ph}) ORDER BY code, period", chunk):
                out[code].append({
                    "period": period, "parent_np": pnp, "revenue": rev,
                    "op_profit": op, "report_type": rt, "fiscal_year": fy,
                    "np_yoy": yoy, "dps": dps, "equity": eq, "report_date": rd,
                    "deduct_np": dnp,
                })
        return out

    # ------------------------------------------------------------------ #
    #  10 年期国债收益率（bond_yield）
    # ------------------------------------------------------------------ #
    def save_bond_yields(self, rows: List[dict]) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.executemany(
                "INSERT OR REPLACE INTO bond_yield (d, y10) VALUES (?,?)",
                [(r["date"], r["y10"]) for r in rows])
            c.commit()

    def latest_bond_yield(self) -> Tuple[Optional[str], Optional[float]]:
        self.init()
        row = self._conn().execute(
            "SELECT d, y10 FROM bond_yield ORDER BY d DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (None, None)

    def load_bond_yields(self, start: str, end: str) -> Dict[str, float]:
        self.init()
        return {r[0]: r[1] for r in self._conn().execute(
            "SELECT d, y10 FROM bond_yield WHERE d>=? AND d<=?", (start, end))}

    # ------------------------------------------------------------------ #
    #  机构一致预期（盈利预测）
    # ------------------------------------------------------------------ #
    def load_forecast(self, code: str, max_age: float) -> Optional[dict]:
        """{years: {year: {mark,np,np_ratio,eps,pe}}, n_org}；过期或无数据返回 None"""
        self.init()
        rows = self._conn().execute(
            "SELECT year, mark, np, np_ratio, eps, pe, n_org, fetched_at "
            "FROM cons_forecast WHERE code=?", (code,)).fetchall()
        if not rows:
            return None
        try:
            newest = max(time.mktime(time.strptime(r[7], "%Y-%m-%d %H:%M:%S"))
                         for r in rows)
        except Exception:                               # noqa: BLE001
            return None
        if time.time() - newest > max_age:
            return None
        years = {r[0]: {"mark": r[1], "np": r[2], "np_ratio": r[3],
                        "eps": r[4], "pe": r[5]} for r in rows}
        return {"years": years, "n_org": rows[0][6]}

    def save_forecast(self, code: str, years: Dict[int, dict],
                      n_org: Optional[int]) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute("DELETE FROM cons_forecast WHERE code=?", (code,))
            c.executemany(
                "INSERT OR REPLACE INTO cons_forecast "
                "(code, year, mark, np, np_ratio, eps, pe, n_org, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [(code, y, v.get("mark"), v.get("np"), v.get("np_ratio"),
                  v.get("eps"), v.get("pe"), n_org, _now())
                 for y, v in years.items()])
            c.commit()

    # ------------------------------------------------------------------ #
    #  券商研报盈利预测（全历史，item = 一篇研报）
    # ------------------------------------------------------------------ #
    _REPORT_COLS = ("info_code", "publish_date", "stock_code", "stock_name",
                    "org_code", "org_name", "researcher", "rating", "last_rating",
                    "rating_change", "target_price", "eps_y1", "year_1",
                    "eps_y2", "year_2", "eps_y3", "year_3", "eps_last",
                    "industry", "title")

    def save_reports(self, rows: List[dict]) -> int:
        """批量写入研报预测，返回真正新增的条数（info_code 已存在则忽略）。"""
        self.init()
        if not rows:
            return 0
        cols = self._REPORT_COLS
        sql = ("INSERT OR IGNORE INTO report (" + ",".join(cols) + ") VALUES ("
               + ",".join("?" * len(cols)) + ")")
        with _WRITE_LOCK:
            c = self._conn()
            before = c.total_changes
            c.executemany(sql, [tuple(r.get(k) for k in cols) for r in rows])
            c.commit()
            return c.total_changes - before

    def load_stock_reports(self, code: str) -> List[dict]:
        """该股全部研报，按发布日升序（同日按 info_code 稳定排序）。"""
        self.init()
        out: List[dict] = []
        for r in self._conn().execute(
                "SELECT info_code, publish_date, org_code, org_name, researcher, "
                "rating, last_rating, rating_change, target_price, "
                "eps_y1, year_1, eps_y2, year_2, eps_y3, year_3, eps_last, "
                "stock_name, industry, title "
                "FROM report WHERE stock_code=? ORDER BY publish_date, info_code", (code,)):
            out.append({
                "info_code": r[0], "publish_date": r[1], "org_code": r[2],
                "org_name": r[3], "researcher": r[4], "rating": r[5],
                "last_rating": r[6], "rating_change": r[7], "target_price": r[8],
                "eps_y1": r[9], "year_1": r[10], "eps_y2": r[11], "year_2": r[12],
                "eps_y3": r[13], "year_3": r[14], "eps_last": r[15],
                "stock_name": r[16], "industry": r[17], "title": r[18],
            })
        return out

    def stock_latest_report_date(self, code: str) -> Optional[str]:
        """该股已入库研报的最新发布日；没有则 None（弹窗据此决定增量起点）。"""
        self.init()
        row = self._conn().execute(
            "SELECT MAX(publish_date) FROM report WHERE stock_code=?", (code,)).fetchone()
        return row[0] if row and row[0] else None

    def report_stats(self) -> dict:
        self.init()
        row = self._conn().execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_code), "
            "MIN(publish_date), MAX(publish_date) FROM report").fetchone()
        return {"rows": row[0] or 0, "stocks": row[1] or 0,
                "start": row[2], "end": row[3]}

    def report_seg_done(self, seg: str) -> bool:
        self.init()
        return bool(self._conn().execute(
            "SELECT 1 FROM report_sync WHERE seg=?", (seg,)).fetchone())

    def report_seg_save(self, seg: str, n: int) -> None:
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute("INSERT OR REPLACE INTO report_sync VALUES (?,?,?)",
                      (seg, n, _now()))
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
                # UPSERT：pb/ps/pcf 缺失时保留库里的旧值——
                # 当日快照/增量重拉可能只带 PE，不能把已入库的 PB 抹成 NULL
                "INSERT INTO stock_pe (trade_date, code, pe_ttm, pe_lar, mktcap, close, pb, ps, pcf) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(trade_date, code) DO UPDATE SET "
                "pe_ttm=excluded.pe_ttm, pe_lar=excluded.pe_lar, mktcap=excluded.mktcap, "
                "close=excluded.close, "
                "pb=COALESCE(excluded.pb, stock_pe.pb), "
                "ps=COALESCE(excluded.ps, stock_pe.ps), "
                "pcf=COALESCE(excluded.pcf, stock_pe.pcf)",
                [(r["date"], r["code"], r.get("pe_ttm"), r.get("pe_lar"),
                  r.get("mktcap"), r.get("close"),
                  r.get("pb"), r.get("ps"), r.get("pcf")) for r in rows])
            self._conn().commit()

    def load_stock_pe(self, codes: List[str], start: str, end: str) -> Dict[str, Dict[str, tuple]]:
        """
        返回 {code: {date: (pe_ttm, mktcap, close, pb, ps, pcf)}}。
        用元组而非字典：长区间回填会载入约 200 万格，小字典的分配开销会成为耗时大头。
        pb/ps/pcf 为 None 表示该行是扩展列启用前入库的旧数据（可用 --init-valuation 重拉补齐）。
        """
        self.init()
        out: Dict[str, Dict[str, tuple]] = {c: {} for c in codes}
        if not codes:
            return out
        B = 400                                   # SQLite 变量上限保护
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for d, c, pe, mc, cl, pb, ps, pcf in self._conn().execute(
                    f"SELECT trade_date, code, pe_ttm, mktcap, close, pb, ps, pcf FROM stock_pe "
                    f"WHERE code IN ({ph}) AND trade_date>=? AND trade_date<=?",
                    (*chunk, start, end)):
                if c in out:
                    out[c][d] = (pe, mc, cl, pb, ps, pcf)
        return out

    # ------------------------------------------------------------------ #
    #  个股历史分红（股息率的数据源，永久保存）
    # ------------------------------------------------------------------ #
    def save_stock_div(self, rows: List[dict]) -> None:
        """写入分红明细 rows=[{code, ex_date, dps, report_date, plan}]"""
        self.init()
        with _WRITE_LOCK:
            self._conn().executemany(
                "INSERT OR REPLACE INTO stock_div (code, ex_date, dps, report_date, plan) "
                "VALUES (?,?,?,?,?)",
                [(r["code"], r["ex_date"], r.get("dps"), r.get("report_date"),
                  r.get("plan")) for r in rows])
            self._conn().commit()

    def load_stock_div(self, codes: List[str]) -> Dict[str, List[tuple]]:
        """返回 {code: [(ex_date, dps), ...]}，按除息日升序（调用方做前缀和 + 二分）"""
        self.init()
        out: Dict[str, List[tuple]] = {c: [] for c in codes}
        if not codes:
            return out
        B = 400                                   # SQLite 变量上限保护
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for c, ex, dps in self._conn().execute(
                    f"SELECT code, ex_date, dps FROM stock_div "
                    f"WHERE code IN ({ph}) ORDER BY code, ex_date", chunk):
                if c in out:
                    out[c].append((ex, dps))
        return out

    def div_pending(self, codes: List[str]) -> List[str]:
        """挑出还没拉过分红历史的股票（从未分红的会返回空结果，靠水位避免重拉）"""
        self.init()
        done = {r[0] for r in self._conn().execute("SELECT code FROM div_fetch_log")}
        return [c for c in codes if c not in done]

    def set_div_fetch_log(self, code: str) -> None:
        self.init()
        with _WRITE_LOCK:
            self._conn().execute(
                "INSERT OR REPLACE INTO div_fetch_log VALUES (?,?)", (code, _now()))
            self._conn().commit()

    def div_coverage(self) -> dict:
        """分红拉取进度：done=已拉过股票数，total=估值库快照日清单数（与 pe_coverage 同分母）"""
        self.init()
        c = self._conn()
        snap = self.latest_market_pe_date()
        total = c.execute("SELECT COUNT(DISTINCT code) FROM stock_pe WHERE trade_date=?",
                          (snap,)).fetchone()[0] if snap else 0
        if not total:
            total = len(self.known_pe_codes())
        done = c.execute("SELECT COUNT(*) FROM div_fetch_log").fetchone()[0]
        return {"total": total, "done": done}

    def _sync_calendar(self) -> None:
        """
        把 stock_pe 里新出现的交易日补进日历表。

        之前只补「比日历最大日期更新」的部分，会漏掉「中间残缺日」：
        某天快照因限流只写入部分股票，后补拉 K 线才补齐的那天，日期小于
        日历最大日期，永远进不了日历 → detect_kline_gaps 的「应有交易日数」
        被低估，真实空洞检测不出来（如港股 10-13/14/15 残缺却查不出）。
        改为补「最近 90 天内所有缺失日期」：扫描范围小、有索引，毫秒级。
        """
        self.init()
        since = (date.today() - timedelta(days=90)).isoformat()
        with _WRITE_LOCK:
            c = self._conn()
            c.execute(
                "INSERT OR IGNORE INTO trade_calendar(trade_date) "
                "SELECT DISTINCT trade_date FROM stock_pe WHERE trade_date >= ?",
                (since,))
            # 必须无条件 commit：Python 3.12+ 的 sqlite3 默认 isolation_level=""
            # 是「隐式事务」，DML 语句执行时自动 BEGIN，不 commit 则写锁不释放。
            # 曾经用 `if cur.rowcount: commit()`，当 INSERT 全被 IGNORE（无新日期）
            # 时 rowcount=0 跳过 commit，导致写锁泄漏——并发回填时另一个线程
            # 等待 30s 后报 `database is locked`。
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

    def fin_codes(self) -> set:
        """fin 表里出现过的所有代码（港股初始化用它确定要拉 K 线的股票清单）。

        不能用「最新一期财报」过滤：港股公司财年截止日不统一（12 月、3 月、
        9 月年结都有），财年非 12 月年结的公司财报落在 03-31 / 09-30 期，
        用最新半年报期过滤会漏掉它们（实测漏 625 只）。"""
        self.init()
        return {r[0] for r in self._conn().execute("SELECT DISTINCT code FROM fin")}

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

    def pb_pending(self, codes: List[str]) -> List[str]:
        """估值重初始化用：这些代码里已入库但缺 pb 列数据的（老数据），需要重拉升级"""
        self.init()
        c = self._conn()
        out: List[str] = []
        B = 500
        for i in range(0, len(codes), B):
            chunk = codes[i:i + B]
            ph = ",".join("?" * len(chunk))
            for (x,) in c.execute(
                    f"SELECT code FROM stock_pe WHERE code IN ({ph}) "
                    f"AND pb IS NULL GROUP BY code", chunk):
                out.append(x)
        return out

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
        否则统一用 d / 今天（日常分析用）；basis 缺省为 fixed（主口径）。
        """
        self.init()
        default_d = d or time.strftime("%Y-%m-%d")
        with _WRITE_LOCK:
            c = self._conn()
            c.executemany(
                "INSERT OR REPLACE INTO etf_daily (d, etf_code, name, pe, g_ttm, g_all, "
                "peg, coverage, n_valid, n_total, report_date, pb, ps, div_yield, "
                "pe_pct, pb_pct, ps_pct, div_calc, deduct_ttm, "
                "np_ttm, deduct_pe, g_deduct, deduct_peg, basis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(r.get("date") or default_d, r.get("code"), r.get("name"), r.get("pe"),
                  r.get("g_ttm"), r.get("g_all"), r.get("peg"), r.get("coverage"),
                  r.get("n_valid"), r.get("n_total"), r.get("report_date"),
                  r.get("pb"), r.get("ps"), r.get("div_yield"),
                  r.get("pe_pct"), r.get("pb_pct"), r.get("ps_pct"),
                  r.get("div_calc", 0), r.get("deduct_ttm_sum"),
                  r.get("np_ttm"), r.get("deduct_pe"), r.get("g_deduct"),
                  r.get("deduct_peg"), r.get("basis") or "fixed")
                 for r in rows if r.get("code")])
            c.commit()

    def clear_daily(self, etf_code: Optional[str] = None,
                    basis: Optional[str] = None) -> int:
        """清空每日快照（口径变更时用）；不传代码/basis 则清空全部。"""
        self.init()
        with _WRITE_LOCK:
            c = self._conn()
            where, args = [], []
            if etf_code:
                where.append("etf_code=?")
                args.append(etf_code)
            if basis:
                where.append("basis=?")
                args.append(basis)
            sql = "DELETE FROM etf_daily" + (" WHERE " + " AND ".join(where) if where else "")
            c.execute(sql, args)
            n = c.total_changes
            c.commit()
        return n

    def get_history(self, etf_code: str, days: int = 180,
                    basis: str = "fixed") -> List[dict]:
        """历史快照。默认 fixed（最新持仓回溯，柱子间可比）；basis='pit' 取旧口径。"""
        self.init()
        rows = self._conn().execute(
            "SELECT d, name, pe, g_ttm, g_all, peg, coverage, n_valid, n_total, report_date,"
            " pb, ps, div_yield, pe_pct, pb_pct, ps_pct, div_calc, deduct_ttm, "
            " np_ttm, deduct_pe, g_deduct, deduct_peg "
            "FROM etf_daily WHERE etf_code=? AND basis=? ORDER BY d DESC LIMIT ?",
            (etf_code, basis, days)).fetchall()
        return [{"date": r[0], "name": r[1], "pe": r[2], "g_ttm": r[3], "g_all": r[4],
                 "peg": r[5], "coverage": r[6], "n_valid": r[7], "n_total": r[8],
                 "report_date": r[9], "pb": r[10], "ps": r[11], "div_yield": r[12],
                 "pe_pct": r[13], "pb_pct": r[14], "ps_pct": r[15],
                 "div_calc": r[16], "deduct_ttm": r[17],
                 "np_ttm": r[18], "deduct_pe": r[19], "g_deduct": r[20],
                 "deduct_peg": r[21], "basis": basis} for r in rows]

    def get_calc_version(self, etf_code: str, basis: str = "fixed") -> int:
        """该 ETF 某口径的历史快照是用哪个计算口径版本算的（0 = 从未记录）"""
        self.init()
        row = self._conn().execute(
            "SELECT version FROM etf_calc_version WHERE etf_code=? AND basis=?",
            (etf_code, basis)).fetchone()
        return row[0] if row else 0

    def set_calc_version(self, etf_code: str, version: int,
                         basis: str = "fixed") -> None:
        """回填完成后记录口径版本；版本一致时下次只做增量"""
        self.init()
        with _WRITE_LOCK:
            self._conn().execute(
                "INSERT INTO etf_calc_version(etf_code, basis, version, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(etf_code, basis) DO UPDATE SET "
                "version=excluded.version, updated_at=excluded.updated_at",
                (etf_code, basis, version, date.today().isoformat()))
            self._conn().commit()

    def pe_ranking(self, years: Optional[float] = None, top: int = 40,
                   min_days: int = 60) -> dict:
        """
        全市场 ETF 的 PE 历史百分位排行（数据源：etf_daily 每日快照）。

        百分位口径与 analyze_etf 的历史分位一致：窗口内 PE 序列中
        <= 当前值的比例 × 100，越低 = 当前估值在自身历史上越便宜。
        当前值取窗口内最新一条快照的 PE——快照由分析（当天）与回填
        （历史区间）写入，回填过的 ETF 天然带最近交易日的数据。

        years:     窗口年数，None 表示「成立以来」（该 ETF 全部快照历史）。
        top:       返回前 N 名（百分位升序，附 rank 字段）。
        min_days:  窗口内有效 PE 样本少于该值的 ETF 不参与——样本太少的
                   分位没有参考意义（约一个季度的量级）。

        全表一次拉取 + 内存分组：几十万行毫秒~百毫秒级，调用方（server）
        再做一层 memo 缓存即可。只有分析过 / 回填过历史的 ETF 才有序列，
        覆盖面随「排行数据准备」批量回填任务扩大。
        """
        self.init()
        cutoff = ""
        if years:
            cutoff = (date.today() - timedelta(days=int(years * 365))).isoformat()
        out: Dict[str, dict] = {}
        # 只排场内 ETF：etf_daily 里还混着用户分析/回填过的场外基金
        # （005827 等），它们的单只百分位在分析结果页已有，排行限定场内。
        for code, name, d, pe, peg, cov, g in self._conn().execute(
                "SELECT etf_code, name, d, pe, peg, coverage, g_ttm "
                "FROM etf_daily WHERE d >= ? "
                "AND etf_code IN (SELECT code FROM etf) "
                "ORDER BY etf_code, d", (cutoff,)):
            s = out.setdefault(code, {"name": name, "n_rows": 0, "pes": [],
                                      "lo": None, "hi": None, "last": None})
            s["n_rows"] += 1
            if pe and pe > 0:
                s["pes"].append(pe)
                s["lo"] = pe if s["lo"] is None else min(s["lo"], pe)
                s["hi"] = pe if s["hi"] is None else max(s["hi"], pe)
            s["last"] = (d, pe, peg, cov, g)            # 按 d 升序，最后一条即最新
        rows: List[dict] = []
        for code, s in out.items():
            last = s["last"]
            if not last or not last[1] or len(s["pes"]) < min_days:
                continue
            cur = last[1]
            pes = s["pes"]
            rows.append({
                "code": code, "name": s["name"], "date": last[0], "pe": cur,
                "pct": round(sum(1 for x in pes if x <= cur) / len(pes) * 100.0, 1),
                "days": len(pes), "total_days": s["n_rows"],
                "coverage": last[3], "peg": last[2], "g_ttm": last[4],
                "pe_lo": s["lo"], "pe_hi": s["hi"],
            })
        rows.sort(key=lambda r: (r["pct"], -(r["coverage"] or 0)))
        eligible = len(rows)
        rows = rows[:max(1, top)]
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        return {"rows": rows, "eligible": eligible, "candidates": len(out),
                "cutoff": cutoff or None, "min_days": min_days}

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
            # 研报盈利预测（只有 A 股库有数据，港股库恒为 0）
            "report_rows": q("SELECT COUNT(*) FROM report"),
            "report_stocks": q("SELECT COUNT(DISTINCT stock_code) FROM report"),
            "report_range": c.execute(
                "SELECT MIN(publish_date), MAX(publish_date) FROM report").fetchone(),
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
