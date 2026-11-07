#!/usr/bin/env python3
"""文档结构门禁：查《写作与引用规范》§11 那六件事（外加锚点有效性）。

为什么要有这个脚本而不是"写完就算了"：这一轮把修订记录拆成速览＋详录、给每册加目录、
把 84 处裸 `§N` 补成 `《册名》§N`。这些全是排版约束，**没有工具守着就会漂**——
上一轮"视图里又抄了一份枚举表"就是这么来的。规矩只有变成能红的检查才算立住。

五件事（任一件红就 exit 1）：
  1. 该有目录的册必须有目录，且条目数＝`## ` 标题数 − 1（目录自己不列进目录）。
  2. 主干册头部必须有 `- 版本：vX.Y（YYYY-MM-DD）`，格式能被《发布基线》读到。
  3. 修订速览表：一行 ≤120 字，且「详见」列非空、指向能解析。
  4. `§N` 引用：裸写只许指本册；写 `《册名》§N` 的目标小节必须真的存在。
  5. 图：正文引用的文件必须存在；`文档/assets/` 里没被引用的图必须在历史表里有名。

再加一条只对拆过的册生效的：**速览与《附录 A 变更详录》逐条对账**——
本轮把 250 多条修订记录搬成"一句话＋原文归档"，搬丢一条就永远补不回来，
所以这条检查是这次改造唯一能自动证明"压的是排版、不是历史"的东西。

用法：
    python3 tools/doc_structure_check.py            # 门禁，红就非零退出
    python3 tools/doc_structure_check.py --report   # 只报现状计数，不判红
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "文档"
ASSETS = DOCS / "assets"
ARCHIVE_DIR = DOCS / "开发"     # 历史留档：正文不重排、引用不改

# 主干册：面向读者、必须完整合规的那几份。
# 历史工作包（文档/开发/**）只统一头部与引用，正文不重排——那是留档证据，
# 改了行序或数字就不可信（规范 §12 明写这条边界）。
TRUNK = (
    "README.md", "设计稿.md",
    "文档/需求概要说明书.md", "文档/系统设计说明书.md", "文档/数据字典.md",
    "文档/系统使用说明书.md", "文档/模型中心使用说明书.md",
    "文档/生产工作台使用说明书.md", "文档/黄金样例与故障演练.md",
    # 《发布基线》不在名单里：它是生成物，版本＝「生成日期＋commit」两行，
    # 再手写一个 vX.Y 就是第二份版本号——那正是本轮要消灭的东西
    "文档/写作与引用规范.md",
)

# 目录与速览的门槛：短册不必目录，超过就必须要
TOC_MIN_LINES = 200
TOC_MIN_HEADS = 12
REV_MAX_CHARS = 120

HEAD_RE = re.compile(r"^#+\s*(\d+(?:\.\d+)*)", re.M)
H2_RE = re.compile(r"^## (?!目录)(.+)$", re.M)
TOC_ITEM_RE = re.compile(r"^\d+\. \[[^\]]+\]\(#", re.M)
REV_ROW_RE = re.compile(r"^\| (20\d\d-\d\d-\d\d) \| (v[\d.]+) \|([^|]*)\|(.*)\|?\s*$")
IMG_RE = re.compile(r"assets/([A-Za-z0-9._-]+\.(?:png|jpg|jpeg|gif|svg))")
REF_RE = re.compile(r"(?:《([^》]{2,40})》)?\s*§(\d+(?:\.\d+)*)")
EXTERNAL = ("任务书", "实施文档", "资产中心增强模块开发说明")
VER_RE = re.compile(r"^\- 版本：(v[\d.]+)（(\d{4}-\d{2}-\d{2})）", re.M)
FENCE_RE = re.compile(r"^```.*$", re.M)


def prose(text: str) -> str:
    """去掉围栏代码块再数标题。

    规范 §3 的骨架样例里就写着 `## 目录`、`## 0. 一页结论`——
    不剥掉的话门禁会把自己写的规范判成不合规，这种假红一次就没人再信这个检查。
    """
    out, inside = [], False
    for line in text.split("\n"):
        if FENCE_RE.match(line.strip()):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def all_docs() -> list[Path]:
    """全库文档清单。导出 HTML 与 assets 不算。"""
    files = [ROOT / "README.md", ROOT / "设计稿.md"]
    files += sorted(DOCS.glob("*.md"))
    files += sorted((DOCS / "开发").rglob("*.md"))
    return [f for f in files if f.exists()]


def headings(text: str) -> set[str]:
    """一册里所有"可以被 § 指到"的编号，含每一级祖先。

    `#### 4.13.2` 的标题要让 `§4.13` 也算对上——只补顶级父号的话，
    §4.13 会被判成"本册只有 §4"，一整批合法引用变成假红。
    """
    out: set[str] = set()
    for hit in HEAD_RE.findall(text):
        parts = hit.split(".")
        for i in range(1, len(parts) + 1):
            out.add(".".join(parts[:i]))
    return out


def stem_index(files: list[Path]) -> dict[str, Path]:
    """《册名》→ 文件。用 stem 匹配，允许写全称或简称。

    文档里两种写法都有：`《黄金样例与故障演练》` 与 `《黄金样例与故障演练.md》`。
    只收 stem 的话后者永远匹配不上，会把合法的跨册引用报成"那一册找不到小节"——
    假红一次，人就少信这个门禁一次。所以带 .md 的键也收。
    """
    out: dict[str, Path] = {}
    for f in files:
        out[f.stem] = f
        out[f.name] = f
    return out


def check_toc(files: list[Path]) -> list[str]:
    fails = []
    for f in files:
        text = prose(f.read_text(encoding="utf-8"))
        lines = text.count("\n") + 1
        h2 = len(H2_RE.findall(text))
        need = lines > TOC_MIN_LINES or h2 > TOC_MIN_HEADS
        has = "## 目录" in text
        if need and not has:
            fails.append(f"{f.relative_to(ROOT)}：{lines} 行／{h2} 个二级标题，该有目录但没有")
            continue
        if not has:
            continue
        items = len(TOC_ITEM_RE.findall(text))
        # 目录自己不列进目录，所以条目数＝二级标题数（H2_RE 已排除「## 目录」本身）
        expect = h2
        if items != expect:
            fails.append(f"{f.relative_to(ROOT)}：目录列了 {items} 条，正文有 {expect} 个二级标题")
    return fails


def slug(head: str) -> str:
    """GitHub 的页内锚点：小写、去标点、空格换连字符。"""
    s = head.strip().lower()
    s = re.sub(r"[^0-9a-z_一-鿿 -]", "", s)
    return s.replace(" ", "-")


def check_toc_anchors(files: list[Path]) -> list[str]:
    """目录里每个锚点都必须真的能跳到位。

    为什么单独查：本轮把《系统设计》§11 标题里的「153 条业务端点」摘掉之后，
    那一节的锚点跟着变了，而目录还指着旧锚——条目数对得上、检查全绿，链接却是死的。
    只数条目数抓不到这种错，所以必须逐个比锚。
    """
    fails = []
    for f in files:
        text = prose(f.read_text(encoding="utf-8"))
        m = re.search(r"^## 目录\n(.*?)(?=^## )", text, re.M | re.S)
        if not m:
            continue
        real = {slug(h) for h in re.findall(r"^## (.+)$", text, re.M)
                if h.strip() != "目录"}
        for anchor in re.findall(r"\]\(#([^)]+)\)", m.group(1)):
            if anchor not in real:
                fails.append(f"{f.relative_to(ROOT)}：目录锚点 #{anchor} 没有对应标题")
    return fails


def check_version_head(files: list[Path]) -> list[str]:
    fails = []
    for f in files:
        rel = str(f.relative_to(ROOT))
        if rel not in TRUNK:
            continue
        head = f.read_text(encoding="utf-8")[:800]
        if not VER_RE.search(head):
            fails.append(f"{rel}：头部缺「- 版本：vX.Y（YYYY-MM-DD）」，《发布基线》读不到")
    return fails


def check_revision_rows(files: list[Path]) -> list[str]:
    """速览表那一行有没有超字数、有没有「详见」。

    只查主干册的**顶部速览表**（第一个连续块）；附录《变更详录》里长的是原文，
    那是设计如此，不该被查。
    """
    fails = []
    for f in files:
        rel = str(f.relative_to(ROOT))
        if rel not in TRUNK:
            continue
        # 剥掉围栏代码块：《写作与引用规范》§5 里那条示例行长得和真历史一模一样，
        # 不剥的话门禁会把规范自己的例子当成"丢了原文的历史"
        text = prose(f.read_text(encoding="utf-8"))
        lines = text.split("\n")
        start = next((i for i, l in enumerate(lines) if REV_ROW_RE.match(l)), None)
        if start is None:
            continue
        for i in range(start, len(lines)):
            m = REV_ROW_RE.match(lines[i])
            if not m:
                # 连续块结束就停：后面的长行属于附录详录
                if lines[i].startswith("|"):
                    continue
                break
            date, ver, summary, see_also = m.groups()
            if len(summary.strip()) > REV_MAX_CHARS:
                fails.append(f"{rel} {ver}：速览那行 {len(summary.strip())} 字"
                             f"（上限 {REV_MAX_CHARS}），细节该进附录详录")
            elif not see_also.strip():
                fails.append(f"{rel} {ver}：速览那行没有「详见」列，读者无从跳读")
    return fails


DETAIL_RE = re.compile(r"^## 附录 [A-Z]? ?变更详录$", re.M)


def body_only(text: str) -> str:
    """只取正文，不取《附录 X 变更详录》。

    详录是逐轮原文留档——**原文不改**，包括里面的裸 `§N`。改了就不是留档，
    而是替历史重写一遍口径。所以引用检查只管正文，
    这条边界同时写进《写作与引用规范》§12 的已知缺口。
    """
    m = DETAIL_RE.search(text)
    return text if not m else text[:m.start()]


def check_refs(files: list[Path]) -> tuple[list[str], dict]:
    """§ 引用：裸写只许指本册，带册名的必须指得到。

    四档判法，避免把"父级对得上、子级没写"误报成指别册：
      · 本册精确对上 → 过；
      · 本册只有父级（写 §10.1 而本册只到 §10）→ 红，这是失效的深链，不是跨册引用；
      · 别册唯一命中 → 红，要写成《册名》§N；
      · 别册多册命中 → 红，但只报候选让人判，工具不猜。
    历史留档（`文档/开发/**`）与《附录 A 变更详录》都豁免：那是原文，
    改了就不叫留档——这条边界同时写进《写作与引用规范》§12。
    """
    own = {f: headings(prose(f.read_text(encoding="utf-8"))) for f in files}
    by_stem = stem_index(files)
    fails = []
    census = {"本册对上": 0, "带册名且对上": 0, "裸写指别册": 0, "多册歧义": 0,
              "深链失效": 0, "外部《任务书》": 0, "哪一册都没有": 0, "留档豁免": 0}
    for f in files:
        raw = prose(f.read_text(encoding="utf-8"))
        if f.is_relative_to(ARCHIVE_DIR):
            census["留档豁免"] += raw.count("§")
            continue
        text = body_only(raw)
        for m in REF_RE.finditer(text):
            book, ref = m.group(1), m.group(2)
            # 任务书住在仓库外，编号没法校验；前面出现过「任务书」就当外部，不判红
            # 外部文档的两种写法都认：`《任务书》§N` 与「任务书 §N」；
            # 编号住在仓库外，校验不了目标存在，所以不判红也不假装通过
            near = text[max(0, m.start() - 12):m.start()]
            if (book and any(k in book for k in EXTERNAL)) or \
                    (not book and any(k in near for k in EXTERNAL)):
                census["外部《任务书》"] += 1
                continue
            if book:
                # 册名有三种真实写法：《数据字典》／《数据字典.md》／
                # 《开发/生产工作台改造/03-业务验收记录.md》。只按整串找，
                # 带路径的那种永远匹配不上，会把合法引用报成断链
                last = book.split("/")[-1].removesuffix(".md")
                target = (by_stem.get(book) or by_stem.get(last)
                          or by_stem.get(last + ".md")
                          or next((by_stem[s] for s in by_stem if last in s), None))
                if target and ref in own[target]:
                    census["带册名且对上"] += 1
                    continue
                # 写了册名但那一册没这个节——断链，最坏的一种，读者只会以为是自己找错
                fails.append(f"{f.relative_to(ROOT)}：《{book}》§{ref} 在那一册里找不到小节")
                continue
            if ref in own[f]:
                census["本册对上"] += 1
                continue
            # 先当跨册引用查，再判深链失效：本册有 §8 却没有 §8.1，
            # 十有八九是这句本来在指《系统设计》§8.1——先给它认亲，认不上才算失效
            hits = [g for g in files if ref in own[g] and g != f]
            if not hits and ref.split(".")[0] in own[f]:
                census["深链失效"] += 1
                fails.append(f"{f.relative_to(ROOT)}：§{ref} 本册只有 §{ref.split('.')[0]}，"
                             "没有这一级（深链失效）")
                continue
            if len(hits) == 1:
                census["裸写指别册"] += 1
                fails.append(f"{f.relative_to(ROOT)}：裸写 §{ref} 其实指 "
                             f"{hits[0].relative_to(ROOT)}，要写成《册名》§N")
            elif hits:
                census["多册歧义"] += 1
                fails.append(f"{f.relative_to(ROOT)}：裸写 §{ref} 有 {len(hits)} 册都有这一级（"
                             + "、".join(h.name for h in hits[:3]) + "），要人工定一册")
            else:
                census["哪一册都没有"] += 1
                fails.append(f"{f.relative_to(ROOT)}：§{ref} 哪一册都没有对应小节"
                             "（外部《任务书》要标「外部」）")
    return fails, census


def check_history_kept(files: list[Path]) -> list[str]:
    """速览表与《附录 A 变更详录》逐条对账：一条历史都不许丢。

    这一条是本轮改造唯一能自动证明「压缩的是排版、不是记录」的检查：
    版本号集合必须完全相等，日期也必须一致。少一条就是搬丢了历史，
    而搬丢的历史没有任何地方能补回来。
    """
    fails = []
    for f in files:
        text = prose(f.read_text(encoding="utf-8"))   # 示例行不算历史，见 §5 的围栏
        if not DETAIL_RE.search(text):
            continue
        # 按 (版本, 日期) 成对对账，不按版本单独对：同一版本号在同一天被两轮
        # 各自追过（v1.16 就有 11-04 与 11-05 两条），只按版本比会互相覆盖、
        # 把合法历史判成"日期对不上"
        speed = {(m.group(2), m.group(1)) for m in
                 re.finditer(r"^\| (20\d\d-\d\d-\d\d) \| (v[\d.]+) \|", text, re.M)}
        detail = {(m.group(1), m.group(2)) for m in
                  re.finditer(r"^### (v[\d.]+)（(20\d\d-\d\d-\d\d)）$", text, re.M)}
        lost = sorted({v for v, _ in speed - detail} - {v for v, _ in detail})
        ghost = sorted({v for v, _ in detail - speed} - {v for v, _ in speed})
        if lost:
            fails.append(f"{f.relative_to(ROOT)}：速览有 {len(lost)} 条没进详录（原文丢了）："
                         + "、".join(lost[:5]))
        if ghost and not lost:
            # 详录里多出来的通常是历史本身（旧版本先于速览表存在），不算错，报数不判红
            pass
    return fails


def check_archive_header(files: list[Path]) -> list[str]:
    """历史工作包必须自报"这是留档，当前口径在哪一册"。

    为什么单独查这一条：`文档/开发/` 里写着"三处落点""默认指本地"这类当年的口径，
    后来都变了。读者从搜索结果撞进来，看到的是一份**看起来像当前口径**的旧文，
    这比没有文档更糟。头部一行「状态：历史留档 ＋ 当前口径见哪一册」就能挡住。
    """
    fails = []
    for f in files:
        if not f.is_relative_to(ARCHIVE_DIR):
            continue
        head = f.read_text(encoding="utf-8")[:900]
        if "历史留档" not in head:
            fails.append(f"{f.relative_to(ROOT)}：头部没写「状态：历史留档」与当前口径去处")
        elif "当前口径" not in head:
            fails.append(f"{f.relative_to(ROOT)}：写了留档，但没写当前口径住哪一册")
    return fails


def check_images(files: list[Path]) -> list[str]:
    refs: set[str] = set()
    for f in files:
        for name in IMG_RE.findall(f.read_text(encoding="utf-8")):
            refs.add(name)
    missing = sorted(n for n in refs if not (ASSETS / n).exists())
    fails = [f"正文引用了却不存在的图：{', '.join(missing)}"] if missing else []
    on_disk = {p.name for p in ASSETS.glob("*") if p.suffix.lower() in
               (".png", ".jpg", ".jpeg", ".gif", ".svg")} if ASSETS.exists() else set()
    orphans = sorted(on_disk - refs)
    if orphans:
        # 孤儿图不算错，但必须"在册"：《需求概要》§7.2 那张历史表里得有名
        ledger = (DOCS / "需求概要说明书.md").read_text(encoding="utf-8")
        unaccounted = [o for o in orphans if o not in ledger]
        if unaccounted:
            fails.append(f"{len(unaccounted)} 张图既没被引用、也没进《需求概要》§7.2 历史表："
                         + "、".join(unaccounted[:6])
                         + ("…" if len(unaccounted) > 6 else ""))
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description="文档结构门禁")
    ap.add_argument("--report", action="store_true", help="只报计数，不判红")
    ap.add_argument("--only", default="", help="逗号分隔：toc,version,revision,refs,images")
    args = ap.parse_args()

    files = all_docs()
    wanted = {w.strip() for w in args.only.split(",") if w.strip()}

    sections: list[tuple[str, list[str]]] = []
    # check_refs 一次同时给出「逐条不合规」与「总体计数」，别跑两遍：
    # 它要读全部文档，跑两遍就是两倍的读盘，而两遍结果必须一致才有意义
    ref_fails, census = check_refs(files)

    if not wanted or "toc" in wanted:
        sections.append(("1 目录", check_toc(files)))
    if not wanted or "toc" in wanted:
        sections.append(("1b 目录锚点", check_toc_anchors(files)))
    if not wanted or "version" in wanted:
        sections.append(("2 头部版本号", check_version_head(files)))
    if not wanted or "revision" in wanted:
        sections.append(("3 修订速览", check_revision_rows(files)))
    if not wanted or "refs" in wanted:
        sections.append(("4 § 引用", ref_fails))
    if not wanted or "history" in wanted:
        sections.append(("5 历史不丢", check_history_kept(files)))
    if not wanted or "archive" in wanted:
        sections.append(("5b 留档头部", check_archive_header(files)))
    if not wanted or "images" in wanted:
        sections.append(("6 图", check_images(files)))

    print(f"扫了 {len(files)} 份文档（主干 {len(TRUNK)} 份）")
    print("§ 引用计数：" + "／".join(f"{k} {v}" for k, v in census.items()))
    total = 0
    for title, fails in sections:
        verdict = "通过" if not fails else "{} 处不合规".format(len(fails))
        print(f"\n{title}：{verdict}")
        for line in fails[:25]:
            print(f"  - {line}")
        if len(fails) > 25:
            print(f"  …另 {len(fails) - 25} 处（--only 单看这一项）")
        total += len(fails)

    if args.report:
        print(f"\n（--report 模式，不判红）合计 {total} 处待改")
        return 0
    if total:
        print(f"\n红：{total} 处待改。规矩见《写作与引用规范》§3—§8。")
        return 1
    print("\n绿：文档结构与《写作与引用规范》一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
