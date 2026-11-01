#!/usr/bin/env python3
"""把 文档/ 下的 Markdown 导出成单文件 HTML。

为什么要这个脚本：三份说明书要拿给别人看，不能要求对方同时拿到
`文档/assets/` 或 `app/截图/`。这里把图片直接编码成 data URI 写进 HTML，
一个文件就是一份可交付文档。

只依赖标准库，不引入 Markdown 解析包。支持的语法是当前文档实际用到的：
标题、段落、有序／无序列表、表格、围栏代码、图片、链接、粗体、斜体、行内代码。
"""
from __future__ import annotations

import base64
import html
import mimetypes
import re
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parents[1] / "文档"
OUT_DIR = DOCS_DIR / "导出"

INLINE_CODE_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")


def data_uri(path: Path) -> str:
    """图片转 data URI。失败不静默跳过——文档里少一张图必须让构建报错。"""
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def resolve_asset(md_path: Path, ref: str) -> str:
    """把 Markdown 里的相对图片路径变成 data URI；外链原样保留。"""
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    target = (md_path.parent / ref).resolve()
    if not target.exists():
        raise FileNotFoundError(f"{md_path.name} 引用了不存在的图片：{ref}")
    return data_uri(target)


def inline(text: str, md_path: Path) -> str:
    """行内元素。顺序固定：转义 → 图片 → 链接 → 行内码 → 粗斜体。"""
    out = html.escape(text, quote=False)

    def img(m: re.Match) -> str:
        alt = m.group(1)
        src = resolve_asset(md_path, m.group(2).strip())
        return f'<img alt="{alt}" src="{src}">'

    def link(m: re.Match) -> str:
        label = m.group(1)
        href = m.group(2).strip()
        if href.endswith(".md"):
            href = href[:-3] + ".html"
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    out = IMAGE_RE.sub(img, out)
    out = LINK_RE.sub(link, out)
    out = INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = BOLD_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = ITALIC_RE.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in body.split("|")]


def render_table(lines: list[str], md_path: Path) -> str:
    header = split_row(lines[0])
    rows = [split_row(line) for line in lines[2:]]
    th = "".join(f"<th>{inline(cell, md_path)}</th>" for cell in header)
    body = []
    for row in rows:
        # Markdown 表允许尾列省略；补齐，避免导出的 HTML 少格子。
        while len(row) < len(header):
            row.append("")
        tds = "".join(f"<td>{inline(cell, md_path)}</td>" for cell in row[:len(header)])
        body.append(f"<tr>{tds}</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead>' \
           f"<tbody>{''.join(body)}</tbody></table></div>"


def render_markdown(md_path: Path) -> str:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code))}</code></pre>")
            continue

        if stripped == "---":
            out.append("<hr>")
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2), md_path)}</h{level}>")
            i += 1
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and TABLE_SEP_RE.match(lines[i + 1].strip()):
            table = [lines[i]]
            i += 1
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i])
                i += 1
            out.append(render_table(table, md_path))
            continue

        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            lis = "".join(f"<li>{inline(item, md_path)}</li>" for item in items)
            out.append(f"<ul>{lis}</ul>")
            continue

        if re.match(r"^\d+\.\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            lis = "".join(f"<li>{inline(item, md_path)}</li>" for item in items)
            out.append(f"<ol>{lis}</ol>")
            continue

        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith(("```", "|", "---", "#")) or \
               re.match(r"^[-*]\s+", nxt) or re.match(r"^\d+\.\s+", nxt):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{inline(' '.join(para), md_path)}</p>")

    return "\n".join(out)


def page(title: str, body: str, source: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  --ink: #1d1d1f;
  --muted: #5f6673;
  --line: rgba(0,0,0,.12);
  --bg: #f5f5f7;
  --card: rgba(255,255,255,.92);
  --code: #f0f2f5;
  --accent: #2453cf;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font: 15px/1.72 -apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
}}
main {{
  max-width: 1080px;
  margin: 32px auto 72px;
  padding: 44px 56px 64px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: 0 14px 38px rgba(0,0,0,.08);
}}
.meta {{
  color: var(--muted);
  font-size: 13px;
  margin: -24px 0 32px;
}}
h1 {{ font-size: 34px; line-height: 1.25; margin: 0 0 18px; letter-spacing: -.02em; }}
h2 {{ font-size: 26px; margin: 42px 0 16px; padding-bottom: 8px; border-bottom: 1px solid var(--line); }}
h3 {{ font-size: 21px; margin: 32px 0 12px; }}
h4 {{ font-size: 17px; margin: 24px 0 10px; }}
p, ul, ol {{ margin: 0 0 16px; }}
ul, ol {{ padding-left: 26px; }}
li {{ margin: 7px 0; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
code {{
  padding: 2px 5px;
  border-radius: 5px;
  background: var(--code);
  font-family: "SFMono-Regular", Menlo, Consolas, monospace;
  font-size: .92em;
}}
pre {{
  overflow: auto;
  padding: 18px;
  border-radius: 12px;
  background: #17181b;
  color: #f2f3f5;
}}
pre code {{ padding: 0; background: transparent; color: inherit; }}
img {{
  display: block;
  max-width: 100%;
  height: auto;
  margin: 18px auto 8px;
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 8px 24px rgba(0,0,0,.08);
}}
.table-wrap {{ overflow-x: auto; margin: 18px 0 24px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th, td {{ padding: 10px 12px; border: 1px solid var(--line); vertical-align: top; text-align: left; }}
th {{ background: #ececf0; font-weight: 650; white-space: nowrap; }}
tbody tr:nth-child(even) {{ background: rgba(0,0,0,.018); }}
hr {{ border: 0; border-top: 1px solid var(--line); margin: 34px 0; }}
footer {{
  max-width: 1080px;
  margin: -48px auto 40px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}}
@media print {{
  body {{ background: #fff; }}
  main {{ margin: 0; border: 0; box-shadow: none; border-radius: 0; }}
  img {{ break-inside: avoid; }}
  table {{ break-inside: avoid; }}
}}
@media (max-width: 720px) {{
  main {{ margin: 0; padding: 28px 20px 44px; border-radius: 0; }}
  h1 {{ font-size: 28px; }}
}}
</style>
</head>
<body>
<main>
{body}
</main>
<footer>由 <code>tools/build_doc_html.py</code> 生成 · 源文件：{html.escape(source)} · 图片已内嵌，单文件可直接分发</footer>
</body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for md_path in sorted(DOCS_DIR.glob("*.md")):
        body = render_markdown(md_path)
        title_match = re.search(r"<h1>(.*?)</h1>", body)
        title = re.sub(r"<.*?>", "", title_match.group(1)) if title_match else md_path.stem
        html_path = OUT_DIR / f"{md_path.stem}.html"
        html_path.write_text(page(title, body, md_path.name), encoding="utf-8")
        print(f"{html_path.name}  {html_path.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
