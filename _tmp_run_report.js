const fs = require('fs');

const $ = () => ({ value: 'ttm' });
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v)))
  ? '—' : Number(v).toFixed(d);
const mktTag = () => 'A';
const STATUS_CN = {};
function weekStart(s) {
  const d = new Date(s + 'T00:00:00');
  const day = (d.getDay() + 6) % 7;   // 周一=0
  d.setDate(d.getDate() - day);
  return d.toISOString().slice(0, 10);
}

eval(fs.readFileSync('d:/ETF_Analysis/ETF_Analysis/_tmp_build.js', 'utf8'));

const raw = JSON.parse(fs.readFileSync('d:/ETF_Analysis/ETF_Analysis/_tmp_report_data.json', 'utf8'));
const kline = JSON.parse(fs.readFileSync('d:/ETF_Analysis/ETF_Analysis/_tmp_kline.json', 'utf8'));
const text = buildAiReport(raw.d, raw.hist, {
  mcap: raw.d.market_cap, pctDay: -2.34, kline: kline, panel: [],
});
fs.writeFileSync('d:/ETF_Analysis/ETF_Analysis/_tmp_report.md', text, 'utf8');

const lines = text.split('\n');
console.log('总行数:', lines.length, '总字符:', text.length);

// 按 ## / ### 标题切段统计
let cur = { title: '(前言)', n: 0 };
const segs = [cur];
for (const ln of lines) {
  if (/^###? /.test(ln)) {
    cur = { title: ln.replace(/^#+\s*/, ''), n: 0 };
    segs.push(cur);
  }
  cur.n += 1;
}
for (const s of segs) {
  console.log(`${String(s.n).padStart(4)} 行  ${s.title}`);
}
