function buildAiReport(d, hist, extra) {
  const mode = $('#mode').value;
  const gUsed = mode === 'ttm' ? d.g_ttm : d.g_latest;
  const gs = d.growth_series || [];
  // 历史给全量逐月（fixed 可回溯到 2018，约 106 个月）；
  // 最近一个月额外给逐日（见 7.1），更早的只给逐月，避免报告过长
  const months = hist.by_month || [];
  const now = new Date();
  const ymd = now.toISOString().slice(0, 10).replace(/-/g, '');
  const L = [];

  L.push(`# ${d.name}(${d.code}) 估值分析数据`);
  L.push('');
  L.push('> 以下为 ETF/基金 PEG 估值工具导出的结构化数据，供 AI 做估值分析。');
  L.push('> 请先读「数据口径与局限」一节再解读数字。');
  L.push('');

  /* ---- 1. 基本信息 ---- */
  L.push('## 一、基本信息');
  L.push('');
  const info = [
    ['名称', d.name], ['代码', d.code], ['基金类型', d.fund_type],
    ['股票仓位', d.equity_ratio != null ? fmt(d.equity_ratio, 1) + '%' : null],
    ['A股/港股/其他', `${d.n_a} / ${d.n_hk} / ${d.n_oversea} 只`],
    ['持仓完整性', d.holdings_full === false ? '仅季报前十大重仓（样本不完整）' : '全持仓（半年报/年报）'],
  ];
  if (d.oversea_weight) {
    info.push(['其他市场权重', fmt(d.oversea_weight, 2) + '%（美股等，无估值数据，未计入）']);
  }
  if (d.n_hk) {
    info.push(['港股权重', d.hk_weight != null ? fmt(d.hk_weight, 2) + '%' : null]);
    info.push(['港股口径', 'PE 由 当日总市值 ÷ TTM股东应占溢利 自算，财报为半年度']);
  }
  const fbInfo = d.fin_basis || {};
  info.push(
    ['持仓报告期', d.report_date], ['最新财报期', d.fin_period],
    ['财报口径', fbInfo.pe_mode === 'uniform'
      ? `统一口径：全部成分股停在第 ${fbInfo.uniform || d.fin_period} 期（已定稿），缺该期的成分股未计入`
      : '混合口径：各成分股用自己最新已披露的报告期'],
    ['PE 数据日', d.pe_date], ['增速口径', mode === 'ttm'
      ? 'TTM 全口径加权（Σ(w·净利) 滚动12个月同比，含亏损/负增长股）'
      : '最新报告期累计同比（Σ(w·净利)，含亏损/负增长股）'],
    ['生成时间', now.toLocaleString('zh-CN')]);
  if (extra && extra.mcap != null) info.push(['基金规模', fmt(extra.mcap / 1e8, 1) + ' 亿元']);
  if (extra && extra.pctDay != null) info.push(['当日涨跌', fmt(extra.pctDay, 2) + '%']);
  if (d.feeder_from) {
    info.push(['穿透来源', `本结果由联接基金 ${d.feeder_from.name}（${d.feeder_from.code}）自动穿透而来`]);
  }
  info.forEach(([k, v]) => L.push(`- ${k}：${v == null || v === '' ? '—' : v}`));
  L.push('');

  /* ---- 2. 核心结论 ---- */
  L.push('## 二、核心结论');
  L.push('');
  L.push('| 指标 | 数值 | 说明 |');
  L.push('|---|---|---|');
  const rows = [
    ['PEG（整体法）', fmt(d.peg, 3), 'PE ÷ 加权增速；增速≤0 时为空'],
    ['PEG（扣非口径）', fmt(d.deduct_peg, 3),
      '扣非PE ÷ 扣非加权增速；扣非增速≤0 时为空（亏损周期常见）'],
    ['PE-TTM（整体法）', fmt(d.pe, 2), 'Σ持仓市值 ÷ Σ归母净利润'],
    ['扣非PE（整体法）', fmt(d.deduct_pe, 2),
      'Σw / Σ(w·扣非TTM/市值)，只含扣非TTM>0 的股票'],
    ['归母净利增速（TTM）', fmt(d.g_ttm, 2) + '%',
      '全口径加权（含亏损/负增长股）：Σ(w·净利) 的滚动12个月同比'],
    ['扣非增速（TTM·加权）', fmt(d.g_deduct_ttm, 2) + '%',
      'Σ(w·扣非TTM) 同比，全口径含亏损/负增长股；港股/缺扣非用归母顶上'],
    ['归母净利增速（最新报告期累计）', fmt(d.g_latest, 2) + '%',
      '全口径加权（含亏损/负增长股）：Σ(w·净利) 的本期累计同比'],
    ['等权增速（TTM·对照）', fmt(d.g_all, 2) + '%',
      d.g_all != null && gUsed != null
        ? `简单相加（不乘权重）；与加权增速差 ${(gUsed - d.g_all).toFixed(1)}pct，即持仓集中度的影响`
        : '简单相加（不乘权重）'],
    ...(d.np_ttm_sum != null ? [['归母净利（全口径加权）',
      fmt(d.np_ttm_sum / 1e8, 2) + ' 亿元',
      'Σ(w·归母TTM)，TTM 滚动 12 个月；不是当季/当月利润']] : []),
    ...(d.deduct_ttm_sum != null ? [['扣非净利（全口径加权）',
      fmt(d.deduct_ttm_sum / 1e8, 2) + ' 亿元',
      'Σ(w·扣非TTM)，与归母同一组报告期；港股/缺扣非用归母顶上']] : []),
    ['PEG（中位数法）', fmt(d.peg_median, 3), 'PE中位数 ÷ 增速中位数（仅正增长样本），抗极端值'],
    ['PEG（权重加权平均）', fmt(d.peg_weighted_avg, 3), '成分股 PEG 加权平均，易被极端值带偏'],
    ['有效样本覆盖率', fmt(d.coverage, 1) + '%', `${d.n_valid}/${d.n_total} 只（仅用于 PEG 与中位数）`],
    ['有效样本', `${d.n_valid} / ${d.n_total}`, 'PE>0 且正增长的成分股（不用于组合增速）'],
    ...(d.pb != null ? [['PB（整体法）', fmt(d.pb, 2),
      `Σw/Σ(w/PB)${d.pb_coverage != null && d.pb_coverage < 100 ? `，样本覆盖 ${fmt(d.pb_coverage, 0)}%` : ''}；周期底部 PE 失真时的主要参照`]] : []),
    ...(d.ps != null ? [['PS（市销率·TTM）', fmt(d.ps, 2),
      `市值÷TTM营收${d.ps_coverage != null && d.ps_coverage < 100 ? `，样本覆盖 ${fmt(d.ps_coverage, 0)}%` : ''}`]] : []),
    ...(d.pcf != null ? [['PCF（市现率·TTM）', fmt(d.pcf, 2), '市值÷TTM经营现金流，A 股 EV 的替代口径']] : []),
    ...(d.ev_ebit != null ? [['EV/EBIT（近似）', fmt(d.ev_ebit, 2), '仅港股成分：EV=市值+总负债−现金，EBIT=TTM营业利润']] : []),
    ...(d.div_yield != null ? [['股息率（加权）', fmt(d.div_yield, 2) + '%', '近一年分红÷现价，权重加权']] : []),
    ...(d.deduct_ttm_sum != null ? [['扣非净利（全口径加权）', fmt(d.deduct_ttm_sum / 1e8, 2) + ' 亿元',
      'Σ(持仓权重×成分股 TTM 扣非归母净利润)，含亏损股与负增长股；TTM 滚动 12 个月，不是单季/单月利润']] : []),
    ...(d.pe_equal != null ? [['等权 PE', fmt(d.pe_equal, 2), '有效样本算术平均，与整体法差距大=权重股拉偏']] : []),
    ...(d.peg_all != null ? [['等权 PEG（对照）', fmt(d.peg_all, 3),
      '同一 PE ÷ 等权增速；与加权 PEG 的差异来自持仓集中度，不是样本剔除']] : []),
    ...((d.percentile && Object.keys(d.percentile).length) ? Object.entries(d.percentile).map(([k, v]) => [
      `${{pe: 'PE', pb: 'PB', ps: 'PS'}[k] || k} 历史分位`,
      `全历史 ${v.all != null ? fmt(v.all, 0) + '%' : '—'} / 5年 ${v.y5 != null ? fmt(v.y5, 0) + '%' : '—'} / 3年 ${v.y3 != null ? fmt(v.y3, 0) + '%' : '—'}`,
      '基于每日快照序列；低分位=相对自身历史偏便宜',
    ]) : []),
    ...(d.forward && d.forward.pe_f != null ? [
      [`前瞻 PE（${d.forward.y1}E）`, fmt(d.forward.pe_f, 2), '市值 ÷ 机构一致预期净利，仅 A 股成分'],
      [`预期净利 CAGR（${d.forward.y1}→${d.forward.y3}）`, fmt(d.forward.cagr, 1) + '%', '一致预期净利润两年复合'],
      ['前瞻 PEG', fmt(d.forward.peg_f, 3), '前瞻 PE ÷ CAGR，与 TTM 口径 PEG 对照'],
      ['一致预期覆盖', fmt(d.forward.cons_weight, 0) + '%', `有预测数据的 A 股权重占比（${d.forward.n_used} 只）`],
    ] : []),
    ...(d.erp && d.erp.value != null ? [
      ['盈利收益率（1/PE）', fmt(100 / d.pe, 2) + '%', '整体法 PE 的倒数'],
      ['10Y 国债收益率', fmt(d.erp.y10, 2) + '%', `无风险利率（${d.erp.y10_date || '—'}）`],
      ['股权风险溢价 ERP', fmt(d.erp.value, 2) + '%', '盈利收益率 − 无风险利率，越高=股相对债越便宜'],
      ...(d.erp.pct != null ? [['ERP 历史分位', fmt(d.erp.pct, 0) + '%', '当前 ERP 高于历史上多少比例的交易日']] : []),
    ] : []),
  ];
  rows.forEach(([k, v, n]) => L.push(`| ${k} | ${v} | ${n} |`));
  L.push('');

  /* ---- 2.1 成分股分组统计 ---- */
  if (d.dist && d.dist.profit) {
    L.push('### 成分股分组统计（盈利/亏损 × 正/负增长）');
    L.push('');
    L.push('| 分组 | 只数 | 权重% | PE中位数 | PE调和均值 | 增速中位数% |');
    L.push('|---|---|---|---|---|---|');
    const dv = d.dist;
    L.push(`| 盈利股（净利>0） | ${dv.profit.n} | ${fmt(dv.profit.weight, 2)} | ${fmt(dv.profit.pe_med, 1)} | ${fmt(dv.profit.pe_harm, 1)} | — |`);
    L.push(`| 亏损股（净利≤0） | ${dv.loss.n} | ${fmt(dv.loss.weight, 2)} | — | — | — |`);
    L.push(`| 正增长股 | ${dv.growth_pos.n} | ${fmt(dv.growth_pos.weight, 2)} | — | — | ${fmt(dv.growth_pos.g_med, 1)} |`);
    L.push(`| 负增长股 | ${dv.growth_neg.n} | ${fmt(dv.growth_neg.weight, 2)} | — | — | ${fmt(dv.growth_neg.g_med, 1)} |`);
    L.push('');
  }

  /* ---- 2.2 TTM 增速构成 ---- */
  const fb = d.fin_basis;
  if (fb && fb.parts && fb.parts.length) {
    L.push('### TTM 增速构成（计算依据）');
    L.push('');
    L.push('| 报告期 | 报告 | 角色 |');
    L.push('|---|---|---|');
    fb.parts.forEach(p => L.push(`| ${p.period} | ${p.label} | ${p.role} |`));
    if (fb.pit && fb.dist && fb.dist.length > 1) {
      const tot = fb.dist.reduce((s, x) => s + x[1], 0) || 1;
      L.push('');
      L.push('> 各公司披露进度不同，基准期按真实披露日逐只对齐（PIT）。实际基准期分布：'
        + fb.dist.map(x => `${x[0]} × ${x[1]} 家（${(x[1] / tot * 100).toFixed(0)}%）`).join('、') + '。');
    }
    L.push('');
  }

  /* ---- 3. 近五年财报摘要 ---- */
  const fin = gs.slice(0, 20);
  if (fin.length) {
    L.push('## 三、近五年财报摘要（组合层面，绝对量 + 增速）');
    L.push('');
    L.push('> 净利、同比、环比均为**全口径 + 持仓权重加权**（亏损股、负增长股都计入）；「正增长样本同比」仅为对照。');
    L.push('');
    L.push('| 报告期 | 类型 | 加权TTM归母净利(亿元) | 去年同期(亿元) | 加权同比% | TTM环比% | 单季环比% | 正增长样本同比% | 样本 |');
    L.push('|---|---|---|---|---|---|---|---|---|---|');
    fin.forEach(s => L.push(
      `| ${s.period} | ${s.label} | ${s.np_now != null ? fmt(s.np_now / 1e8, 1) : '—'}`
      + ` | ${s.np_prev != null ? fmt(s.np_prev / 1e8, 1) : '—'}`
      + ` | ${fmt(s.g, 2)} | ${fmt(s.qoq_ttm, 2)} | ${fmt(s.qoq_single, 2)}`
      + ` | ${fmt(s.g_valid, 2)} | ${s.vused}/${s.used} |`));
    L.push('');
  }

  /* ---- 4. 增速序列 ---- */
  if (gs.length) {
    L.push('## 四、组合增速序列（近 ' + gs.length + ' 个报告期）');
    L.push('');
    L.push('| 报告期 | 类型 | 加权同比% | TTM环比% | 单季环比% | 正增长样本同比% | 样本 |');
    L.push('|---|---|---|---|---|---|---|');
    gs.forEach(s => L.push(`| ${s.period} | ${s.label} | ${fmt(s.g, 2)} | ${fmt(s.qoq_ttm, 2)} | ${fmt(s.qoq_single, 2)} | ${fmt(s.g_valid, 2)} | ${s.vused}/${s.used} |`));
    L.push('');
  }

  /* ---- 5. 当前持仓 ---- */
  const N = 100;
  const list = (d.stocks || []).slice(0, N);
  const rest = (d.stocks || []).slice(N);
  const restW = rest.reduce((a, s) => a + (s.weight || 0), 0);
  if (list.length) {
    L.push(`## 五、当前持仓（按权重降序，前 ${list.length} 只）`);
    L.push('');
    L.push('| 代码 | 名称 | 市场 | 权重% | 股价 | PE-TTM | 增速% | PEG | 财报期 | 状态 |');
    L.push('|---|---|---|---|---|---|---|---|---|---|');
    list.forEach(s => L.push(
      `| ${s.code} | ${s.name} | ${mktTag(s)} | ${fmt(s.weight, 2)}`
      + ` | ${fmt(s.price, 2)} | ${fmt(s.pe_ttm, 2)}`
      + ` | ${fmt(mode === 'ttm' ? s.g_ttm : s.g_latest, 2)} | ${fmt(s.peg, 3)}`
      + ` | ${s.fin_period || '—'} | ${STATUS_CN[s.status] || s.status} |`));
    if (rest.length) {
      L.push(`| — | 其余 ${rest.length} 只 | — | ${fmt(restW, 2)} | — | — | — | — | — | 未单列 |`);
    }
    L.push('');
  }

  /* ---- 5.1 前二十大持仓个股逐期增速 ---- */
  const top20 = (d.stocks || []).slice(0, 20);
  const bases = (d.fin_basis && d.fin_basis.series_bases) || [];
  const withSeries = top20.filter(s => s.g_series && s.g_series.length);
  if (withSeries.length && bases.length) {
    L.push('### 前二十大持仓个股逐期增速（TTM 归母净利同比%，由新到旧）');
    L.push('');
    L.push('| 代码 | 名称 | 权重% | ' + bases.join(' | ') + ' |');
    L.push('|---|'.repeat(3 + bases.length));
    withSeries.forEach(s => L.push(
      `| ${s.code} | ${s.name} | ${fmt(s.weight, 2)} | `
      + bases.map((_, i) => s.g_series[i] == null ? '—' : s.g_series[i].toFixed(1) + '%').join(' | ')
      + ' |'));
    L.push('');
    L.push('> 期别与「组合增速序列」一致，空值=该期无 TTM 数据（港股半年度财报或缺失）。');
    L.push('');

    const withQoqT = top20.filter(s => s.qoq_ttm_series && s.qoq_ttm_series.length);
    if (withQoqT.length) {
      L.push('### 前二十大持仓个股逐期 TTM 环比（%，本期TTM净利 vs 上一期）');
      L.push('');
      L.push('| 代码 | 名称 | 权重% | ' + bases.join(' | ') + ' |');
      L.push('|---|'.repeat(3 + bases.length));
      withQoqT.forEach(s => L.push(
        `| ${s.code} | ${s.name} | ${fmt(s.weight, 2)} | `
        + bases.map((_, i) => s.qoq_ttm_series[i] == null ? '—' : s.qoq_ttm_series[i].toFixed(1) + '%').join(' | ')
        + ' |'));
      L.push('');
    }

    const withQoqS = top20.filter(s => s.qoq_single_series && s.qoq_single_series.length);
    if (withQoqS.length) {
      L.push('### 前二十大持仓个股逐期单季环比（%，单季归母净利 vs 上一单季，仅 A 股成分）');
      L.push('');
      L.push('| 代码 | 名称 | 权重% | ' + bases.join(' | ') + ' |');
      L.push('|---|'.repeat(3 + bases.length));
      withQoqS.forEach(s => L.push(
        `| ${s.code} | ${s.name} | ${fmt(s.weight, 2)} | `
        + bases.map((_, i) => s.qoq_single_series[i] == null ? '—' : s.qoq_single_series[i].toFixed(1) + '%').join(' | ')
        + ' |'));
      L.push('');
    }
  }

  /* ---- 5.2 前二十大持仓个股逐期财务明细 ---- */
  const finTop = (d.stocks || []).slice(0, 20);
  const withFin = finTop.filter(s => s.fin_series && s.fin_series.some(r => r));
  if (withFin.length && bases.length) {
    L.push('### 前二十大持仓个股逐期财务明细（累计口径）');
    L.push('');
    L.push('> 期别与「组合增速序列」一致，由新到旧；归母净利/营收为报告期**累计值**，同比为**累计同比**，与组合层的 TTM 增速口径不同。空行=该期无数据（港股半年度财报或未披露）。');
    L.push('');
    withFin.forEach(s => {
      L.push(`**${s.name}（${s.code}）**｜权重 ${fmt(s.weight, 2)}%`);
      L.push('');
      L.push('| 期别 | 归母净利(亿元) | 同比增速% | 营收(亿元) | EPS | ROE% | 股息率% |');
      L.push('|---|---|---|---|---|---|---|');
      bases.forEach((b, i) => {
        const r = s.fin_series[i];
        if (!r) return;
        L.push(`| ${b} | ${r.np != null ? fmt(r.np / 1e8, 1) : '—'}`
          + ` | ${r.np_yoy != null ? fmt(r.np_yoy, 1) + '%' : '—'}`
          + ` | ${r.revenue != null ? fmt(r.revenue / 1e8, 1) : '—'}`
          + ` | ${r.eps != null ? fmt(r.eps, 2) : '—'}`
          + ` | ${r.roe != null ? fmt(r.roe, 1) + '%' : '—'}`
          + ` | ${r.div_yield != null ? fmt(r.div_yield, 1) + '%' : '—'} |`);
      });
      L.push('');
    });
  }

  /* ---- 5.3 前二十大成分股：机构历史预测（逐机构、逐次，不聚合） ---- */
  const panel = (extra && extra.panel) || [];
  if (panel.length) {
    const wMap = {};
    (d.stocks || []).forEach(s => { wMap[s.code] = s.weight; });

    L.push('### 前二十大成分股：机构历史预测（逐机构）');
    L.push('');
    L.push('> 按机构分组，每家列出**最近 4 篇**（近 36 个月）研报的预测，组内按日期倒序；'
      + '机构按最新研报时间排序。括号内是该机构相对**它自己上一篇**对同一财年的变化 ——'
      + '不是和别人的均值比。看某一家就能判断它是在持续上调、还是连续下修。');
    L.push('');
    panel.forEach(p => {
      const ys = p.years || [];
      const w = wMap[p.code];
      L.push(`#### ${p.code} ${p.name}`
        + (w != null ? ` ｜ 权重 ${fmt(w, 2)}%` : '')
        + ` ｜ 近 36 个月有研报的 ${p.n_org} 家机构（库内共 ${p.total} 篇）`);
      L.push('');
      if (!ys.length) {
        L.push('（这些研报都没有给出分年度 EPS 预测）');
        L.push('');
        return;
      }
      L.push('| 机构 | 日期 | 评级 | 目标价 | '
        + ys.map(y => y + 'E').join(' | ') + ' |');
      L.push('|' + '---|'.repeat(4 + ys.length));
      (p.orgs || []).forEach(o => {
        (o.rows || []).forEach((r, i) => {
          const cells = ys.map(y => {
            const v = (r.e || {})[String(y)];
            if (v == null) return '—';
            const c = (r.chg || {})[String(y)];
            let tag = '';
            if (c != null) {
              tag = c > 0.5 ? ` (↑${c}%)`
                : (c < -0.5 ? ` (↓${Math.abs(c)}%)` : ' (持平)');
            }
            return fmt(Number(v), 2) + tag;
          });
          // 同一机构的多行只首行写名字，其余用「〃」，否则整列都在重复
          L.push(`| ${i === 0 ? o.name : '〃'} | ${r.d} | ${r.r || '—'}`
            + ` | ${r.t == null ? '—' : fmt(r.t, 2)} | ${cells.join(' | ')} |`);
        });
      });
      L.push('');
    });
    L.push('> 港股/美股没有免费的逐篇研报源，只有 A 股成分参与。'
      + '「—」表示该机构未对相应财年给出预测，「〃」表示与上一行同一家机构。');
    L.push('');
  }

  /* ---- 5.5 历史股价 / 净值（K 线） ---- */
  const kline = extra && extra.kline;
  if (kline && kline.rows && kline.rows.length >= 2) {
    const isFundK = kline.kind === 'fund';
    const unitName = isFundK ? '净值' : '股价';
    const kDec = isFundK ? 4 : 3;
    const rowsK = kline.rows;
    const lastK = rowsK[rowsK.length - 1];
    const firstK = rowsK[0];
    const lastInfo = kline.last || {};
    const loK = Math.min(...rowsK.map(r => r.l));
    const hiK = Math.max(...rowsK.map(r => r.h));

    L.push(`## 六、历史${unitName}（K 线）`);
    L.push('');
    L.push(`- 最新${unitName}：${lastInfo.price != null ? Number(lastInfo.price).toFixed(kDec) : (lastK.c != null ? lastK.c.toFixed(kDec) : '—')}`);
    L.push(`- 最新交易日：${lastInfo.date || lastK.date}`);
    L.push(`- 当日涨跌：${lastInfo.pct != null ? (lastInfo.pct > 0 ? '+' : '') + lastInfo.pct.toFixed(2) + '%' : '—'}`);
    if (isFundK && d.est_pct && d.est_pct.pct != null) {
      L.push(`- 当天预测：${d.est_pct.pct > 0 ? '+' : ''}${d.est_pct.pct.toFixed(2)}%`
        + `（按最新持仓成分股当日涨跌加权估算，覆盖 ${d.est_pct.coverage != null ? d.est_pct.coverage.toFixed(0) : '—'}% 权重）`);
    }
    L.push(`- 历史区间（${firstK.date} ~ ${lastK.date}）：最高 ${hiK.toFixed(kDec)} / 最低 ${loK.toFixed(kDec)}`);
    L.push(`- 口径：${isFundK ? '单位净值（合成 K 线，o=前收/c=今收，周/月取首开末收+区间高低）' : '前复权日 K（周/月由日K聚合）'}`);
    L.push('');

    // 周/月聚合（复用 drawKline 的 weekStart）
    const wkMap = new Map(), moMap = new Map();
    for (const r of rowsK) {
      const wk = weekStart(r.date), mo = r.date.slice(0, 7);
      for (const [m, k] of [[wkMap, wk], [moMap, mo]]) {
        const b = m.get(k);
        if (!b) m.set(k, { key: k, date: r.date, o: r.o, h: r.h, l: r.l, c: r.c });
        else { b.date = r.date; b.h = Math.max(b.h, r.h); b.l = Math.min(b.l, r.l); b.c = r.c; }
      }
    }
    const weeks = [...wkMap.values()], monthsK = [...moMap.values()];
    const weeksN = weeks.slice(-52), monthsN = monthsK.slice(-36);
    const pctOf = b => b.o ? (b.c - b.o) / b.o * 100 : null;
    const pctStr = v => v == null ? '—' : (v > 0 ? '+' : '') + v.toFixed(2);

    L.push(`### 近 ${weeksN.length} 周收盘序列（周K，由新到旧）`);
    L.push('');
    L.push('| 周（周一） | 收盘 | 周涨跌% |');
    L.push('|---|---|---|');
    [...weeksN].reverse().forEach(w => L.push(`| ${w.key} | ${w.c.toFixed(kDec)} | ${pctStr(pctOf(w))} |`));
    L.push('');

    L.push(`### 近 ${monthsN.length} 月收盘序列（月K，由新到旧）`);
    L.push('');
    L.push('| 月份 | 收盘 | 月涨跌% |');
    L.push('|---|---|---|');
    [...monthsN].reverse().forEach(m => L.push(`| ${m.key} | ${m.c.toFixed(kDec)} | ${pctStr(pctOf(m))} |`));
    L.push('');
  }

  /* ---- 6. 历史估值：最近一月逐日 + 更早逐月 ---- */
  L.push('## 七、历史估值');
  L.push('');
  // 7.1 最近一个月逐日（约 30 自然日内的每个交易日）
  const cutoff = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
  const recentDaily = (hist.daily || []).filter(r => r.date >= cutoff).reverse();
  if (recentDaily.length) {
    L.push(`### 7.1 最近一个月逐日（${recentDaily[0].date} ~ `
      + `${recentDaily[recentDaily.length - 1].date}，${recentDaily.length} 个交易日）`);
    L.push('');
    L.push('| 日期 | PE | 增速% | PEG | 扣非PE | 扣非增速% | 扣非PEG | 归母净利(亿) | 扣非净利(亿) | 覆盖率% |');
    L.push('|---|---|---|---|---|---|---|---|---|---|');
    recentDaily.forEach(r => L.push(
      `| ${r.date} | ${fmt(r.pe, 2)} | ${fmt(r.g_ttm, 2)} | ${fmt(r.peg, 3)}`
      + ` | ${fmt(r.deduct_pe, 2)} | ${fmt(r.g_deduct, 2)} | ${fmt(r.deduct_peg, 3)}`
      + ` | ${r.np_ttm != null ? fmt(r.np_ttm / 1e8, 2) : '—'}`
      + ` | ${r.deduct_ttm != null ? fmt(r.deduct_ttm / 1e8, 2) : '—'}`
      + ` | ${fmt(r.coverage, 1)} |`));
    L.push('');
    L.push('> 逐日为当日收盘后的估值快照，TTM 口径（不是当日利润）。'
      + '一个月之前的历史只给逐月值（7.2），更细的日线请在工具界面查看。');
    L.push('');
  }
  if (months.length) {
    L.push(`### 7.2 逐月（全部历史，${months.length} 个月）`);
    L.push('');
    L.push('| 月末 | PE-TTM | PE区间 | 增速% | PEG | PEG区间 | 扣非PE | 扣非增速% | 扣非PEG | PB | PS | 归母净利(亿) | 扣非净利(亿) | 股息率% | 覆盖率% |');
    L.push('|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|');
    months.forEach(m => L.push(
      `| ${m.date} | ${fmt(m.pe, 2)}`
      + ` | ${fmt(m.pe_lo, 2)}~${fmt(m.pe_hi, 2)}`
      + ` | ${fmt(m.g_ttm, 2)} | ${fmt(m.peg, 3)}`
      + ` | ${m.peg_lo != null ? fmt(m.peg_lo, 3) + '~' + fmt(m.peg_hi, 3) : '—'}`
      + ` | ${fmt(m.deduct_pe, 2)} | ${fmt(m.g_deduct, 2)} | ${fmt(m.deduct_peg, 3)}`
      + ` | ${fmt(m.pb, 2)} | ${fmt(m.ps, 2)}`
      + ` | ${m.np_ttm != null ? fmt(m.np_ttm / 1e8, 1) : '—'}`
      + ` | ${m.deduct_ttm != null ? fmt(m.deduct_ttm / 1e8, 1) : '—'}`
      + ` | ${fmt(m.div_yield, 2)}`
      + ` | ${fmt(m.coverage, 1)} |`));
    L.push('');
    L.push('> 表中「增速%」为**全口径 + 持仓权重加权**：Σ(w·TTM归母净利) 的同比，'
      + '亏损股与负增长股都计入；PEG 只在增速 &gt; 0 时有值，为空表示增速非正。');
    L.push('> 「扣非增速%」= Σ(w·扣非TTM) 同比（全口径加权）；「扣非PE」只含扣非TTM&gt;0 的股票；'
      + '「扣非PEG」= 扣非PE ÷ 扣非增速，扣非增速 ≤ 0 时为空。');
    L.push('> 表中「归母净利(亿)」与「扣非净利(亿)」均为**全口径 + 持仓权重加权**，'
      + '取该月末最后一个交易日，TTM 滚动 12 个月，**不是当月利润**；港股/缺扣非用归母顶上。');
    if (months.length < 12) {
      L.push('');
      L.push(`> 注意：本基金当前仅缓存 ${months.length} 个月的月度估值，`
        + `在工具界面执行「回填历史」可覆盖 2018 年以来的全部区间。`);
    }
  } else {
    L.push('> 该基金暂无历史月度数据。在工具界面执行「回填历史」后，'
      + '这里会给出 2018 年以来逐月的 PE / 增速 / PEG / 扣非净利 / 股息率。');
  }
  L.push('');

  /* ---- 8. 口径与局限 ---- */
  L.push('## 八、数据口径与局限（解读前必读）');
  L.push('');
  L.push('### 口径');
  L.push('');
  L.push('- PE 为**整体法**：PE = Σ持股权重 / Σ(持股权重 ÷ PE_i)（按持仓权重加权的调和平均），不是成分股 PE 的简单平均，也非中位数');
  L.push('- **增速为全口径 + 持仓权重加权**：`g = Σ(w·np) / Σ(w·np_prev) − 1`，'
    + '亏损股、负增长股全部计入，不做任何样本筛选；净利润是绝对金额，必须乘权重。'
    + '旧口径（只留 PE>0 且正增长）会系统性抬高增速——013817 实测正增长样本 +26% vs 全样本等权 -108%');
  L.push('- 表中「等权增速」= 简单相加（不乘权重），与加权增速的差额反映**持仓集中度**（大权重股主导），不是样本剔除');
  L.push('- 「有效样本」= PE>0 且正增长的成分股，**只用于 PEG 的成立条件与中位数/加权平均 PEG**，不用于组合增速；'
    + 'PEG 只在加权增速 > 0 时有值，增速 ≤ 0 时留空');
  L.push('- **扣非净利(亿) = 全口径 + 持仓权重加权**：`Σ(持仓权重% ÷ 100 × 成分股 TTM 扣非归母净利润)`。'
    + '亏损股、负增长股全部计入，不做任何样本筛选；净利润是绝对金额，必须乘权重（简单相加等于假设每只都 100% 持有）。'
    + 'TTM 为滚动 12 个月，季/月柱取该桶最后一个交易日，**不是当季/当月单期利润**；'
    + '港股不披露扣非、A 股某期缺扣非时用归母净利润顶上；'
    + '与成分股明细的「扣非TTM(亿)」（逐股 100% 口径）不可横向相加对比');
  L.push('- **归母净利(亿)** = `Σ(w·TTM归母净利)`，与扣非净利完全同一套加权规则，可直接相减得到非经常性损益');
  L.push('- **扣非 PEG 是与归母 PEG 并列的独立体系**，不替代主指标：'
    + '`扣非PE = Σw/Σ(w·扣非TTM/市值)`（只含扣非TTM>0 的股票），'
    + '`扣非增速 = Σ(w·扣非TTM)/Σ(w·上年扣非TTM)−1`（全口径加权），'
    + '`扣非PEG = 扣非PE ÷ 扣非增速`（增速≤0 时为空）。'
    + '**分工**：制造业/周期股看扣非（剔除政府补助、资产处置等一次性损益）；'
    + '金融股与港股看归母（港股无扣非口径）。两者背离时（归母 PEG 便宜但扣非 PEG 缺失或很高）'
    + '说明利润可能靠非经常性项目撑着');
  L.push('- 扣非 PE 与扣非增速的样本天然不同：增速含负扣非股，PE 只含正扣非股——'
    + '因此在亏损周期里「有扣非增速、没有扣非PE/PEG」是正常现象，不是数据缺失');
  if (fbInfo.pe_mode === 'uniform') {
    L.push(`- 财报为**统一口径**：全组合强制停在第 ${fbInfo.uniform || d.fin_period} 期`
      + '（已过法定披露截止日的定稿财报），缺该期的成分股直接不参与计算；'
      + '**不是**逐只按最新披露日对齐，因此披露季内不会混入部分公司的三季报');
  } else {
    L.push('- 个股财报按**真实披露日**对齐（point-in-time），避免用到尚未公开的数据；'
      + '各成分股分别用自己最新已披露的报告期（混合口径）');
  }
  L.push('- **成分股盈利预测**来自券商研报（东财研报中心，2017 年起约 14.8 万篇）：'
    + '报告正文给的是逐机构最新一篇的原始预测与各家自身的修正幅度，**不做平均聚合**；'
    + '港股/美股没有免费的逐篇研报源，不参与');
  L.push('- A 股 PE 为东财 PE-TTM 口径；**港股无现成历史 PE，由 当日总市值 ÷ TTM股东应占溢利 自算**，且港股财报为半年度（中报/年报），更新节奏比 A 股慢半年');
  L.push('- **历史序列为「最新持仓回溯」口径（fixed）**：全部历史统一用最新一期持仓权重，'
    + '财报/行情仍按披露日 PIT。因此「归母净利(亿)」柱子之间可直接比较，'
    + '增速分母 = 同一套持仓的上年同期 TTM（柱子 ÷ 分母 = 增速，三者自洽）。'
    + '**代价是持仓有前视**（如 2022 年那套组合并非当时实际持有），'
    + '因此本序列适合判断「当前这篮子股票历史上贵不贵」，**不适合回测收益**；'
    + '复盘当时市场看到的估值请切换到 PIT 口径（每期用当时持仓，无前视，但柱子含调仓影响）');
  L.push('- 月度估值的数值取该月**最后一个交易日**，区间为该月最高/最低');
  L.push('- 月度股息率为该月末时点的组合加权股息率：A 股 = 近 12 个月累计每股分红 ÷ 当日股价（分红明细滚动窗口），港股 = 财报近一年每股分红 ÷ 当日收盘价；亏损/负增长股照样分红，不因其被 PE 剔除而连带剔除');
  L.push('- 月度 PB/PS 与 PE 同源逐日计算；**周期性行业（有色/光伏/新能源等）判断底部与顶部，PB 分位比 PE/PEG 可靠**——PE 在盈利高峰会失真地低、在盈利谷底会失真地高');
  L.push('- PEG = PE ÷ 增速。PEG<1 只是传统经验值，需结合增速可持续性、行业景气与上述偏差综合判断');
  L.push('');
  L.push('### 局限');
  L.push('');
  L.push('- 持仓来自**定期报告**（季报前十大 / 半年报与年报全持仓），非实时，滞后最长可达一个季度以上；主动型基金季度间可能大幅调仓');
  L.push('- PE-TTM 数据滞后 1~2 个交易日');
  if (d.warn) {
    L.push('- 本基金特有的提示：' + d.warn);
  }
  L.push('- 本数据仅为估值参考，不构成任何投资建议');
  L.push('');
  return L.join('\n');
}