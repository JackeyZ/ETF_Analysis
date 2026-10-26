# -*- coding: utf-8 -*-
"""临时：导出 013816 数据供报告统计"""
import json
import sys
from dataclasses import asdict
sys.path.insert(0, r'd:/ETF_Analysis/ETF_Analysis')
from etf_peg import peg
from etf_peg.store import A

r = peg.analyze_etf("013816", "013816", "ttm", record=False, series_quarters=20)
d = asdict(r)
d["market_cap"] = 237000000.0
d["est_pct"] = {"pct": -2.34, "coverage": 93.7}

daily = A.get_history("013816", 99999, "fixed")
hist = {"daily": daily,
        "by_year": peg.aggregate_history(daily, "year"),
        "by_quarter": peg.aggregate_history(daily, "quarter"),
        "by_month": peg.aggregate_history(daily, "month")}
json.dump({"d": d, "hist": hist}, open(r'd:/ETF_Analysis/ETF_Analysis/_tmp_report_data.json',
                                       'w', encoding='utf-8'), ensure_ascii=False)

# K 线（净值）：报告「六、历史股价/净值」用
from etf_peg import datasource as ds
kline = ds.fetch_fund_nav("013816")
json.dump(kline, open(r'd:/ETF_Analysis/ETF_Analysis/_tmp_kline.json',
                      'w', encoding='utf-8'), ensure_ascii=False)
print("daily", len(daily), "months", len(hist["by_month"]), "stocks", len(d["stocks"]),
      "kline", len(kline.get("rows") or []))
