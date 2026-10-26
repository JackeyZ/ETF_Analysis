# -*- coding: utf-8 -*-
"""复验：目标年度 / 一致预期序列 / 修正统计 / 机构数。"""
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

from etf_peg import reports, store

for code in ("600519", "300750", "000001", "688256"):
    d = reports.stock_forecast(code)
    if d.get("empty"):
        print(f"{code}: 无数据")
        continue
    print("=" * 84)
    print(f"{d['code']} {d['name']}  {d['total']} 篇 / {d['n_org']} 家 | "
          f"目标年 {d['targets']} | 序列 {len(d['series'])} 月")
    for y in d["targets"]:
        a = d["revision"]["d30"].get(str(y), {})
        b = d["revision"]["d90"].get(str(y), {})
        print(f"   {y}E  近30天 上{a.get('up',0)} 下{a.get('down',0)} 平{a.get('flat',0)}"
              f"   |  近90天 上{b.get('up',0)} 下{b.get('down',0)} 平{b.get('flat',0)}")
    tail = d["series"][-4:]
    for s in tail:
        print(f"     {s['m']}  " + "  ".join(
            f"{y}E={s['v'][y]}({s['n'][y]}家)" for y in s["v"]))

print()
print("=" * 84)
print("核查平安银行的机构分布（227 篇却只有 5 家？）")
rows = store.load_stock_reports("000001")
c = Counter((r["org_code"], r["org_name"]) for r in rows)
print(f"   不同 (code,name) 组合 {len(c)} 个，前 8：")
for (oc, on), n in c.most_common(8):
    print(f"      {oc}  {on}  {n} 篇")
