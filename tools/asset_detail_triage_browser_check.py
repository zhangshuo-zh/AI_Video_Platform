#!/usr/bin/env python3
"""资产详情页 ＋ 文本提取分诊页 ＋ 三个工作区的公共件：真浏览器验收。

为什么要补：2026-11-09 的覆盖面盘点里，`asset-detail.js`（408 行）、
`asset-triage.js`（296 行）、`asset-workspaces.js`（102 行）从来没被任何浏览器
门禁打开过。`assets.js` 与 `writing.js` 上一轮刚补（见
`tools/writing_assets_browser_check.py`），这三个是同一批剩下的。后端有
`test_assets.py`／`test_asset_studio.py`／`test_extraction.py` 守着逻辑，所以这
补的又是**渲染盲区**：接口对不对有人管，"接口回来的东西有没有被画对"没人管。

优先挑**口算得出答案**的——这类断言改坏了立刻红，不依赖"看起来对"：

  * `missingOf()`（asset-workspaces.js:87）是纯函数，判据分散在五个分支
    （图／提示词／简介／档案／形态，且人物与场景道具的形态说法不同）。
    这里造出缺项数已知的资产，按"页面必须报那个数、且逐项点名"来量，
    正例反例都有：资料齐的只剩 1 项，全空的报满 5 项，道具必须说
    "至少一个状态形态"而不是"服饰形态"。
  * `suggest()` 与 `mergePlan()`（asset-triage.js:52、65）是同一条链上的两个
    纯函数：前者决定"标不标出来像已有的"，后者决定"合并会补哪几格、哪几格
    不动"。**它们与服务端的 `extraction_merge` 是两份实现**——口径一旦漂开，
    界面承诺"已有内容一个字都不覆盖"、服务端却覆盖了，人是在点完之后才发现。
    这里直接读确认框里那三行字（会补／别名追加／不动），两个方向都验。
  * `WORKSPACES` 的顺序与 `modules.js` 的 `assets.subs` 必须一字不差
    （asset-workspaces.js:13 的注释写了原因：反着写的结果是"高亮资产库、
    页面摆的是候选"）。这是**两个文件之间的约定**，跟 `charCount` 与
    `_tend()` 那一对同类：两边各自的测试都绿，也证明不了它们对得上。
  * 删除保护在界面上有没有落地：被镜头引用的资产，删除弹窗必须把引用数摆出来
    并禁用确认钮；不被引用的必须**真的可点**（只验前半句的话，一个"永远禁用"
    的实现也能绿——那是假绿）。

只读纪律：整轮**不点接受／合并确认／拒绝／保存／删除**，不建 Job 之外不发任何
写请求。唯一的写动作是种子数据本身（进程内直插仓储），以及 `/tmp` 下的库副本。

反向验证：脚本底部 `MUTATIONS` 逐条写明"改哪一行 → 哪条断言红"，八条都在本机
真跑过一遍。不做这一步的"全绿"没有信息量：断言写错的绿与被测代码正确的绿长得
一模一样。

用法：python3 tools/asset_detail_triage_browser_check.py
（缺后端依赖时会自动用 `app/backend/.venv` 的解释器重执行自己）
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H


def _ensure_backend_deps() -> None:
    """本脚本要在进程内直插仓储造种子，一路 import 到 pydantic。

    服务用 `H.backend_python()` 起，那只管子进程；本机依赖只在
    `app/backend/.venv` 里，而 CI 装进系统解释器。缺依赖时用 venv 那只
    重执行自己一次，`python3 tools/...` 在哪都能直接跑。
    """
    try:
        import pydantic  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    target = Path(H.backend_python())
    # 比字符串而不是 resolve()：venv 的 bin/python 是软链，resolve() 后两个
    # 解释器会变成同一个真实文件，"已经在 venv 里"就永远判不出来。
    if Path(sys.executable) == target:
        raise SystemExit("这个解释器连 pydantic 都没有，后端 venv 里那份也一样："
                         "先按 requirements.txt 装依赖再跑")
    entry = sys.argv[0] if str(sys.argv[0]).endswith(".py") else __file__
    os.execv(str(target), [str(target), str(Path(entry).resolve()), *sys.argv[1:]])


_ensure_backend_deps()

BACKEND = H.BACKEND
TMP = H.temp_dir("fv-ad-")
PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

# 报告整页未捕获异常：前端没有测试框架，这是唯一的自动化信号。
COLLECT_ERRORS = """
window.__fvErrors = [];
window.addEventListener('error', e => window.__fvErrors.push(String(e.message)));
window.addEventListener('unhandledrejection',
  e => window.__fvErrors.push('unhandled: ' + String(e.reason)));
"""

DOC_BODY = "第一集开场，雨落在旧码头，阿雨撑伞而立"


def prepare_env() -> None:
    for key, sub in [("FASTVIDEO_DB", "platform.db"), ("FASTVIDEO_FILES", "files"),
                     ("FASTVIDEO_JOBS", "jobs"), ("FASTVIDEO_WORKSPACE", "workspace")]:
        os.environ[key] = str(TMP / sub)
    TMP.mkdir(parents=True, exist_ok=True)
    H.prepare_backend_db(TMP / "platform.db")


def _admin_id() -> int:
    from db import get_conn

    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.id FROM app_user u
               JOIN user_role ur ON ur.user_id = u.id
               JOIN role r ON r.id = ur.role_id
               WHERE r.code = 'admin' AND u.is_active = 1
               ORDER BY u.is_superuser DESC, u.id LIMIT 1"""
        ).fetchone()
    if row is None:
        raise SystemExit("副本库里没有可用的 admin 账号，验不了这两个页面")
    return int(row["id"])


def mint_admin_session(token_hash: str, admin_id: int) -> None:
    from datetime import datetime, timedelta

    from repositories.users import sessions

    sessions.create(token_hash, admin_id,
                    (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))


def seed_db() -> dict:
    """造一份"用了半季"的库：五条资产（覆盖 missingOf 的每个分支）、一镜引用、
    一次抽取与七条候选（覆盖 suggest／mergePlan 的每个分支）。

    直插仓储而不走 HTTP：服务还没起；而且这些是"库里的历史"，不是要验的写路径。
    """
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    from db import get_conn
    from repositories.content import assets as asset_repo
    from repositories.content import episodes as ep_repo
    from repositories.content import profiles as prof_repo
    from repositories.content import shots as shot_repo
    from repositories.content import variants as var_repo
    from repositories.extraction import runs as run_repo
    from repositories.jobs import jobs as job_repo
    from repositories.projects import projects as project_repo
    from services import documents as doc_service

    admin = _admin_id()
    project = project_repo.create("雨巷", "一条旧码头与它的雨", [], admin)
    project_id = int(project["id"])
    # 详情页与分诊页都读 `store.current`，而项目列表按 last_opened_at 倒序排——
    # 副本库里带着现役项目，不把种子项目顶到最前，页面打开时选中的就是别人。
    project_repo.mark_opened(project_id)

    made = doc_service.create_document(project_id, None, "novel", "第一集",
                                       DOC_BODY, actor="seed")
    doc_row = made["row"]

    # ── 五条资产，每条对准 missingOf 的一个分支 ─────────────────────────
    # 资料齐的：图 0、提示词有、简介有、档案写了、形态 1 个
    # → 只缺「参考图」1 项
    rich = asset_repo.create(project_id, "character", "阿雨", "撑伞的人",
                             "短发，米色风衣", aliases=["小雨"], tags=["主角"])
    rich_id = int(rich["id"])
    prof_repo.upsert(rich_id, biography="生在码头", personality="寡言",
                     growth_arc="从渔家女到船娘", text_prompt="写她")
    var_repo.create(asset_id=rich_id, variant_type="outfit", name="雨夜外套", prompt="深色")

    # 全空的：图 0、提示词空、简介空、没档案、没形态 → 缺满 5 项
    empty = asset_repo.create(project_id, "character", "空人物", "", "")
    # 有简介与提示词，但没档案、没形态 → 缺 3 项
    mid = asset_repo.create(project_id, "character", "老周", "船老大", "驼背，灰白胡")
    # 道具：形态那一条的说法必须与人物不同（"状态形态"而不是"服饰形态"）→ 缺 2 项
    prop = asset_repo.create(project_id, "prop", "油纸伞", "一把旧伞", "竹骨，褪色")
    # 全局资产：不该出现「升为全局」勾选框，且明说不能收回
    glob = asset_repo.create(None, "character", "全局人", "谁的都看得见", "固定的样子")

    # 一镜引用「阿雨」：删除保护与「被哪些镜头引用」都要有真对象
    ep = ep_repo.create(project_id, "第一集")
    shot_repo.create(int(ep["id"]), "阿雨站在雨里", "prompt", 3.0, [rich_id], [])

    # ── 一次抽取 ＋ 七条候选，每条对准 suggest／mergePlan 的一个分支 ──────
    job = job_repo.create({
        "project_id": project_id, "capability": "text", "kind": "text",
        "execution_mode": "local", "provider_code": "seed", "model_code": "seed-model",
        "pricing_snapshot": None, "created_by": admin,
    })
    # 作业早就跑完了：create() 只会写 queued，这里把它推到终态。
    # 直改 SQL 而不是走状态机，因为这是"库里的历史"，不是要验的转移路径。
    with get_conn() as conn:
        conn.execute("UPDATE job SET status = 'succeeded', "
                     "finished_at = datetime('now','localtime') WHERE id = ?",
                     (int(job["id"]),))
    run = run_repo.create_run(
        project_id=project_id, doc_id=int(doc_row["id"]), job_id=int(job["id"]),
        doc_title="第一集", base_version="v0", content_hash="h0",
        model_code="seed-model", prompt_version="p1", created_by=admin)
    run_id = int(run["id"])

    items = [
        # ① 同名命中「空人物」，且目标两格都空 → mergePlan 说"会补：简介、
        #    一致性提示词"；候选别名「旧名」不在目标上 → "别名追加：旧名"
        {"kind": "character",
         "payload": {"name": "空人物", "summary": "补来的简介",
                     "consistency_prompt": "补来的提示词", "aliases": ["旧名"]},
         "confidence": 0.9,
         "evidence": {"quote": "雨落在旧码头", "start": 3, "end": 8}},
        # ② 别名命中：候选名「小雨」是「阿雨」的别名，不是它的正名。
        #    名字本身已经在目标的别名里 → mergePlan 不该重复追加。
        {"kind": "character",
         "payload": {"name": "小雨", "summary": "另一个说法",
                     "consistency_prompt": "另一个说法", "aliases": []},
         "confidence": 0.7,
         "evidence": {"quote": "阿雨撑伞而立", "start": 8, "end": 14}},
        # ③ 同名命中「阿雨」，目标两格都有内容 → "不动：简介、一致性提示词"
        {"kind": "character",
         "payload": {"name": "阿雨", "summary": "撑伞的人",
                     "consistency_prompt": "短发，米色风衣", "aliases": []},
         "confidence": 0.92,
         "evidence": {"quote": "阿雨撑伞而立", "start": 9, "end": 15}},
        # ④ 没有任何原文证据 → 页面要明说"服务端不会让它进资产"
        {"kind": "scene",
         "payload": {"name": "码头", "summary": "旧码头", "consistency_prompt": "石阶"},
         "confidence": 0.65, "evidence": {}},
        # ⑤ 普通道具候选
        {"kind": "prop",
         "payload": {"name": "油纸伞", "summary": "一把旧伞", "consistency_prompt": "竹骨"},
         "confidence": 0.8,
         "evidence": {"quote": "撑伞", "start": 13, "end": 15}},
        # ⑥ 镜头候选：只给导出入口，不给「接受成新资产」
        {"kind": "shot",
         "payload": {"name": "镜1", "summary": "阿雨站在雨里"},
         "confidence": 0.6,
         "evidence": {"quote": "阿雨撑伞而立", "start": 9, "end": 15}},
        # ⑦ 已经处理过的那条：终态不该再给任何按钮
        {"kind": "character",
         "payload": {"name": "已处理的人", "summary": "x", "consistency_prompt": "y"},
         "confidence": 0.5, "evidence": {"quote": "雨", "start": 1, "end": 2}},
    ]
    run_repo.insert_items(run_id, items)
    run_repo.set_status(run_id, "done", "种子", materialized=True)
    rejected = [r for r in run_repo.items(run_id) if r["kind"] == "character"][-1]
    run_repo.decide(int(rejected["id"]), "rejected", admin)

    # 每条候选的 id 按插入顺序拿回来：断言要认准"哪一行"，不能只认"页面上有这串字"。
    rows = run_repo.items(run_id)
    by_name = {}
    for r in rows:
        try:
            name = json.loads(r["payload"]).get("name") or ""
        except ValueError:
            name = ""
        by_name.setdefault(name, []).append(int(r["id"]))

    return {
        "project_id": project_id,
        "run_id": run_id,
        "rich": rich_id,
        "empty": int(empty["id"]),
        "mid": int(mid["id"]),
        "prop": int(prop["id"]),
        "global": int(glob["id"]),
        "item_empty": by_name["空人物"][0],      # ① 同名命中、目标两格都空
        "item_alias": by_name["小雨"][0],        # ② 别名命中
        "item_rich": by_name["阿雨"][0],         # ③ 同名命中、目标两格都有
        "item_scene": by_name["码头"][0],        # ④ 无原文证据
        "item_prop": by_name["油纸伞"][0],       # ⑤ 有原文证据
        "item_shot": by_name["镜1"][0],          # ⑥ 镜头
        "item_rejected": int(rejected["id"]),    # ⑦ 已驳回
        # missingOf() 逐项推出来的期望值（顺序就是页面上的顺序）
        "miss_empty": ["参考图（第一张就是封面）", "一致性提示词", "简介",
                       "人物档案（履历／性格／成长过程）", "至少一个服饰形态"],
        "miss_prop": ["参考图（第一张就是封面）", "至少一个状态形态"],
    }


def start_chrome():
    import proto_cdp_check as P

    return H.launch_chrome(P.PORT)


def go(cdp, url: str) -> None:
    """换页必须整页重载：应用没有 hashchange 监听（app.js 只在启动时读一次
    location.hash），只改 hash 的 Page.navigate 根本不会换视图。"""
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.5)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.6)


def wait_css(cdp, sel: str, timeout: float = 8.0) -> bool:
    """等某个选择器出现。详情与候选都是异步取的，直接断言会测到"还没渲染"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval(f"!!document.querySelector({json.dumps(sel)})"):
            return True
        time.sleep(0.2)
    return False


def card_text(cdp, title: str):
    """按 h2 取一整张卡片的 innerText。比 nth-child 稳：卡片是条件渲染的，
    序号会跟着"档案读不到／形态读不到"那几个分支变。"""
    return cdp.eval("(() => { const c = [...document.querySelectorAll('.card')].find("
                    "el => el.querySelector('.card-head h2')?.innerText.trim() === "
                    f"{json.dumps(title)}); return c ? c.innerText : null; }})()")


def item_text(cdp, item_id: int):
    """取某一条候选整块的 innerText。断言必须认出"哪一行"：
    同名与别名命中会给出**完全相同**的 chip 文字（都是「像是已有的「阿雨」」），
    只认那串字的话，两条候选各自都能让断言绿——那正是假绿。"""
    return cdp.eval(f"document.querySelector('#as-item-{item_id}')?.innerText")


def main() -> int:
    prepare_env()
    seed = seed_db()

    from security import new_session_token

    token, token_hash = new_session_token()
    mint_admin_session(token_hash, _admin_id())

    proc = H.start_service(PORT)
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}"
              + (f"  · {detail}" if detail else ""), flush=True)

    chrome = None
    try:
        H.wait_http(f"{BASE}/api/health")
        import proto_cdp_check as P

        chrome = start_chrome()
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=COLLECT_ERRORS)
        cdp.cmd("Runtime.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=BASE, path="/", httpOnly=True)

        # ── 详情页 · missingOf 正例 ─────────────────────────────────────
        # 深链 `#assets/0/a<id>`：assets.js 的 render 先看 getParam('assets')，
        # 有值就整页交给 asset-detail.js（assets.js:184）。
        go(cdp, f"{BASE}/#assets/0/a{seed['rich']}")
        wait_css(cdp, "#ad-name")
        time.sleep(0.6)

        head = P.text_of(cdp, ".page-head") or ""
        check("深链打开的就是那一条资产（不是列表也不是别的条目）",
              "阿雨" in head and "空人物" not in head and "老周" not in head,
              head.replace("\n", " ")[:70])

        miss = card_text(cdp, "还缺什么") or ""
        check("「还缺什么」算得准：资料齐的只剩参考图一项",
              "1 项" in miss and "参考图" in miss
              and "人物档案" not in miss and "形态" not in miss,
              miss.replace("\n", " ")[:110])

        # 页头那个数是 SQL 里 json_each 数出来的，引用卡里那个数是 shots 接口
        # 算出来的——两条不同的代码路径，必须给出同一个数。
        ref_card = card_text(cdp, "被哪些镜头引用") or ""
        check("引用计数两条路径一致（页头 vs 引用卡）",
              "被 1 条镜头引用" in head and ref_card.count("1 条") >= 1
              and "阿雨站在雨里" in ref_card,
              f"页头行={head.strip().splitlines()[-1][:44]} / 卡片={ref_card.splitlines()[1:2]}")

        var_card = card_text(cdp, "命名形态") or ""
        check("形态清单把「几个形态」与「几张图」分开数",
              "1 个" in var_card and "0 图" in var_card and "雨夜外套" in var_card,
              var_card.replace("\n", " ")[:90])

        # 被引用的资产：删除弹窗必须把引用数摆出来，且确认钮真的禁用。
        P.eval_js(cdp, "(() => {const b=document.querySelector('#ad-del');"
                       " if(b) b.click(); return true})()")
        time.sleep(0.4)
        modal = P.text_of(cdp, "#ad-del-modal") or ""
        del_disabled = P.eval_js(cdp, "document.querySelector('#ad-del-ok')?.disabled")
        check("被引用时删除弹窗摆出引用数并禁用确认钮",
              "正被 1 条镜头引用" in modal and "删不掉" in modal and del_disabled is True,
              f"disabled={del_disabled}")

        # ── 详情页 · missingOf 反例 ────────────────────────────────────
        go(cdp, f"{BASE}/#assets/0/a{seed['empty']}")
        wait_css(cdp, "#ad-name")
        time.sleep(0.6)
        miss = card_text(cdp, "还缺什么") or ""
        # 逐项点名而不只数个数：个数对了但列错项（比如列了"简介"两次、
        # 漏了"人物档案"）同样是不对。
        hit_items = [x for x in seed["miss_empty"] if x in miss]
        check("「还缺什么」算得准：全空的报满 5 项且逐项点名",
              "5 项" in miss and len(hit_items) == 5,
              f"命中 {len(hit_items)}/5：{hit_items}")

        # ── 详情页 · 道具那一条的说法 ──────────────────────────────────
        go(cdp, f"{BASE}/#assets/0/a{seed['prop']}")
        wait_css(cdp, "#ad-name")
        time.sleep(0.6)
        miss = card_text(cdp, "还缺什么") or ""
        check("道具的形态缺项说「状态形态」而不是「服饰形态」",
              "至少一个状态形态" in miss and "服饰" not in miss,
              miss.replace("\n", " ")[:110])

        # 不被引用的资产：确认钮必须真的可点。只验"被引用时禁用"的话，
        # 一个恒禁用的实现也能绿——那是假绿。
        P.eval_js(cdp, "(() => {const b=document.querySelector('#ad-del');"
                       " if(b) b.click(); return true})()")
        time.sleep(0.4)
        prop_modal = P.text_of(cdp, "#ad-del-modal") or ""
        prop_disabled = P.eval_js(cdp, "document.querySelector('#ad-del-ok')?.disabled")
        check("不被引用时删除确认钮可用（不是恒禁用）",
              bool(prop_modal) and prop_disabled is False
              and "删不掉" not in prop_modal,
              f"disabled={prop_disabled}")
        P.eval_js(cdp, "(() => {const b=document.querySelector('#ad-del-cancel');"
                       " if(b) b.click(); return true})()")
        time.sleep(0.3)

        # ── 详情页 · 全局资产 ──────────────────────────────────────────
        go(cdp, f"{BASE}/#assets/0/a{seed['global']}")
        wait_css(cdp, "#ad-name")
        time.sleep(0.6)
        ghead = P.text_of(cdp, ".page-head") or ""
        grid = P.eval_js(cdp, "document.querySelector('.form-grid')?.innerText") or ""
        check("全局资产不给「升为全局」勾选框，并说明不能收回",
              "全局资产池" in ghead
              and not P.has(cdp, "#ad-global")
              and "不能收回成单个项目资产" in grid,
              f"有勾选框={P.has(cdp, '#ad-global')}")
        # 反过来：项目资产上那个勾选框必须在（否则"永远不显示"也能绿）
        go(cdp, f"{BASE}/#assets/0/a{seed['mid']}")
        wait_css(cdp, "#ad-name")
        time.sleep(0.5)
        check("项目资产上「升为全局」勾选框在（不是永远不显示）",
              bool(P.has(cdp, "#ad-global")))

        # ── 交棒：详情 → 资产创作选中那一条 ────────────────────────────
        # 走 setStudioTarget／takeStudioTarget（asset-workspaces.js:43、49）。
        # 这里真出过缺陷的形态就是"只切了工作区却没交出目标"，页面停在草稿上。
        go(cdp, f"{BASE}/#assets/0/a{seed['rich']}")
        wait_css(cdp, "[data-goto-studio]")
        time.sleep(0.5)
        P.eval_js(cdp, "(() => {const b=document.querySelector('[data-goto-studio]');"
                       " if(b) b.click(); return true})()")
        time.sleep(1.6)
        handed = P.eval_js(cdp, "document.querySelector('#as-target')?.value")
        check("详情页交棒到资产创作：工作区切过去且选中那一条资产",
              P.eval_js(cdp, "location.hash") == "#assets/1"
              and str(handed) == str(seed["rich"]),
              f"hash={P.eval_js(cdp, 'location.hash')} / #as-target={handed}")

        # ── 分诊页 ─────────────────────────────────────────────────────
        go(cdp, f"{BASE}/#assets/2")
        wait_css(cdp, "#as-items")
        time.sleep(1.0)

        # suggest()：同名与别名命中必须说成两件不同的事。
        # 判据只有字面相等一条（asset-triage.js:52），所以「小雨」只能算别名命中。
        # 认行不认字：两条候选的 chip 文案一模一样，只能靠 data-item 分。
        alias_row = item_text(cdp, seed["item_alias"]) or ""
        same_row = item_text(cdp, seed["item_rich"]) or ""
        check("别名命中的候选在它自己那一行说「别名命中」",
              "像是已有的「阿雨」" in alias_row and "别名命中" in alias_row
              and "同名" not in alias_row,
              alias_row.replace("\n", " ")[:100])
        check("同名候选在它自己那一行说「同名」",
              "像是已有的「阿雨」" in same_row and "同名" in same_row
              and "别名命中" not in same_row,
              same_row.replace("\n", " ")[:100])

        # 没有原文证据的候选：服务端不会让它进资产，页面必须先说清。
        # 反例同时要验：有证据的那条**不能**也挂这句话——否则一句
        # 恒显示的文案就能让断言绿。
        scene_row = item_text(cdp, seed["item_scene"]) or ""
        evidence_row = item_text(cdp, seed["item_prop"]) or ""
        check("没有原文证据的候选明说不会进资产，有证据的不说这句",
              "这一条没有原文证据，服务端不会让它进资产" in scene_row
              and "原文第" not in scene_row
              and "这一条没有原文证据" not in evidence_row
              and "原文第 13—15 字" in evidence_row,
              scene_row.replace("\n", " ")[:100])

        # 镜头候选：只给导出，不给接受／拒绝。
        shot_row = P.eval_js(cdp, f"""(() => {{
          const el = document.querySelector('#as-item-{seed['item_shot']}');
          return el ? {{accept: !!el.querySelector('[data-accept]'),
                        reject: !!el.querySelector('[data-reject]'),
                        merge: !!el.querySelector('[data-merge]')}} : null;
        }})()""")
        md_href = P.eval_js(cdp, "document.querySelector('#as-shots-md')?.getAttribute('href')")
        check("镜头候选只给 Markdown 导出，不给接受／拒绝／合并钮",
              shot_row and not shot_row["accept"] and not shot_row["reject"]
              and not shot_row["merge"]
              and str(md_href or "").endswith(f"/extract/runs/{seed['run_id']}/shots.md"),
              f"{shot_row} / href={md_href}")

        # 已处理的候选：一次一条，刷新后以服务端状态为准。
        done_row = P.eval_js(cdp, f"""(() => {{
          const el = document.querySelector('#as-item-{seed['item_rejected']}');
          return el ? {{buttons: [...el.querySelectorAll('button')].length,
                        text: el.innerText}} : null;
        }})()""")
        check("已处理的候选不再给任何按钮",
              done_row and done_row["buttons"] == 0 and "已驳回" in done_row["text"],
              str(done_row and done_row["text"].replace("\n", " ")[:70]))

        # 分组必须按 角色→场景→道具→镜头（ITEM_ORDER 只决定显示顺序）。
        # 2026-11-10 起资产类型统一叫「角色」（与服务端 `ASSET_KINDS` 一致），
        # 原来这里写的是「人物」——术语统一时必须同步改这条验收，否则面板
        # 会一直红着，而红的原因还是"界面改对了"。
        groups = P.eval_js(cdp, "[...document.querySelectorAll('.grp')]"
                               ".map(e => e.innerText.trim())") or []
        check("分组按 角色→场景→道具→镜头 排，且每条数对得上",
              groups == ["角色 · 4 条", "场景 · 1 条", "道具 · 1 条", "镜头 · 1 条"],
              str(groups))

        # ── mergePlan 的两个方向 ───────────────────────────────────────
        def open_merge(item_id: int, target_name: str | None):
            return P.eval_js(cdp, f"""(() => {{
              const item = document.querySelector('#as-item-{item_id}');
              if (!item) return 'no-item';
              const sel = item.querySelector('[data-merge-sel]');
              if (!sel) return 'no-sel';
              if ({json.dumps(target_name)} !== null) {{
                const opt = [...sel.options]
                  .find(o => o.innerText.trim() === {json.dumps(target_name)});
                if (!opt) return 'no-opt';
                sel.value = opt.value;
              }}
              const btn = item.querySelector('[data-merge]');
              if (btn) btn.click();
              return 'ok';
            }})()""")

        def dialog_text():
            return " ".join(P.eval_js(
                cdp, "[...document.querySelectorAll('.modal')]"
                     ".filter(m => !m.hidden).map(m => m.innerText)") or [])

        def close_dialog():
            P.eval_js(cdp, "(() => {const b=document.querySelector('[data-d-cancel]');"
                           " if(b) b.click(); return true})()")
            time.sleep(0.4)

        # ① 合并到两格都空的目标：「空人物」候选同名命中「空人物」，不指定
        #    target 就用 suggest 预选的那条——正是"两格都空"。
        assert open_merge(seed["item_empty"], None) == "ok"
        time.sleep(0.7)
        dlg = dialog_text()
        check("合并前说清「会补」哪两格（目标两格都空）",
              "会补：简介、一致性提示词" in dlg, dlg[:190])
        check("合并前把候选别名列进「别名追加」",
              "别名追加：旧名" in dlg, dlg[:190])
        close_dialog()

        # ② 合并到两格都满的目标：「小雨」预选的是「阿雨」（两格都有内容），
        #    且「小雨」本来就在阿雨的别名里 → 不该说"会补"，也不该重复追加别名。
        assert open_merge(seed["item_alias"], None) == "ok"
        time.sleep(0.7)
        dlg2 = dialog_text()
        check("合并前说清「不动」哪两格（已有内容不覆盖）",
              "不动：简介（已有，不会覆盖）" in dlg2
              and "不动" in dlg2 and "一致性提示词（已有，不会覆盖）" in dlg2, dlg2[:210])
        check("目标两格都满、候选名已是其别名时：不补、不追加别名",
              "没有空格子可补" in dlg2 and "别名不追加" in dlg2, dlg2[:210])
        close_dialog()

        # ── 工作区顺序：两个文件之间的约定 ─────────────────────────────
        # 页内 chip 读 asset-workspaces.js 的 WORKSPACES，侧栏读 modules.js 的
        # assets.subs。两边反着写的结果是"高亮资产库、页面摆的是候选"。
        chips_txt = P.eval_js(cdp, "[...document.querySelectorAll('[data-sub]')]"
                                   ".map(e => e.innerText.trim())") or []
        nav_txt = P.eval_js(cdp, """(() => {
          const g = [...document.querySelectorAll('.nav-group')]
            .find(x => x.querySelector('[data-v="assets"]'));
          return g ? [...g.querySelectorAll('.nav-sub .sub')].map(e => e.innerText.trim())
                   : null;
        })()""")
        check("页内工作区顺序与侧栏二级导航一字不差",
              chips_txt == nav_txt == ["资产库", "资产创作", "文本提取"],
              f"页内={chips_txt} / 侧栏={nav_txt}")
        active = P.eval_js(cdp, "[...document.querySelectorAll('[data-sub]')]"
                                ".findIndex(e => e.classList.contains('blue'))")
        check("当前工作区高亮的是「文本提取」（下标 2）", active == 2, f"blue 下标={active}")

        # ── wsIndex 越界：钳回第一个工作区 ─────────────────────────────
        # 撤掉某个二级页之后旧地址仍可能被粘过来，钳不回就会渲染出一个
        # 高亮和内容对不上的页面（router.js:76 那道钳位）。
        go(cdp, f"{BASE}/#assets/9")
        wait_css(cdp, "#asset-count")
        time.sleep(0.8)
        check("越界的二级下标钳回第一个工作区",
              P.eval_js(cdp, "location.hash") == "#assets/0"
              and P.eval_js(cdp, "[...document.querySelectorAll('[data-sub]')]"
                                 ".findIndex(e => e.classList.contains('blue'))") == 0,
              f"hash={P.eval_js(cdp, 'location.hash')}")

        errors = P.eval_js(cdp, "(window.__fvErrors||[])")
        check("整轮没有 JS 异常", not errors, str(errors)[:220])
    finally:
        if chrome is not None:
            try:
                chrome.terminate()
            except Exception:
                pass
        H.stop(proc)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


# 变异清单：每条都在本机真跑过一遍（改一行 → 跑 → 看到指定的那条红 → 改回）。
# 不做这一步的"全绿"没有信息量：断言写错的绿与被测代码正确的绿长得一模一样。
MUTATIONS = """
1. asset-workspaces.js:90  missingOf 删掉「参考图」那一行
     → 「还缺什么」算得准：资料齐的只剩参考图一项            红
2. asset-workspaces.js:99  道具分支改成 '至少一个视角形态'
     → 道具的形态缺项说「状态形态」而不是「服饰形态」        红
3. asset-triage.js:109  同名／别名那个三元改成写死 '同名'
     → 别名命中的候选在它自己那一行说「别名命中」            红
4. asset-triage.js:68   mergePlan 把 !(target.summary) 改成 target.summary
     → 合并前说清「会补」哪两格（目标两格都空）              红
5. asset-workspaces.js:14  WORKSPACES 把 library 与 studio 对调
     → 页内工作区顺序与侧栏二级导航一字不差                  红
6. asset-detail.js:302  删除确认钮去掉 ${a.ref_shot_count ? ' disabled' : ''}
     → 被引用时删除弹窗摆出引用数并禁用确认钮                红
7. asset-detail.js:329  去掉 setStudioTarget(cache.asset.id) 那一行
     → 详情页交棒到资产创作：工作区切过去且选中那一条资产    红
8. asset-triage.js:110  镜头也放进"可接受"那一支（去掉 kind !== 'shot'）
     → 镜头候选只给 Markdown 导出，不给接受／拒绝／合并钮    红
"""


if __name__ == "__main__":
    sys.exit(main())
