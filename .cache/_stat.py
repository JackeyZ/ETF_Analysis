import sqlite3
c = sqlite3.connect(r"D:\行情分析\etf_peg.db")
q = lambda s: c.execute(s).fetchone()[0]

print("成分股并集（历史全期）")
for etf in ("510300", "003096", "512480", "159915"):
    n = q(f"select count(distinct stock_code) from holding where etf_code='{etf}'")
    reps = q(f"select count(distinct report_date) from holding where etf_code='{etf}'")
    have = q(f"select count(*) from (select distinct stock_code from holding where etf_code='{etf}') "
             f"where stock_code in (select code from stock_pe group by code having count(*)>=2000)")
    print(f"  {etf}: 并集={n} 只, 报告期={reps}, 其中已有全历史PE={have}")

print()
print("已拉全历史 PE 的股票数(>=2000行):",
      q("select count(*) from (select code from stock_pe group by code having count(*)>=2000)"))
print("pe_fetch_log 条目:", q("select count(*) from pe_fetch_log"))

print()
print("--- 每个交易日 stock_pe 覆盖的股票数（抽样）---")
for d in ("2018-01-02", "2019-06-03", "2021-01-04", "2023-01-03", "2025-01-02", "2026-01-02", "2026-09-09"):
    print(f"  {d}: {q(chr(34)+'x'+chr(34)) if False else q(f'select count(*) from stock_pe where trade_date={chr(39)}{d}{chr(39)}')}")

print()
print("DB MB:", q("select (select page_count from pragma_page_count())*(select page_size from pragma_page_size())/1024/1024"))
