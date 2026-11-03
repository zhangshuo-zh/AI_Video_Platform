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
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw).stdout


def git_info() -> tuple[str, str]:
    short = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).strip()
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT).strip()
    return short, branch


def design_version() -> str:
    """设计稿最新修订行的版本号。发布以设计稿版本为标记。"""
    text = (ROOT / "设计稿.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| \d{4}-\d{2}-\d{2} \| (v[\d.]+) \|", text, re.M)
    return rows[-1] if rows else "?"


def doc_versions() -> dict[str, str]:
    out = {}
    for name in DOC_NAMES:
        head = (DOCS / f"{name}.md").read_text(encoding="utf-8")[:600]
        m = re.search(r"版本：(v[\d.]+)（(\d{4}-\d{2}-\d{2})）", head)
        out[name] = f"{m.group(1)}（{m.group(2)}）" if m else "?"
    return out


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
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "FASTVIDEO_DB": str(Path(tmp) / "probe.db"),
               "PYTHONDONTWRITEBYTECODE": "1"}
        out = run([sys.executable, "-c", probe], cwd=BACKEND, env=env)
    return json.loads(out)


def run_tests() -> tuple[int, str]:
    """现场跑全量 pytest。返回 (passed 数, 耗时原文)。

    输出原样转发到本进程 stdout：CI 里这一步是"测试＋门禁"合一的，
    只把尾巴三行留给日志的话，用例挂了人看不到断言在哪。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=BACKEND,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
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
CAP_LABELS = {"text": "文本", "image": "图像", "video": "视频",
              "tts": "配音", "music": "音乐"}


def preserve_baseline_line(pattern: str) -> str:
    """重生成时保留人工填写的行（演练、备注）。找不到就给占位。"""
    if BASELINE.exists():
        m = re.search(pattern, BASELINE.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    return "尚未演练／待填写"


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
    a(f"| 迁移 | 0001—{facts['migration']:04d}，共 {facts['migration']} 个 | `app/backend/migrations/` |")
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
      "没列出的槽位不是被这张表过滤，而是还没配置，在「系统设置 · 模型与密钥」里显示「未配置」。")
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
    a(f"- 最近一次真浏览器验收：{facts['drill_browser']}")
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
            if str(facts["business"]) not in text or str(facts["operations"]) not in text:
                fails.append(f"{where}: 业务/总路由数与基线不一致（{facts['business']}/{facts['operations']}）")
    if facts["migration"] >= 22:
        dd = (DOCS / "数据字典.md").read_text(encoding="utf-8")
        if f"{facts['tables']} 张业务与系统表" not in dd:
            fails.append(f"数据字典.md: 表数不是 {facts['tables']} 张")
        if f"0001—{facts['migration']:04d}" not in dd:
            fails.append(f"数据字典.md: 迁移号没追到 0001—{facts['migration']:04d}")
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
    probe = probe_app()
    passed, duration = passed_from_log(log) if log else run_tests()
    facts = {"commit": commit, "branch": branch, "design": design_version(),
             "docs": doc_versions(), "migration": latest, "tables": probe["tables"],
             "paths": probe["paths"], "operations": probe["operations"],
             "business": probe["business"], "health": probe["health"],
             "auth": probe["auth"], "passed": passed, "duration": duration,
             "matrix": validation_matrix(),
             "configured_slots": provider_slot_counts()[0],
             "total_slots": provider_slot_counts()[1],
             "drill_job": preserve_baseline_line(r"Job 故障演练：(.+)$"),
             "drill_backup": preserve_baseline_line(r"备份恢复演练：(.+)$"),
             "drill_pipeline": preserve_baseline_line(r"产线故障演练：(.+)$"),
             "drill_browser": preserve_baseline_line(r"真浏览器验收：(.+)$")}
    if check:
        raise SystemExit(check_docs(facts))
    BASELINE.write_text(build_md(facts), encoding="utf-8")
    print(f"已生成 {BASELINE.relative_to(ROOT)}（{facts['passed']} passed，"
          f"{facts['business']}/{facts['operations']} 路由，迁移 00{facts['migration']}）")


if __name__ == "__main__":
    main()
