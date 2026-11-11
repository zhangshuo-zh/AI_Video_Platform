# -*- coding: utf-8 -*-
'''列出界面上给人看的长说明，按文件排序，附行号，供逐个精简。'''
import pathlib, re, sys, html

JS = pathlib.Path('app/frontend/js')
TAG = re.compile(r'<[^>]+>')

def text_of(s):
    return html.unescape(TAG.sub('', s)).strip()

rows = []
for f in sorted(JS.rglob('*.js')):
    lines = f.read_text(encoding='utf-8').splitlines()
    text = '\n'.join(lines)
    # 抓 <p class="note">…</p> 与 class="sub"/"hint" 里较长的裸文本
    for m in re.finditer(r'<p class="note"[^>]*>(.*?)</p>', text, re.S):
        body = text_of(m.group(1))
        if len(body) >= 40:
            ln = text[:m.start()].count('\n') + 1
            rows.append((len(body), str(f).replace('app/frontend/js/', ''), ln, body))
    for m in re.finditer(r'class="(?:sub|hint)"[^>]*>([^<>{}]{40,})', text):
        body = text_of(m.group(1))
        ln = text[:m.start()].count('\n') + 1
        rows.append((len(body), str(f).replace('app/frontend/js/', ''), ln, body))

rows.sort(key=lambda r: -r[0])
total = sum(r[0] for r in rows)
print(f'共 {len(rows)} 处，合计 {total} 字')
by_file = {}
for n, f, ln, body in rows:
    by_file.setdefault(f, [0, 0])
    by_file[f][0] += 1
    by_file[f][1] += n
print('\n按文件：')
for f, (c, n) in sorted(by_file.items(), key=lambda kv: -kv[1][1]):
    print(f'  {f:40} {c:3} 处  {n:5} 字')
print('\n最长的 12 处：')
for n, f, ln, body in rows[:12]:
    print(f'  [{n:3}] {f}:{ln}  {body[:70]}…')
