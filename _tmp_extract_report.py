# -*- coding: utf-8 -*-
"""临时：提取 buildAiReport 源码"""
import pathlib
import re

html = pathlib.Path(r'd:/ETF_Analysis/ETF_Analysis/web/index.html').read_text(encoding='utf-8')
m = re.search(r'function buildAiReport\(d, hist, extra\) \{', html)
start = m.start()
i = html.index('{', start)
depth = 0
for j in range(i, len(html)):
    if html[j] == '{':
        depth += 1
    elif html[j] == '}':
        depth -= 1
        if depth == 0:
            end = j + 1
            break
pathlib.Path(r'd:/ETF_Analysis/ETF_Analysis/_tmp_build.js').write_text(
    html[start:end], encoding='utf-8')
print("extracted")
