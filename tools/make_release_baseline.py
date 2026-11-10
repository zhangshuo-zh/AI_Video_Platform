#!/usr/bin/env python3
"""生成《发布基线》（文档/发布基线.md），并提供 --check 校验四份说明书的关键数字。

为什么要有这个脚本：四份说明书里的关键数字（迁移号、表数、路由数、测试数）
过去靠手抄，出过"封面改了、正文表还留着旧数"的事故。基线一律现算，不做第二份
手工账：

- 迁移与表数：把 `FASTVIDEO_DB` 指到临时库、跑一遍 `create_app()`，
  让迁移真实执行后再数表——不碰现役库，也不靠数文件猜。
- 路由数：从 `app.routes` 现算。口径只有两条，都在这里定义：
  总 API 路由 = 全部业务 + 健康检查 + 认证；业务路由 = 总数减去后两者。
- 测试：现场跑一遍 pytest，记录 passed 数与耗时。测试通过不等于真实验收通过，
  两件事在基线里分开记。
- 验证矩阵：只读现役库 `model_validation`。环境检查永远不能替真实调用说话，
  这条口径与 `validations.summary_for`（唯一定义处）保持一致。

`--check` 不写文件：校验四份说明书中这些数字是否与本机现算一致，
不一致时逐条列出并让退出码为 1，供提交前当门闩用。
`.github/workflows/tests.yml` 的「发布基线门禁」就是它的常驻去处——CI 里测试
已单独跑过，所以带 `--pytest-log` 读同一次运行的日志，不再白等第二遍。

只依赖标准库。用法：
    app/backend/.venv/bin/python tools/make_release_baseline.py           # 生成
    app/backend/.venv/bin/python tools/make_release_baseline.py --check   # 只校验
    ... --check --pytest-log /tmp/pytest.log                              # CI：复用本次测试日志
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "app" / "backend"
DOCS = ROOT / "文档"
BASELINE = DOCS / "发布基线.md"

DOC_NAMES = ("需求概要说明书", "系统设计说明书", "数据字典", "系统使用说明书")

# 已知未验证与阻塞项**不写在这个脚本里**：条目住 `tools/release_blockers.json`，
# 每条带核对日期。上一版把它们写成常量，硬字幕做完之后没人回头改，基线继续宣称
# "没有测试覆盖、页面也没有入口"——自动生成不等于自动正确，第二份手写账迟早会漂。
BLOCKERS_FILE = ROOT / "tools" / "release_blockers.json"


def load_blockers() -> list[dict]:
    """读阻塞项清单。缺文件、缺字段直接拒绝生成，不产出空表糊过去。"""
    if not BLOCKERS_FILE.exists():
        raise SystemExit(f"缺 {BLOCKERS_FILE.relative_to(ROOT)}：阻塞项不再写死在脚本里，"
                         "请把条目（含核对日期）放进那个文件")
    try:
        data = json.loads(BLOCKERS_FILE.read_text(encoding="utf-8"))
    except ValueError as err:
        raise SystemExit(f"{BLOCKERS_FILE.name} 解析失败：{err}")
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit(f"{BLOCKERS_FILE.name} 里没有 items")
    for entry in items:
        missing = [k for k in ("item", "reason", "checked_at") if not entry.get(k)]
        if missing:
            raise SystemExit(f"{BLOCKERS_FILE.name} 有一条缺 {missing}：{entry!r}")
    return items


# 现算的"生产可用"口径，必须与 validations.summary_for 保持一致（那里是唯一定义处）：
# 环境检查 ok ＋ 真实调用 ok ＋ 产物验收 ok。缺任何一环都只算"未达生产可用"。
PRODUCTION_READY_KINDS = ("env_check", "real_call", "artifact_review")


def run(cmd: list[str], **kw) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        # 失败时把子进程的原话打出来：只抛 CalledProcessError 的话，
        # 真正的原因（缺依赖、沙箱拦了 git init）全被吞掉，门禁看起来像随机红。
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise SystemExit(
            f"子进程失败（{proc.returncode}）：{' '.join(map(str, cmd))[:200]}")
    return proc.stdout


def git_info() -> tuple[str, str]:
    short = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).strip()
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT).strip()
    return short, branch


def _vkey(v: str) -> tuple[int, ...]:
    """把 "v0.118" 变成可比较的 (0, 118)。
    按字符串比会错：`"v0.9" > "v0.118"`，因为逐字符比到第二位就分出胜负了。"""
    return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())


def design_version() -> str:
    """设计稿最新修订行的版本号。发布以设计稿版本为标记。

    取**数值最大**的那一行，不取表里最后一行。这一句原来写的是 `rows[-1]`：
    v0.118 那轮把修订表重排成「最新在前」，重排前表是新旧混排的，最后一行
    恰好是最新的那条，所以这个错一直看不出来；重排之后它开始印 **v0.1**
    （10-30 的初稿行），而三道门禁没有一道查这一行——错的版本号就这么一路
    绿灯进了公开仓库。现在除了取对，`--check` 还要复查基线自己那行（见 check_docs）。
    """
    text = (ROOT / "设计稿.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| \d{4}-\d{2}-\d{2} \| (v[\d.]+) \|", text, re.M)
    return max(rows, key=_vkey) if rows else "?"


def doc_versions() -> dict[str, str]:
    out = {}
    for name in DOC_NAMES:
        head = (DOCS / f"{name}.md").read_text(encoding="utf-8")[:600]
        m = re.search(r"版本：(v[\d.]+)（(\d{4}-\d{2}-\d{2})）", head)
        out[name] = f"{m.group(1)}（{m.group(2)}）" if m else "?"
    return out


def _facts_from_baseline() -> dict:
    """探针跑不起来时的降级读数：从基线文件里取当前值。

    受限环境（沙箱拦删除、没有 git）里 `create_app()` 会中途退出，
    但门禁的主要职责是"文档与基线一致"，不是"每次都重算路由"。
    降级时打印一行说明，并把 `degraded` 带回去，CI 上会正常现算。
    """
    text = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else ""

    def num(pattern: str, default: int = 0) -> int:
        m = re.search(pattern, text)
        return int(m.group(1)) if m else default

    return {"paths": num(r"(\d+) 路径"),
            "operations": num(r"总 API 路由 \| (\d+) 操作"),
            "business": num(r"业务路由 \| (\d+)"),
            "tables": num(r"\| 表 \| (\d+) 张"),
            "health": [], "auth": []}


def probe_app() -> dict:
    """在子进程里用临时库建 app：拿路由口径与"迁移跑完后应有的表数"。

    放子进程是因为 config 在 import 时读环境变量，改完还得还回去；
    干脆隔离掉，主进程环境保持干净，pytest 才不会误连临时库。
    """
    probe = r"""
import json, os, sqlite3, sys
sys.path.insert(0, ".")
from main import create_app
app = create_app()
# 这台 FastAPI 把 include 的路由表示成惰性 _IncludedRouter，app.routes 摊不开；
# openapi schema 由路由表生成，是路径/操作数的权威来源。
spec = app.openapi()
METHODS = ("get", "post", "put", "delete", "patch")
ops = {}
for path, methods in spec["paths"].items():
    found = [m for m in methods if m in METHODS]
    if found:
        ops[path] = found
paths = len(ops)
operations = sum(len(v) for v in ops.values())
health = sorted(p for p in ops if p.endswith("/health"))
auth = sorted(p for p in ops if "/auth/" in p or p.rstrip("/").endswith("/auth"))
auth_ops = sum(len(ops[p]) for p in health + auth)
con = sqlite3.connect(os.environ["FASTVIDEO_DB"])
tables = con.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
print(json.dumps({"paths": paths, "operations": operations,
                  "health": health, "auth": auth,
                  "business": operations - auth_ops, "tables": tables}))
"""
    # 不用 TemporaryDirectory：它在退出时删目录，受限环境里删除会被拦，
    # 于是整个门禁被一个"临时文件没清掉"噎死（traceback 盖住真正的检查结果）。
    tmp = tempfile.mkdtemp(prefix="fv-probe-")
    try:
        # 四样都指到临时目录：`create_app()` 会迁移、建 files/、git init 工作区。
        # 只改 FASTVIDEO_DB 时，其余三样仍落在 backend/data/，所谓"只读门禁"
        # 其实在仓库里写了目录——与 docstring 第 17 行自相矛盾。
        env = {**os.environ,
               "FASTVIDEO_DB": str(Path(tmp) / "probe.db"),
               "FASTVIDEO_FILES": str(Path(tmp) / "files"),
               "FASTVIDEO_WORKSPACE": str(Path(tmp) / "workspace"),
               "FASTVIDEO_JOBS": str(Path(tmp) / "jobs"),
               "PYTHONDONTWRITEBYTECODE": "1",
               # 同 run_tests：/tmp 的删除探测被拦时子解释器起不来，
               # 指到确定可写可删的目录（只读 /tmp 的容器同样受益）。
               **{"TMPDIR": str(Path(tmp).parent),
                  "TEMP": str(Path(tmp).parent),
                  "TMP": str(Path(tmp).parent)}}
        try:
            out = run([sys.executable, "-c", probe], cwd=BACKEND, env=env)
        except (SystemExit, Exception) as exc:   # noqa: B014 — SystemExit 不是 Exception
            facts = _facts_from_baseline()
            facts["degraded"] = str(exc)[:160]
            print(f"⚠ 探针不可用：{facts['degraded']}；路由与表数沿用基线现值",
                  file=sys.stderr)
            return facts
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return json.loads(out)


def run_tests() -> tuple[int, str]:
    """现场跑全量 pytest。返回 (passed 数, 耗时原文)。

    输出原样转发到本进程 stdout：CI 里这一步是"测试＋门禁"合一的，
    只把尾巴三行留给日志的话，用例挂了人看不到断言在哪。

    `FASTVIDEO_PYTEST_TMPDIR` 可选：容器/沙箱里 `tempfile` 的探测删除
    可能被宿主策略拦掉（实测会报 No usable temporary directory），
    指定一个确定可写的目录后，子进程在 import pytest 之前就把它定下来。
    """
    # basetemp 每次唯一：`pytest-of-<user>` 留在地上时，下一次
    # `mkdir(exist_ok=False)` 会直接 FileExistsError，整套用例变成 58 个 error。
    base = tempfile.mkdtemp(prefix="fv-baseline-")
    args = ["-q", "--tb=line", "-p", "no:cacheprovider", "--basetemp", base]
    tmpdir = os.environ.get("FASTVIDEO_PYTEST_TMPDIR")
    if tmpdir:
        code = ("import sys, tempfile; tempfile.tempdir = sys.argv[1]; "
                "import pytest; raise SystemExit(pytest.main(sys.argv[2:]))")
        cmd = [sys.executable, "-c", code, tmpdir, *args]
    else:
        cmd = [sys.executable, "-m", "pytest", *args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=BACKEND,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             # 这里原来强制设 `FASTVIDEO_TEST_SKIP_ENV=1`，理由是"与 CI 同一条
             # 规则，基线数字才不会漂"。这一项已经去掉（2026-11-09 外部审查
             # 第 9 条）：它和 CI workflow、conftest 是同一个开关的三处，
             # 三处一起去掉之后，`delete_guard` 只按真实探测决定跳不跳，
             # 删除类用例在这个进程里也会真跑——基线数字因此是"真跑出来的"，
             # 不是"两边一样地少跑几条算出来的"。
             # 子解释器启动时 tempfile 要在系统临时目录里做"建了再删"的探测；
             # /tmp 只读或删除被宿主策略拦时，探测全失败会直接
             # "No usable temporary directory" 起不来。指到 basetemp 旁边
             # （本来就是个确定可写可删的目录），只读 /tmp 的容器同样受益。
             **{"TMPDIR": str(Path(base).parent),
                "TEMP": str(Path(base).parent),
                "TMP": str(Path(base).parent)}})
    text = proc.stdout + proc.stderr
    sys.stdout.write(text)
    sys.stdout.flush()
    m = re.search(r"(\d+) passed[^\n]*?in ([\d.]+)s", text)
    if proc.returncode != 0 or m is None:
        raise SystemExit("pytest 未全绿，拒绝生成基线")
    return int(m.group(1)), m.group(0).split("in ", 1)[1].rstrip()


def passed_from_log(path: Path) -> tuple[int, str]:
    """从本次 CI 已经跑过的 pytest 日志里取 passed 数与耗时。

    为什么需要这一条：CI 已经单独跑了一遍全量测试（为了让失败输出直接可读），
    --check 再跑一遍就是白等 25 秒。这里读的是同一次运行留下的日志，
    不是人抄的数——数字仍然"现算"，只是算的是本次流水线。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if re.search(r"\b\d+ failed\b|\b\d+ error", text):
        raise SystemExit("日志里有失败或错误，门禁不通过")
    m = re.search(r"(\d+) passed[^\n]*?in ([\d.]+)s", text)
    if m is None:
        raise SystemExit(f"日志里找不到 passed 汇总，别拿空日志过门禁：{path}")
    return int(m.group(1)), m.group(0).split("in ", 1)[1].rstrip()


def live_db_path() -> Path:
    env = os.environ.get("FASTVIDEO_DB")
    return Path(env) if env else BACKEND / "data" / "platform.db"


def provider_slot_counts() -> tuple[int, int]:
    """`provider_config` 的固定行数与其中填了 provider 的行数。

    十行是产品口径（5 类能力 × 本地／云端）；填了 provider 的才是"配过"的。
    矩阵只列有配置或有记录的槽位，这两个数负责把"没列出来的那几行"说清楚——
    没列出来是没配置，不是被过滤。
    """
    path = live_db_path()
    if not path.exists():
        return 0, 0
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        total = int(con.execute("SELECT COUNT(*) FROM provider_config").fetchone()[0])
        configured = int(con.execute(
            "SELECT COUNT(*) FROM provider_config WHERE provider <> ''").fetchone()[0])
    finally:
        con.close()
    return configured, total


def validation_matrix() -> list[dict]:
    """只读现役库：每个配置过的槽位，五档出处各自最新一条的状态。"""
    path = live_db_path()
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    slots: dict[tuple, dict] = {}
    try:
        for r in con.execute("SELECT capability, backend, provider, model, enabled "
                             "FROM provider_config ORDER BY capability, backend"):
            if r["provider"]:
                slots[(r["capability"], r["backend"], r["provider"])] = {
                    "model": r["model"], "enabled": bool(r["enabled"]),
                    "kinds": {}}
        for r in con.execute("SELECT capability, backend, provider, kind, state, job_id, "
                             "created_at FROM model_validation ORDER BY id"):
            key = (r["capability"], r["backend"], r["provider"])
            slot = slots.setdefault(key, {"model": "", "enabled": True, "kinds": {}})
            slot["kinds"][r["kind"]] = {"state": r["state"], "job_id": r["job_id"],
                                        "at": r["created_at"]}
    finally:
        con.close()
    out = []
    for (cap, backend, provider), slot in sorted(slots.items()):
        kinds = slot["kinds"]
        ready = all(kinds.get(k, {}).get("state") == "ok" for k in PRODUCTION_READY_KINDS)
        out.append({"capability": cap, "backend": backend, "provider": provider,
                    "model": slot["model"], "enabled": slot["enabled"],
                    "kinds": kinds, "production_ready": ready})
    return out


KIND_ORDER = ("env_check", "mock", "real_call", "target_machine", "artifact_review")
KIND_LABELS = {"env_check": "环境检查", "mock": "模拟", "real_call": "真实调用",
               "target_machine": "目标机基准", "artifact_review": "产物验收"}
TARGET_BASELINE = DOCS / "目标机基准.md"


def target_baseline_state() -> tuple[int, int]:
    """读《目标机基准》那张表：返回（已跑条数, 总条数）。

    已跑 = 读数不是「—」且状态里没有「未跑」。这张表是人工填的，
    与 `model_validation.target_machine` 是两件事：后者是记录，前者是读数。
    """
    if not TARGET_BASELINE.exists():
        return 0, 0
    text = TARGET_BASELINE.read_text(encoding="utf-8")
    rows = re.findall(r"^\|\s*\d+\s*\|[^|]*\|([^|]*)\|([^|]*)\|\s*$", text, re.M)
    if not rows:
        return 0, 0
    done = sum(1 for reading, status in rows
               if "未跑" not in status and reading.strip() not in ("—", "-", ""))
    return done, len(rows)
CAP_LABELS = {"text": "文本", "image": "图像", "video": "视频",
              "tts": "配音", "music": "音乐"}


def preserve_baseline_line(pattern: str) -> list[str]:
    """重生成时保留**人工登记**的行（演练、备注）。找不到就给占位。

    为什么是"保留"而不是"现算"：这几行记的是"那一轮真跑过什么"，由验收的人
    在《黄金样例与故障演练》里落笔后回填，脚本不重跑三套浏览器（那是几分钟，
    且当前工作树可能正被别的会话改动，跑出来的数不代表这一册登记的那一轮）。
    代价是它**不会自己变新**——所以 `build_md` 里那行明写"人工登记、重生成不
    自动更新"，别让"最近一次"四个字把旧读数冒充成当前观测（《写作与引用规范》§13.1）。

    **返回全部匹配行，不是第一条**（2026-11-10 修）。原来用 `re.search` 只取
    第一条，而「真浏览器验收」这张单子会一行一行往后追加
    （「…补充」「…补充之二」「…补充之三」都命中同一个正则）。
    于是重生成一次就静默删掉后三条——**它们记的是真跑过的验收，
    删掉之后没人知道那几套工具曾经全过**。实测：HEAD 的 §4 有 4 行验收记录，
    重生成后剩 1 行，文件 72 行掉到 67 行，而门禁当时不查这一节，一路绿灯。
    """
    if BASELINE.exists():
        found = [m.group(1) for m in
                 re.finditer(pattern, BASELINE.read_text(encoding="utf-8"), re.M)]
        if found:
            return found
    return ["尚未演练／待填写"]


def preserve_labeled_lines(pattern: str) -> list[tuple[str, str]]:
    """同 `preserve_baseline_line`，但连**行的标签一起**返回：[(标签, 正文)]。

    为什么不能只保留正文：这几行的标签本身带信息——「真浏览器验收」
    「真浏览器验收补充」「…补充之二」「…补充之三」是四批不同的工作，
    按序号一眼能看出先后。只存正文再统一印成「补充」，
    四条就变成四个一模一样的抬头，等于把"第几批"这个事实删掉
    （2026-11-10 第一次修法就是这么错的，重生成后三条全印成「补充」）。
    标签由文件自己带着，这里原样搬走，不重新发明。
    """
    if BASELINE.exists():
        found = [(m.group("label"), m.group("body")) for m in
                 re.finditer(pattern, BASELINE.read_text(encoding="utf-8"), re.M)]
        if found:
            return found
    return [("真浏览器验收（人工登记，重生成基线不会自动更新）", "尚未演练／待填写")]


def build_md(facts: dict) -> str:
    today = date.today().isoformat()
    lines: list[str] = []
    a = lines.append
    a("# AI 视频生产平台 发布基线")
    a("")
    a("- 本文件由 `tools/make_release_baseline.py` 生成：数字一律现算，不要手改；")
    a("- 四份说明书的关键数字以本文件为准；`--check` 校验两者一致——本地提交前跑一次，"
      "CI（`.github/workflows/tests.yml` 的「发布基线门禁」）每次都跑。")
    a("")
    a(f"- 生成日期：{today}")
    a(f"- Git：`{facts['commit']}`（{facts['branch']}）；设计稿 {facts['design']}")
    a("- 文档版本：" + "；".join(f"{k} {v}" for k, v in facts["docs"].items()))
    a("")
    a("## 1. 代码事实（现算）")
    a("")
    a("| 项 | 值 | 来源 |")
    a("|---|---|---|")
    gap = ("，编号无 0048" if facts["migration_missing"] == [48]
           else (f"，缺号 {facts['migration_missing']}" if facts["migration_missing"] else ""))
    a(f"| 迁移 | 0001—{facts['migration']:04d}，共 {facts['migration_count']} 个{gap} | `app/backend/migrations/` |")
    a(f"| 表 | {facts['tables']} 张 | 临时库真实跑完迁移后数 |")
    a(f"| 总 API 路由 | {facts['operations']} 操作 / {facts['paths']} 路径 | `app.openapi()` 现算 |")
    a(f"| 业务路由 | {facts['business']}（总 API 路由减去健康检查 {len(facts['health'])} 条与认证 {len(facts['auth'])} 条） | 口径见上 |")
    a(f"| 测试 | **{facts['passed']} passed**（{facts['duration']}） | 本次生成现场执行 |")
    a(f"| 健康检查 | {'、'.join(facts['health'])} | 排除在业务路由外 |")
    a(f"| 认证 | {'、'.join(facts['auth'])} | 排除在业务路由外 |")
    a("")
    a("> 口径定义：**业务路由数**＝需要登录的业务接口操作数；**总 API 路由数**＝业务路由＋健康检查＋认证。全篇文档只用这两个词。")
    a("")
    a("## 2. 模型验证矩阵（现役库只读）")
    a("")
    a("| 槽位 | " + " | ".join(KIND_LABELS[k] for k in KIND_ORDER) + " | 生产可用 |")
    a("|---|" + "---|" * (len(KIND_ORDER) + 1))
    for slot in facts["matrix"]:
        # provider 为空的行是"这一轨还没配 provider、但留过环境检查痕"，
        # 拼出"文本 · local · "这种尾随分隔符会被读成表格坏了，直接不拼。
        parts = [CAP_LABELS.get(slot['capability'], slot['capability']), slot['backend']]
        if slot['provider']:
            parts.append(slot['provider'])
        track = " · ".join(parts)
        if slot["model"]:
            track += f" / {slot['model']}"
        cells = []
        for k in KIND_ORDER:
            rec = slot["kinds"].get(k)
            cells.append("—" if rec is None else
                         f"{rec['state']}（#{rec['job_id']}）" if rec["job_id"] else rec["state"])
        a(f"| {track} | " + " | ".join(cells) + f" | {'是' if slot['production_ready'] else '否'} |")
    if not facts["matrix"]:
        a("| （现役库无验证记录） | " + " | ".join("—" for _ in KIND_ORDER) + " | 否 |")
    a("")
    a("> 生产可用＝环境检查 ok ＋ 真实调用 ok ＋ 产物验收 ok（与 `validations.summary_for` 同一口径）。环境检查通过只代表条件齐了。")
    a("")
    a(f"> 槽位口径：`provider_config` 是固定十行（5 类能力 × 本地／云端），本表只列"
      f"**填了 provider 或留过验证记录**的槽位（当前 {len(facts['matrix'])} 行；"
      f"其中填了 provider 的 {facts['configured_slots']} / {facts['total_slots']} 行）。"
      "没列出的槽位不是被这张表过滤，而是还没配置，在「模型中心 · 引擎管理」里显示「未配置」。")
    a("")
    a(f"> 目标机基准：**{facts['target_done']}/{facts['target_total']} 已跑**"
      f"（人工填写在 `文档/目标机基准.md`，本行由脚本现读）。"
      "为 0 时 `--check` 打印告警；此列「目标机基准」有记录才算数，"
      "本机的环境检查／真实调用绿灯不能替目标机签字。")
    a("")
    a("## 3. 未验证能力与阻塞项")
    a("")
    blockers = load_blockers()
    a("| 能力 | 阻塞原因（人工前置条件） | 核对日期 |")
    a("|---|---|---|")
    for entry in blockers:
        a(f"| {entry['item']} | {entry['reason']} | {entry['checked_at']} |")
    a("")
    a("> 本表逐条来自 `tools/release_blockers.json`，第三列是最后一次人工核对的日期；"
      "做完的能力要从那里删掉，而不是在脚本里另写一份。")
    a("")
    a("## 4. 演练与备份记录")
    a("")
    a(f"- 最近一次 Job 故障演练：{facts['drill_job']}")
    a(f"- 最近一次备份恢复演练：{facts['drill_backup']}")
    a(f"- 最近一次产线故障演练：{facts['drill_pipeline']}")
    # 真浏览器验收是一张**会一行一行往后追加**的单子（每补一套工具就多一条），
    # 所以这里逐条渲染，且**连标签一起搬走**——标签带着"第几批"这个事实，
    # 统一印成「补充」等于把序号删掉（见 `preserve_labeled_lines`）。
    for label, entry in facts["drill_all"]:
        a(f"- {label}：{entry}")
    a("")
    # **这段以前不在 `build_md` 里，于是每重生成一次就被抹掉一次**（2026-11-10 补）。
    # 它记的是"历史读数不是现值"这条口径本身，被生成器吞掉最讽刺：
    # 文件里正留着 920/1037/…/1202 这些历史数字，却没有一句说明它们不是现值。
    # 当时的"当前值"还硬写着 1218，早就过期——这里改成现算。
    a("> **测试口径（别拿历史数字当现值）**：本文件与《系统设计说明书》里出现的 "
      "`920 / 1037 / 1054 / 1058 / 1067 / 1074 / 1202 passed` 都是**当时那一轮的读数**，"
      "写在各自的变更记录里，不是当前值。当前口径以 "
      "`tools/make_release_baseline.py --check` 打印的为准："
      f"**CI 与开发机现在是同一个数——{facts['passed']} passed**。"
      "（2026-11-09 外部审查第 9 条把 `FASTVIDEO_TEST_SKIP_ENV` 去掉了："
      "它曾让 CI 上 4 条「删资产清图」类用例恒跳过，用「两边一样地不测」换一个稳定的 "
      "passed 数。现在只留 `conftest.delete_guard` 里的真实探测：环境删不掉文件才跳过，"
      "删得掉就必须跑，CI 的 ubuntu-latest 删得掉。开发机可能因沙箱多 1 条跳过——"
      "`test_worker_rss.py` 不给跑 `ps`，内存采样那一档无法取证；CI 上这条会真跑。）"
      "pytest 必须在 `app/backend` 目录下跑，在仓库根目录跑会 "
      "`ModuleNotFoundError: No module named 'main'`。")
    a("")
    return "\n".join(lines) + "\n"


def check_docs(facts: dict) -> int:
    """说明书关键数字对齐基线。谁声明的数字谁被查；不声明的只查残留。

    需求概要与系统设计声明测试数和路由数，必须与现算一致；
    数据字典声明表数与迁移号；使用说明书是操作手册，只查"残留旧数"。
    """
    # 声明了测试数与路由数的文档
    ASSERT_BASELINE = ("需求概要说明书", "系统设计说明书")
    fails: list[str] = []
    for name in DOC_NAMES:
        text = (DOCS / f"{name}.md").read_text(encoding="utf-8")
        where = f"{name}.md"
        if "403 passed" in text:
            fails.append(f"{where}: 残留旧测试数 403")
        if facts["migration"] >= 22 and "0001—0019" in text.replace("0017—0019", "").replace("0019 迁移", ""):
            fails.append(f"{where}: 仓库结构残留 0001—0019")
        if name in ASSERT_BASELINE:
            if f"{facts['passed']} passed" not in text:
                fails.append(f"{where}: 缺「{facts['passed']} passed」")
            # 只查"声明当前基线"的那几行：文档头部（前 20 行）与测试表。
            # 正文里的「977→1037」「v0.97 交付时 628」是修订历史，不能要求它们
            # 等于今天；但头部或测试表里还留着 936，就该红。
            stale: set[int] = set()
            for idx, line in enumerate(text.splitlines()):
                if idx >= 20 and "后端单元／集成" not in line:
                    continue
                for num in re.findall(r"(\d+)\s+passed", line):
                    if int(num) != facts["passed"]:
                        stale.add(int(num))
            if stale:
                fails.append(f"{where}: 残留旧测试数 {sorted(stale)}"
                             f"（基线 {facts['passed']}，历史修订行已豁免）")
            if str(facts["business"]) not in text or str(facts["operations"]) not in text:
                fails.append(f"{where}: 业务/总路由数与基线不一致（{facts['business']}/{facts['operations']}）")
    if facts["migration"] >= 22:
        dd = (DOCS / "数据字典.md").read_text(encoding="utf-8")
        if f"{facts['tables']} 张业务与系统表" not in dd:
            fails.append(f"数据字典.md: 表数不是 {facts['tables']} 张")
        if f"0001—{facts['migration']:04d}" not in dd:
            fails.append(f"数据字典.md: 迁移号没追到 0001—{facts['migration']:04d}")
    # 基线自己头部那一行也要对得上。加这一条的理由就是上面那次事故：
    # `design_version()` 取错行、印出「设计稿 v0.1」，而三道门禁全绿——
    # 因为没有任何一道把基线头部再算一遍。现算与 stamped 不一致，这里就该红。
    base = (DOCS / "发布基线.md").read_text(encoding="utf-8")
    stamped = re.search(r"^- Git：.*；设计稿 (v[\d.]+)", base, re.M)
    if not stamped:
        fails.append("发布基线.md: 头部找不到「；设计稿 vX.Y」那一段")
    elif stamped.group(1) != facts["design"]:
        fails.append(f"发布基线.md: 头部设计稿版本 {stamped.group(1)}"
                     f" 与现算 {facts['design']} 不一致（重跑一次生成）")
    # **基线自己 §1 那几张表也要对得上**（2026-11-10 补）。
    #
    # 上面那条只查头部的「设计稿 vX.Y」，于是 §1 里的迁移号、表数、路由数、
    # 测试数**从来没有任何一道门禁看过**。实测：盘上写着「迁移 47 个／
    # 1239 passed」，真值是 50（49 份文件，无 0048）／1312，`--check` 照样
    # 打印「关键数字与基线一致」并 exit=0——它比的是说明书与**内存里现算的
    # facts**，唯独不比对那个自称"以本文件为准"的文件。四份说明书被检查，
    # 唯一的权威反而没被检查。
    #
    # 为什么这里只查"值对不对"、不查"该有哪些行"：`build_md` 才是格式的
    # 定义处，用正则把它的每一行都复述一遍会长出第二份格式真相。
    # 挑四个真正会被引用的数（迁移／表／路由／测试），逐项与现算比。
    for label, expected in (
            ("迁移", f"0001—{facts['migration']:04d}，共 {facts['migration_count']} 个"),
            ("表", f"{facts['tables']} 张"),
            ("总 API 路由", f"{facts['operations']} 操作 / {facts['paths']} 路径"),
            ("测试", f"**{facts['passed']} passed**")):
        row = re.search(rf"^\| {re.escape(label)} \| (.+?) \|", base, re.M)
        if not row:
            fails.append(f"发布基线.md: §1 找不到「{label}」那一行")
        elif expected not in row.group(1):
            fails.append(f"发布基线.md: §1「{label}」写的是「{row.group(1).strip()}」，"
                         f"现算是「{expected}」——重跑一次生成")
    # **§4 的真浏览器验收条数不能比盘上少**（2026-11-10 补）。
    #
    # 这一节是人工登记、生成器只负责搬运。而搬运曾经只搬第一条
    # （`re.search` 取第一个匹配），于是一次重生成就把后面三条真跑过的
    # 验收记录静默删掉，全部门禁绿灯——因为没有任何一道看这一节的条数。
    # 条数只增不减：新增是"又验收了一套"，减少只可能是搬运出错或有人手删。
    # 注意这里比的是**文件自身前后一致**（现读 vs 现读的条数），不涉及现算；
    # 真要抓"重生成会不会丢"，得在生成前后各数一次——那条防线在
    # `preserve_labeled_lines` 的返回全部匹配里，这里是它的兜底。
    browser_rows = re.findall(r"^- 真浏览器验收[^：]*?：", base, re.M)
    if not browser_rows:
        fails.append("发布基线.md: §4 一条「真浏览器验收」记录都没有了"
                     "（这一节是人工登记，不该被删空）")
    if fails:
        print("与基线不一致：")
        for f in fails:
            print("  ✗", f)
        return 1
    print("四份说明书关键数字与基线一致。")
    return 0


def main() -> None:
    check = "--check" in sys.argv
    # --pytest-log 只给 --check 用：CI 里测试已经单独跑过一遍，读同一次运行的日志，
    # 不再白等第二遍。生成模式不认这个参数——基线里的数必须是这一趟真跑出来的。
    log = Path(sys.argv[sys.argv.index("--pytest-log") + 1]) if "--pytest-log" in sys.argv else None
    if log and not check:
        raise SystemExit("--pytest-log 只与 --check 同用；生成基线必须现场跑测试")
    commit, branch = git_info()
    migrations = sorted((BACKEND / "migrations").glob("[0-9]" * 4 + "_*.sql"))
    latest = int(migrations[-1].name[:4])
    # **「共 N 个」要数文件，不能用最大编号代替**（2026-11-10 修）。
    # 编号是允许跳号的：仓库里就没有 `0048`（0047 之后直接 0049），
    # 于是 `latest = 50` 而实际只有 49 份文件，基线却印「共 50 个」——
    # 一个自己算错、还自称"数字一律现算"的文件。缺号一并写进 facts，
    # 让读者知道是跳号而不是丢了文件（丢了才是事故）。
    migration_count = len(migrations)
    migration_missing = [n for n in range(1, latest + 1)
                         if n not in {int(p.name[:4]) for p in migrations}]
    probe = probe_app()
    passed, duration = passed_from_log(log) if log else run_tests()
    facts = {"commit": commit, "branch": branch, "design": design_version(),
             "docs": doc_versions(), "migration": latest,
             "migration_count": migration_count,
             "migration_missing": migration_missing,
             "tables": probe["tables"],
             "paths": probe["paths"], "operations": probe["operations"],
             "business": probe["business"], "health": probe["health"],
             "auth": probe["auth"], "passed": passed, "duration": duration,
             "matrix": validation_matrix(),
             "target_done": target_baseline_state()[0],
             "target_total": target_baseline_state()[1],
             "configured_slots": provider_slot_counts()[0],
             "total_slots": provider_slot_counts()[1],
             "drill_job": preserve_baseline_line(r"Job 故障演练：(.+)$")[0],
             "drill_backup": preserve_baseline_line(r"备份恢复演练：(.+)$")[0],
             "drill_pipeline": preserve_baseline_line(r"产线故障演练：(.+)$")[0],
             # 真浏览器验收是**会追加的单子**，保留全部行（见 `preserve_labeled_lines`）。
             # 正则要认全几种写法：旧的「真浏览器验收：」、v1.2 之后的
             # 「真浏览器验收（人工登记…）：」，以及补充条目
             # 「真浏览器验收补充（日期）：」「…补充之二（日期）：」——
             # 中间夹着"补充"/"补充之二"，所以标签段用 `[^：]*?` 而不是
             # `(?:（[^）]*）)?`（后者要求括号紧跟其后，补充条目一条都匹配不上）。
             # label 用惰性匹配 + body 吃到行尾，标签原样留住（含"补充之二"）。
             "drill_all": preserve_labeled_lines(
                 r"^- (?P<label>真浏览器验收[^：]*?)：(?P<body>.+)$"),
             # 探针是否降级（2026-11-09 外部审查第 8 条）。
             #
             # 原来 `probe_app()` 失败时把 `degraded` 写进它自己返回的 facts，
             # 但**这里组装新 facts 时没带这一项**，于是这个事实在汇合处被丢掉；
             # `--check` 分支也从不看它。实测过（故意把探针改成必然
             # SystemExit(9)）：应用完全起不来，门禁仍打印
             # 「四份说明书关键数字与基线一致」并且 exit=0。
             # 一个会在应用挂掉时变绿的门禁比没有门禁更糟——
             # 它给了"已验证"的错觉。
             "degraded": probe.get("degraded", "")}
    if check:
        # 探针降级时**拒绝判定**：这一趟根本没验证到应用，路由/表数全是
        # 沿用基线的旧值，继续比下去只会得出"都对得上"。
        # 降级只在生成模式允许（那时人工会看到那行 ⚠）。
        if facts.get("degraded"):
            print(f"✗ 探针降级，本次校验不可信，拒绝判定：{facts['degraded']}",
                  file=sys.stderr)
            raise SystemExit(1)
        # 目标机基准是"另一台机器上才量得出"的数：本机全绿不能替它签字。
        # 一条未跑时只告警（本机开发本来跑不了），但若有人据此标了
        # production_ready，就必须拦住——那是拿本机的绿灯给目标机背书。
        if facts["target_total"] and facts["target_done"] == 0:
            print(f"⚠ 目标机基准：0/{facts['target_total']} 已跑——"
                  "六条一条未跑，production_ready 不得据此宣称"
                  "（详见 文档/目标机基准.md）")
        ready = [r for r in facts["matrix"] if r["production_ready"]]
        if ready and facts["target_done"] == 0:
            print(f"✗ 有 {len(ready)} 档被标 production_ready，但目标机基准一条未跑")
            raise SystemExit(1)
        raise SystemExit(check_docs(facts))
    BASELINE.write_text(build_md(facts), encoding="utf-8")
    print(f"已生成 {BASELINE.relative_to(ROOT)}（{facts['passed']} passed，"
          f"{facts['business']}/{facts['operations']} 路由，"
          f"迁移 {facts['migration']:04d}，共 {facts['migration_count']} 个）")


if __name__ == "__main__":
    main()
