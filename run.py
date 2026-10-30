# -*- coding: utf-8 -*-
"""
ETF PEG 实时分析工具 —— 启动入口。

    python run.py                 # 默认 http://127.0.0.1:8770
    python run.py --port 9000
    python run.py --cli 510300    # 命令行模式，直接输出单只 ETF 的 PEG
"""
from __future__ import annotations

import argparse
import os
import sys

from etf_peg import datasource as ds
from etf_peg import peg
from etf_peg.server import start_server


def _bar(pct, label="") -> None:
    """终端进度条：原地刷新，不刷屏"""
    w = 28
    p = max(0.0, min(100.0, float(pct)))
    n = int(round(p / 100 * w))
    sys.stdout.write("\r  [" + "#" * n + "." * (w - n) + f"] {p:5.1f}%  {str(label)[:46]:<46}")
    sys.stdout.flush()


def _kill_stale_instances(port: int) -> None:
    """
    启动服务前清理残留的旧 run.py 进程（Windows）。

    run.py 用 SQLite（WAL）+ 固定端口，一旦上一次实例没退干净（黑窗口被强杀、
    或此前用隐藏窗口方式启动的），新实例就会和旧实例抢同一个数据库写锁，
    表现为启动时报 `database is locked`、回填卡死。

    这里按「谁占着目标端口」来判断旧实例并结束它：只杀监听该端口的进程，
    绝不会误伤当前启动链路（py.exe 启动器 / 新 python 进程都还没监听端口）。
    清理失败静默降级，不阻断启动。
    """
    if sys.platform != "win32":
        return
    import subprocess
    ps = (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue; "
        f"if ($c) {{ $c | Select-Object -ExpandProperty OwningProcess -Unique | "
        f"ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }} }}"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", ps], capture_output=True, timeout=20)
    except Exception:                                   # noqa: BLE001
        pass


def run_cli(code: str, mode: str) -> None:
    print("载入行情与财报（首次较慢，之后读本地库）...")
    r = peg.analyze_etf(code, code, mode)
    print("\n" + "=" * 68)
    print(f"{r.name} ({r.code})")
    print("=" * 68)
    print(f"持仓报告期      : {r.report_date}  [{'全持仓' if r.holdings_full else '仅前十大重仓'}]")
    print(f"财报报告期      : {r.fin_period}")
    print(f"成分股          : 共 {r.n_total} 只，PEG 有效 {r.n_valid} 只，权重覆盖率 {r.coverage:.1f}%")
    print(f"整体法 PE(TTM)  : {r.pe:.2f}" if r.pe else "整体法 PE(TTM)  : N/A")
    print(f"  └ 含亏损口径  : {r.pe_all:.2f}（覆盖 {r.pe_all_coverage:.0f}% 权重）"
          if r.pe_all else "  └ 含亏损口径  : N/A（组合合计净利 ≤ 0）")
    print(f"整体法增速(TTM) : {r.g_ttm:.2f}%" if r.g_ttm is not None else "整体法增速(TTM) : N/A")
    print(f"整体法增速(最新): {r.g_latest:.2f}%" if r.g_latest is not None else "整体法增速(最新): N/A")
    print(f"等权增速(TTM参照): {r.g_all:.2f}%" if r.g_all is not None else "等权增速(TTM参照): N/A")
    print(f"PEG             : {r.peg:.3f}" if r.peg else "PEG             : N/A")
    print(f"扣非PE(整体法)  : {r.deduct_pe:.2f}" if r.deduct_pe else "扣非PE(整体法)  : N/A")
    print(f"扣非增速(TTM)   : {r.g_deduct_ttm:.2f}%" if r.g_deduct_ttm is not None
          else "扣非增速(TTM)   : N/A")
    print(f"PEG(扣非口径)   : {r.deduct_peg:.3f}" if r.deduct_peg else "PEG(扣非口径)   : N/A")
    print(f"归母净利(加权)  : {r.np_ttm_sum / 1e8:.1f} 亿" if r.np_ttm_sum is not None
          else "归母净利(加权)  : N/A")
    print(f"扣非净利(加权)  : {r.deduct_ttm_sum / 1e8:.1f} 亿" if r.deduct_ttm_sum is not None
          else "扣非净利(加权)  : N/A")
    if r.peg_median:
        print(f"PEG(中位数法)   : {r.peg_median:.3f}   [稳健参考]")
    if r.peg_weighted_avg:
        print(f"PEG(加权平均)   : {r.peg_weighted_avg:.3f}   [易被极端值带偏，仅作对照]")
    if r.warn:
        print(f"\n提示: {r.warn}")

    print("\n权重前 15 大成分股：")
    print(f"{'代码':<8}{'名称':<12}{'权重%':>8}{'PE-TTM':>10}{'增速%':>10}{'PEG':>9}  状态")
    for s in r.stocks[:15]:
        g = s["g_ttm"] if mode == "ttm" else s["g_latest"]
        pe = f"{s['pe_ttm']:.2f}" if s["pe_ttm"] else "N/A"
        gv = f"{g:.1f}" if g is not None else "N/A"
        pg = f"{s['peg']:.2f}" if s["peg"] else "N/A"
        print(f"{s['code']:<8}{s['name'][:11]:<12}"
              f"{(s['weight'] or 0):>8.2f}{pe:>10}{gv:>10}{pg:>9}  {s['status']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="ETF PEG 实时分析工具")
    ap.add_argument("--port", type=int, default=8770, help="Web 服务端口")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址")
    ap.add_argument("--cli", metavar="ETF代码", help="命令行模式，直接分析单只 ETF")
    ap.add_argument("--mode", choices=["ttm", "latest"], default="ttm",
                    help="增速口径：ttm=TTM净利同比(稳健)；latest=最新报告期累计同比(灵敏)")
    ap.add_argument("--store", action="store_true", help="查看本地数据库状态")
    ap.add_argument("--history", metavar="ETF代码", help="查看该 ETF 的 PEG 历史序列")
    ap.add_argument("--basis", choices=["fixed", "pit"], default="fixed",
                    help="历史序列口径：fixed=最新持仓回溯(默认) / pit=当时已知持仓")
    ap.add_argument("--backfill", metavar="ETF代码",
                    help="回填该 ETF 的历史 PE/PEG（需配合 --days）")
    ap.add_argument("--days", type=int, default=180, help="回填天数，默认 180")
    ap.add_argument("--clear-history", metavar="ETF代码",
                    help="清空该 ETF 的历史快照（省略代码则清空全部）")
    ap.add_argument("--init-pe", action="store_true",
                    help="初始化：拉取全市场 A 股的历史 PE（约 2 小时，中断后重跑自动续传）")
    ap.add_argument("--init-hk", action="store_true",
                    help="初始化：拉取全市场港股的历史行情与财报（约 1 小时，可续传）")
    ap.add_argument("--init-valuation", action="store_true",
                    help="重拉全市场历史估值补齐 PB/PS/PCF 列（约 2 小时，可续传；"
                         "升级老库后执行一次，历史分位功能需要）")
    ap.add_argument("--init-div", action="store_true",
                    help="初始化：拉取全市场 A 股的历次现金分红（约 20 分钟，可续传；"
                         "股息率历史柱状图需要）")
    ap.add_argument("--sync-reports", action="store_true",
                    help="同步全市场券商研报的盈利预测（2015 年至今约 14.8 万篇，"
                         "首次 3~5 分钟；之后每次只补当年。成分股弹窗的历史预测靠它）")
    ap.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    from etf_peg import store

    if args.store:
        s = store.stats_all()          # A 股库 + 港股库
        print("本地数据库:", s["db_path"])
        print(f"  文件大小      : {s['db_size_mb']} MB")
        print(f"  财报          : {s['fin_rows']:,} 条 / {s['fin_periods']} 期"
              f"（其中 {s['fin_settled']} 期已定稿，永不重下）")
        print(f"    报告期      : {', '.join(s['fin_period_list'])}")
        print(f"  持仓          : {s['holdings_rows']:,} 条 / "
              f"{s['holdings_reports']} 份报告 / {s['holdings_etf']} 只 ETF")
        print(f"  个股每日 PE    : {s['stock_pe_rows']:,} 条 / {s['stock_pe_codes']} 只"
              + (f"（{s['stock_pe_range'][0]} ~ {s['stock_pe_range'][1]}）"
                 if s["stock_pe_range"] and s["stock_pe_range"][0] else "（暂无）"))
        cov = store.pe_coverage(ds.pe_history_earliest())
        print(f"  PE 初始化进度  : {cov['done']}/{cov['total']} 只已拉到 {ds.pe_history_earliest()}"
              + (f"（清单取自 {cov['snapshot_date']} 全市场快照）"
                 if cov["snapshot_date"] else "（尚无全市场快照）"))
        from etf_peg import hk as _hk
        hk_cov = store.HK.pe_coverage(_hk.HK_EARLIEST)
        print(f"  港股初始化进度  : {hk_cov['done']}/{hk_cov['total']} 只"
              f"（港股库 {s['hk']['db_size_mb']} MB，"
              f"{s['hk']['fin_rows']:,} 条财报 / {s['hk']['stock_pe_rows']:,} 条日行情）")
        print(f"  ETF 名录      : {s['etf_list_rows']} 只")
        print(f"  PEG 每日快照  : {s['daily_rows']:,} 条"
              + (f"（{s['daily_from']} ~ {s['daily_to']}）" if s["daily_from"] else "（暂无）"))
        return

    if args.history:
        rows = store.get_history(args.history, 365, args.basis)
        if not rows:
            print(f"{args.history} 暂无历史快照；分析过该 ETF 后会自动记录当天数据。")
            return
        print(f"{args.history} {rows[0]['name']}  共 {len(rows)} 个交易日快照"
              f"（{args.basis} 口径）\n")
        print(f"{'日期':<12}{'PE':>9}{'PE含亏损':>11}{'增速%':>10}{'等权增速%':>11}"
              f"{'PEG':>9}{'覆盖%':>8}{'持仓期':>13}")
        for r in rows:
            f = lambda v, d=2: "—" if v is None else f"{v:.{d}f}"    # noqa: E731
            print(f"{r['date']:<12}{f(r['pe']):>9}{f(r.get('pe_all')):>11}"
                  f"{f(r['g_ttm'], 1):>10}{f(r['g_all'], 1):>11}"
                  f"{f(r['peg'], 3):>9}{f(r['coverage'], 0):>8}"
                  f"{str(r['report_date'] or '—'):>13}")
        return

    if args.clear_history is not None or os.environ.get("_CLEAR_ALL"):
        code = args.clear_history
        n = store.clear_daily(code)
        print(f"已清空历史快照 {n} 条" + (f"（{code}）" if code else "（全部）"))
        if not args.backfill:
            return

    if args.init_pe:
        from etf_peg import store as _st
        cov = _st.pe_coverage(ds.pe_history_earliest())
        print("初始化：把全市场股票的每日 PE / 市值 / 收盘价拉进本地库")
        print(f"  当前覆盖：{cov['done']}/{cov['total']} 只，共 {cov['rows']:,} 行")
        print("  中断后重跑会自动续传；单只失败不影响整体\n")
        try:
            r = ds.init_all_stock_pe(
                on_progress=lambda done, total, label: _bar(
                    done / total * 100 if total else 0, label))
        except KeyboardInterrupt:
            print("\n\n已中断。已拉取的部分已入库，下次运行会继续。")
            return
        print("\n\n完成：本次新拉 %d 只，失败 %d 只，当前覆盖 %d/%d 只。"
              % (r["newly_done"], r["failed"],
                 r["coverage"]["done"], r["coverage"]["total"]))
        return

    if args.init_hk:
        from etf_peg import hk as _hk
        from etf_peg import store as _st
        cov = _st.HK.pe_coverage(_hk.HK_EARLIEST)
        print("初始化：把全市场港股的每日行情 / 市值与财报拉进港股库")
        print(f"  当前覆盖：{cov['done']}/{cov['total']} 只，共 {cov['rows']:,} 行")
        print("  中断后重跑会自动续传；单只失败不影响整体\n")
        try:
            r = _hk.init_all_hk(
                on_progress=lambda done, total, label: _bar(
                    done / total * 100 if total else 0, label))
        except KeyboardInterrupt:
            print("\n\n已中断。已拉取的部分已入库，下次运行会继续。")
            return
        print("\n\n完成：本次新拉 %d 只，失败 %d 只，当前覆盖 %d/%d 只。"
              % (r["newly_done"], r["failed"],
                 r["coverage"]["done"], r["coverage"]["total"]))
        return

    if args.init_valuation:
        from etf_peg import store as _st
        cov = _st.pb_pending(_st.known_pe_codes())
        print("重初始化：把存量历史估值补齐 PB / PS / PCF 列（历史分位功能需要）")
        print(f"  待重拉：{len(cov)} 只（全市场共享一份数据，约 1.5~2 小时）")
        print("  中断后重跑会自动续传；单只失败不影响整体\n")
        try:
            r = ds.init_all_stock_valuation(
                on_progress=lambda done, total, label: _bar(
                    done / total * 100 if total else 0, label))
        except KeyboardInterrupt:
            print("\n\n已中断。已重拉的部分已入库，下次运行会继续。")
            return
        print("\n\n完成：本次重拉 %d 只，失败 %d 只。"
              % (r["newly_done"], r["failed"]))
        return

    if args.init_div:
        from etf_peg import store as _st
        cov = _st.div_coverage()
        print("初始化：把全市场 A 股的历次现金分红拉进本地库（股息率历史柱状图需要）")
        print(f"  当前覆盖：{cov['done']}/{cov['total']} 只")
        print("  中断后重跑会自动续传；单只失败不影响整体\n")
        try:
            r = ds.init_all_stock_div(
                on_progress=lambda done, total, label: _bar(
                    done / total * 100 if total else 0, label))
        except KeyboardInterrupt:
            print("\n\n已中断。已拉取的部分已入库，下次运行会继续。")
            return
        print("\n\n完成：本次新拉 %d 只，失败 %d 只。"
              % (r["newly_done"], r["failed"]))
        return

    if args.sync_reports:
        from etf_peg import reports as _rp
        st0 = store.report_stats()
        print("同步：全市场券商研报的盈利预测（2015 年至今）")
        print(f"  库内现有：{st0['rows']:,} 篇 / {st0['stocks']:,} 只股票"
              + (f"（{st0['start']} ~ {st0['end']}）" if st0["rows"] else "（空）"))
        print("  往年拉过一次就不再回源，当年每次重拉；中断后重跑自动续传\n")
        last = {"seg": ""}

        def _prog(seg, done, total, new):
            if seg != last["seg"]:
                last["seg"] = seg
                print()
            _bar(done / total * 100 if total else 0,
                 f"{seg} 年 {done}/{total} 页，本次新增 {new} 篇")

        try:
            r = _rp.sync(on_progress=_prog)
        except KeyboardInterrupt:
            print("\n\n已中断。已拉取的部分已入库，下次运行会继续。")
            return
        print(f"\n\n完成：本次新增 {r['new']:,} 篇，"
              f"库内共 {r['rows']:,} 篇 / {r['stocks']:,} 只股票"
              f"（{r['start']} ~ {r['end']}）")
        return

    if args.backfill:
        code = args.backfill
        print(f"回填 {code} 近 {args.days} 天的历史 PE/PEG …\n")
        n = peg.backfill_etf(code, args.days, on_progress=_bar)
        # 注意：区间早已算满时 backfill_etf 会提前返回 len(dates)，
        # 这里的 n 是「区间内交易日数」而非「本次新增条数」，措辞不能用「写入」。
        print(f"\n\n完成，共 {n} 个交易日。\n")
        rows = store.get_history(code, args.days + 5)
        print(f"{'日期':<12}{'PE':>9}{'增速%':>10}{'PEG':>9}{'覆盖%':>8}{'有效':>9}{'持仓期':>13}")
        for r in rows:
            f = lambda v, d=2: "—" if v is None else f"{v:.{d}f}"      # noqa: E731
            print(f"{r['date']:<12}{f(r['pe']):>9}{f(r['g_ttm'], 1):>10}"
                  f"{f(r['peg'], 3):>9}{f(r['coverage'], 0):>8}"
                  f"{str(r['n_valid']) + '/' + str(r['n_total']):>9}"
                  f"{str(r['report_date'] or '—'):>13}")
        return

    if args.cli:
        run_cli(args.cli, args.mode)
        return

    # 启动服务前清掉上一次没退干净的实例，避免多进程抢数据库锁 / 端口
    _kill_stale_instances(args.port)
    start_server(host=args.host, port=args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
