#!/usr/bin/env python3
"""智能体中心的真浏览器验收：起一个独立实例，点四个工作区并走完一次签字。

为什么不用 :8000 上那个实例（与模型中心那轮同一条理由）：
  1. 库副本 /tmp/ag_browser/platform.db —— 迁移与造数据都发生在副本上，
     不替你把 0041 跑到真开发库上，也不在真项目里造资产；
  2. `FASTVIDEO_WORKSPACE / FILES / JOBS` 全指临时目录，
     否则测试文档会被 git 提交进真正的稿子目录（conftest 里就写了这条教训）。

一次真付费调用都不发：
  发起弹窗那几条只测**提交前**的读与拒（云端没勾确认 ⇒ 422，钱一分没花）；
  步骤与签字用一个**直接造好的运行**走真队列之外的路径——
  `agents.tick()` 在本脚本进程里调，与 worker 调的是同一个函数。
  "真模型算出的计划"不靠这条浏览器验收冒充，它记在交付报告里。

用法：python3 tools/agents_browser_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H

BACKEND = H.BACKEND
TMP = H.temp_dir("fv-ag-")
PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

COLLECT_ERRORS = """
window.__agErrors = [];
window.addEventListener('error', (e) => window.__agErrors.push(String(e.message)));
window.addEventListener('unhandledrejection', (e) =>
    window.__agErrors.push('promise: ' + String(e.reason)));
"""


def api_post(path, token, data=None):
    """以登录态调一次写接口（这里只用来"打开项目"，把当前项目定在验收那一个）。"""
    import urllib.request

    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=body, method="POST",
        headers={"Cookie": f"fv_session={token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def api_get(path, token):
    """从接口取"那一份答案"，与页面显示的比。

    为什么不在页面里 fetch：`proto_cdp_check.eval_js` 不等 Promise，
    在页面里发请求拿到的是个 Pending 对象——那不是验证，那是假绿灯。
    """
    import urllib.request

    req = urllib.request.Request(BASE + path, headers={"Cookie": f"fv_session={token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def prepare_env():
    """必须在 import 后端模块之前设好：config.py 在导入时就把环境变量读成常量。"""
    # TMP 由 `_harness.temp_dir` 每次新建（随机后缀），不需要再清一遍；
    # 原来写死 /tmp/ag_browser，重启后上一轮的库还在，只能先 rmtree。
    TMP.mkdir(parents=True, exist_ok=True)
    # 顺序要紧：config.py 在 import 时就把这些读成常量，所以先设环境变量，
    # 再迁库；否则迁移会跑到默认的 backend/data/platform.db 上。
    os.environ["FASTVIDEO_DB"] = str(TMP / "platform.db")
    os.environ["FASTVIDEO_FILES"] = str(TMP / "files")
    os.environ["FASTVIDEO_JOBS"] = str(TMP / "jobs")
    os.environ["FASTVIDEO_WORKSPACE"] = str(TMP / "workspace")
    # 有现役库就复制副本；没有（CI 的干净 runner）就从迁移起一个并建 admin。
    H.prepare_backend_db(TMP / "platform.db")


def mint_admin_session():
    """给副本库里那个 admin 账号铸一个会话：不猜口令，也不改口令。"""
    import sqlite3
    from datetime import datetime, timedelta

    from db import get_conn
    from repositories.users import sessions, users
    from security import new_session_token

    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.id FROM app_user u
                     JOIN user_role ur ON ur.user_id = u.id
                     JOIN role r ON r.id = ur.role_id
                    WHERE r.code = 'admin' AND u.is_active = 1
                    ORDER BY u.is_superuser DESC, u.id LIMIT 1""").fetchone()
    if row is None:
        raise SystemExit("副本库里没有可用的 admin 账号，验收没法继续")
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, row["id"], expires)
    return token, users.get_by_id(row["id"])


def seed_scene(admin):
    """直接造一条"计划已回来、一步做完、一步等你批"的运行。

    为什么不靠真模型跑出来：那是一次付费的云端调用，验收不该花人的钱。
    这里造的是**数据形状**，之后的每一步（tick、签字、状态变化）
    走的都是生产代码，没有任何一页 HTML 是写死的。
    """
    import agents
    import agent_tools
    from repositories.agents import agent_runs
    from repositories.content import episodes as episode_repo
    from repositories.projects import projects as project_repo

    pid = project_repo.create("智能体验收_副本", "", [], admin["id"])["id"]
    eid = episode_repo.create(pid, "第 1 集")["id"]
    plan = [
        {"tool": "episode_preflight", "args": {"episode_id": eid}, "why": "先看缺什么"},
        {"tool": "asset_register",
         "args": {"kind": "character", "name": "浏览器验收登记的角色"}, "why": "补上缺的角色"},
    ]
    normalized = []
    for seq, item in enumerate(plan, start=1):
        args = agent_tools.validate(item["tool"], item["args"])
        normalized.append({"seq": seq, "tool": item["tool"], "args": args,
                           "args_hash": agent_tools.args_hash(item["tool"], args),
                           "risk": agent_tools.risk_of(item["tool"]), "why": item["why"]})
    run = agent_runs.create({
        "project_id": pid, "episode_id": eid,
        "goal": "盘点这一集缺什么，并把缺的角色登记上",
        "scene_key": "character_bio",
        "model_snapshot": {"capability": "text", "backend": "cloud",
                           "provider": "deepseek", "model": "deepseek-flash",
                           "route_source": "provider_config.is_default"},
        "approval_valid_seconds": 86400, "created_by": admin["id"]})
    agent_runs.set_plan(run["id"], normalized)
    agent_runs.set_plan_status(run["id"], "ready")
    for item in normalized:
        agent_runs.add_step(run["id"], item["seq"], tool_code=item["tool"],
                            args=item["args"], args_hash=item["args_hash"],
                            risk=item["risk"])
    agents.tick()                          # 只读那步当场做完，写那步停在等审批
    fresh = agent_runs.get(run["id"])
    print("  seed 后：run.cancelled =", fresh["cancelled"], "finished =", fresh["finished_at"],
          "| steps:", [(s["seq"], s["status"]) for s in agent_runs.steps(run["id"])],
          flush=True)
    return {"project_id": pid, "episode_id": eid, "run_id": run["id"],
            "plan": normalized}


def start_service():
    return H.start_service(PORT)


def start_chrome(port):
    return H.launch_chrome(port)


def wait_server():
    H.wait_http(f"{BASE}/api/health")


def go(cdp, url):
    """换页必须整页重载：应用只在启动时读一次 location.hash（同模型中心那条教训）。"""
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.5)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.6)


def main():
    prepare_env()
    token, admin = mint_admin_session()
    scene = seed_scene(admin)

    proc = start_service()
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}"
              + (f"  · {detail}" if detail else ""), flush=True)

    chrome = None
    try:
        wait_server()
        import proto_cdp_check as P

        chrome = start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=COLLECT_ERRORS)
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=BASE, path="/", httpOnly=True)
        cdp.cmd("Runtime.enable")

        # 验收造的运行属于副本里那一个新项目，而「运行与审批」「动作审批」
        # 两栏都是按**当前项目**筛的（不是按全库筛）。先把当前项目打开，
        # 否则页面会诚实地说"这个项目还没有运行"——那不是缺陷，是我没选中。
        api_post(f"/api/projects/{scene['project_id']}/open", token)

        def dump(tag):
            if os.environ.get("AG_DUMP"):
                text = P.eval_js(cdp, "document.querySelector('.view.on')?.innerText || ''")
                print(f"---- DOM[{tag}] ----\n{(text or '')[:900]}\n", flush=True)

        # ---------- 导航与结构 ----------
        go(cdp, f"{BASE}/#agents")
        dump("agents/0")
        titles = P.eval_js(cdp, "[...document.querySelectorAll('.nav-section-t')]"
                                ".map(e => e.textContent)")
        check("侧栏仍是五组（没为智能体新开一组）",
              isinstance(titles, list) and len(titles) == 5, "、".join(titles or []))
        labels = P.eval_js(cdp, "[...document.querySelectorAll('.nav-item')]"
                                ".map(e => e.textContent.trim())")
        check("「智能体中心」进了侧栏，且原有一级菜单一个没少",
              "智能体中心" in (labels or []) and len(labels or []) == 14,
              f"{len(labels or [])} 项：{'、'.join(labels or [])}")
        utility = P.eval_js(cdp, "[...document.querySelectorAll('.nav-group.utility "
                                 ".nav-item')].map(e => e.textContent.trim())")
        check("平台能力组里三件事挨着排（模型中心／智能体中心／系统设置）",
              utility == ["模型中心", "智能体中心", "系统设置"], str(utility))
        check("页面标题是智能体中心", P.has(cdp, ".page-head h1")
              and "智能体中心" in P.text_of(cdp, ".page-head h1"),
              P.text_of(cdp, ".page-head h1"))
        chips = P.eval_js(cdp, "[...document.querySelectorAll('.view.on .sub-chip')]"
                                ".map(e => e.textContent.trim())")
        check("四个工作区切换器齐名",
              chips == ["总览", "智能体配置", "任务路由", "运行与审批"], str(chips))

        # ---------- 总览 ----------
        stats = P.eval_js(cdp, "[...document.querySelectorAll('.view.on .stat')]")
        stat_vals = P.eval_js(cdp, "[...document.querySelectorAll('.view.on .stat-n')]"
                                   ".map(e => e.textContent.trim())")
        check("四个数真的渲染出来", len(stat_vals or []) == 4, str(stat_vals))
        api_overview = api_get("/api/agent/overview", token)
        check("第一个数与服务端一致（界面不自己算）",
              str(api_overview["counts"]["runs"]) == str(stat_vals[0]),
              f"页面 {stat_vals[0]}｜接口 {api_overview['counts']['runs']}")
        caliber_text = P.eval_js(cdp, "document.querySelector('.view.on .card .sub')"
                                      "?.textContent || ''")
        check("口径那句话和接口给的是同一句",
              api_overview["caliber"][:12] in (caliber_text or ""),
              (caliber_text or "")[:44])
        legs = P.eval_js(cdp, "document.querySelector('.view.on table')?.querySelectorAll("
                              "'tbody tr').length ?? 0")
        check("两条腿两行（本地／云端）", legs == 2, f"{legs} 行")
        run_rows = P.eval_js(cdp, "[...document.querySelectorAll('[data-open-run]')].length")
        check("造出来的运行出现在列表里", isinstance(run_rows, int) and run_rows >= 1,
              f"{run_rows} 行")

        # ---------- 配置页 ----------
        go(cdp, f"{BASE}/#agents/1")
        dump("agents/1")
        tools_api = api_get("/api/agent/tools", token)
        # 按表头认出"工具白名单"那张表，而不是按下标取第几张——
        # 页面上表格的顺序以后会变，按下标写的验收会变绿得莫名其妙
        rows = P.eval_js(cdp, "(() => { const t = [...document.querySelectorAll('.view.on table')]"
                              ".find(x => x.innerText.includes('往外送什么'));"
                              "return t ? t.querySelectorAll('tbody tr').length : 0; })()")
        want = len(tools_api["items"])
        check("工具白名单的行数等于接口给的那几行", rows == want, f"页面 {rows}｜接口 {want}")
        body = P.eval_js(cdp, "document.querySelector('.view.on').innerText") or ""
        check("页面上明写没有任意 SQL／Shell／Python",
              "SQL" in body and "Shell" in body and "Python" in body)
        secret = os.environ.get("DEEPSEEK_API_KEY") or ""
        check("密钥值不出现在配置页（只显示变量名与有没有值）",
              (not secret) or secret not in body,
              "环境变量里有值，比对过页面全文")
        check("DEEPSEEK_API_KEY 这个变量名是可以念出来的",
              "DEEPSEEK_API_KEY" in body or "不需要密钥" in body)
        check("驻留那一栏分开说了两族（服务型／子进程型），没混成一个状态",
              "驻留" in body)
        check("有「去模型中心」的跳转而不是第二个配置表单",
              P.has(cdp, "[data-goto-models]") and "新建智能体" not in body)

        # ---------- 任务路由页：只读＋跳转 ----------
        go(cdp, f"{BASE}/#agents/2")
        pol = api_get("/api/model-policies", token)
        scene_rows = P.eval_js(cdp, "document.querySelectorAll('.view.on tbody tr').length")
        check("九个场景都在（读的是模型中心那张表，没另建一份）",
              scene_rows == len(pol["items"]) == 9,
              f"页面 {scene_rows}｜接口 {len(pol['items'])}")
        route_body = P.eval_js(cdp, "document.querySelector('.view.on').innerText") or ""
        check("备用轨不会自动顶上这句话由清单驱动显示在页面上",
              "备用轨不会自动顶上" in route_body)
        check("策略这一页没有第二个「保存」按钮（改在模型中心）",
              P.eval_js(cdp, "[...document.querySelectorAll('.view.on button')]"
                             ".filter(b => /保存/.test(b.textContent)).length") == 0)
        P.click(cdp, "[data-goto-models]")
        time.sleep(0.8)
        hash_after = P.eval_js(cdp, "location.hash")
        check("点「去模型中心·调用策略」真的落到那一页",
              hash_after == "#models/2", str(hash_after))

        # ---------- 运行与审批：详情与签字 ----------
        go(cdp, f"{BASE}/#agents/3")
        dump("agents/3")
        detail_btns = P.eval_js(cdp, "[...document.querySelectorAll('[data-open-run]')]")
        check("运行列表有详情按钮", isinstance(detail_btns, list) and detail_btns,
              f"{len(detail_btns or [])} 条")
        P.eval_js(cdp, "document.querySelector('[data-open-run]').click()")
        time.sleep(2.0)
        detail = P.eval_js(cdp, "document.querySelector('#ag-run-detail')?.innerText || ''")
        check("详情里第一步显示成功、第二步显示等你审批",
              "成功" in detail and "等你审批" in detail, detail[:60].replace("\n", " "))
        check("待审批那一栏说清了这里不能签",
              "不能签" in detail or "审核中心" in detail)
        # 这条原来写成"页面上要有『去生成中心』"，但本轮造的这条运行
        # 没有需要排队写作业的步骤（登记资产是同步做完的），那句自然不出现——
        # 那是验收自己的假设错了，不是页面的错。改量真正要守的东西：
        # 步骤表里只有"关联作业"这一个指针，没有成本／日志／耗时这几列。
        step_headers = P.eval_js(cdp, "[...document.querySelectorAll('#ag-run-detail table thead th')]"
                                      ".map(e => e.textContent.trim())")
        check("步骤表只给关联作业的指针，不抄成本与日志",
              "关联作业" in (step_headers or [])
              and not any(h in (step_headers or []) for h in ("成本", "日志", "耗时")),
              str(step_headers))

        # 审核中心第三栏签字
        go(cdp, f"{BASE}/#review/2")
        review_chips = P.eval_js(cdp, "[...document.querySelectorAll('.view.on .sub-chip')]"
                                       ".map(e => e.textContent.trim())")
        check("审核中心三个页签（动作审批是第三个）",
              review_chips == ["文档审核", "产物审核", "动作审批"], str(review_chips))
        action_rows = P.eval_js(cdp, "document.querySelectorAll('[data-open-action]').length")
        check("待批动作列出了那一步", isinstance(action_rows, int) and action_rows >= 1,
              f"{action_rows} 行")
        P.eval_js(cdp, "document.querySelector('[data-open-action]').click()")
        time.sleep(2.0)
        card = P.eval_js(cdp, "document.querySelector('#action-detail')?.innerText || ''")
        check("审批卡把参数、送出去什么、怎么验收、有效期四样摊开",
              all(k in card for k in ("往外送什么", "怎么验收", "参数指纹")),
              card[:70].replace("\n", " "))
        check("审批卡明写批准绑的是参数指纹",
              "指纹" in card and ("作废" in card or "失效" in card))
        P.eval_js(cdp, "[...document.querySelectorAll('#action-detail [data-sign]')]"
                       ".find(b => b.dataset.sign === 'approved')?.click()")
        time.sleep(2.2)
        signed = P.eval_js(cdp, "document.querySelector('#action-detail')?.innerText || ''")
        check("签完这一卡收掉了（步骤不再出现在等人批那一列）",
              "等你审批" not in signed or "没有" in signed or signed == "",
              signed[:60].replace("\n", " "))

        # 由脚本进程调一次 tick（与 worker 调的是同一个函数），再看页面
        import agents
        agents.tick()
        go(cdp, f"{BASE}/#agents/3")
        P.eval_js(cdp, "document.querySelector('[data-open-run]').click()")
        time.sleep(2.0)
        after = P.eval_js(cdp, "document.querySelector('#ag-run-detail')?.innerText || ''")
        check("批准后由推进节拍执行，页面第二步变成成功",
              "等你审批" not in after and after.count("成功") >= 2,
              after[:70].replace("\n", " "))
        listed = api_get(f"/api/projects/{scene['project_id']}/assets", token)
        items = listed if isinstance(listed, list) else listed.get("items", [])
        names = [a.get("name") for a in items]
        check("那一步真的写进了业务表（不是只在步骤里写了句成功）",
              "浏览器验收登记的角色" in names, str(names)[:60])

        # ---------- 发起弹窗：只测提交前的读与拒 ----------
        go(cdp, f"{BASE}/#agents/3")
        P.eval_js(cdp, "document.querySelector('[data-ag-new]')?.click()")
        time.sleep(1.6)
        check("发起弹窗能打开", P.has(cdp, "#ag-modal:not([hidden])"))
        route_line = P.eval_js(cdp, "document.querySelector('#ag-route')?.innerText || ''")
        check("目标还短时先把话说住，不去读一条注定失败的路线",
              "写清楚" in route_line, route_line[:50])
        P.eval_js(cdp, "(() => { const g = document.querySelector('#ag-goal');"
                       "g.value = '盘点这一集缺哪些资产';"
                       "g.dispatchEvent(new Event('input')); return 1; })()")
        time.sleep(2.0)
        route_line2 = P.eval_js(cdp, "document.querySelector('#ag-route')?.innerText || ''")
        check("填了目标就念出会走哪条轨、往外送什么、按什么计价",
              "算这次计划会走" in route_line2 and "不外发" in route_line2
              or "发给" in route_line2, route_line2[:80].replace("\n", " "))
        cloud_box = P.eval_js(cdp, "!!document.querySelector('#ag-cloud')")
        if cloud_box:
            check("云端那一档必须勾确认才放行",
                  P.eval_js(cdp, "!document.querySelector('#ag-cloud').checked"))
            P.eval_js(cdp, "document.querySelector('[data-ag-submit]').click()")
            time.sleep(1.8)
            err = P.eval_js(cdp, "document.querySelector('#ag-modal-err')?.innerText || ''")
            check("没勾确认时发起被拒且原话可见（一分钱没花）",
                  "确认" in err, err[:70])
        else:
            check("本地轨时界面对本地处理这一句负责", "本地" in route_line2)
        # Escape 关掉弹窗（与前几轮同一套危险确认标准）
        cdp.cmd("Input.dispatchKeyEvent", type="keyDown", key="Escape",
                code="Escape", windowsVirtualKeyCode=27)
        time.sleep(0.6)
        check("Escape 能关发起弹窗",
              P.eval_js(cdp, "document.querySelector('#ag-modal')?.hidden !== false"))

        # ---------- 窄屏与整页无异常 ----------
        go(cdp, f"{BASE}/#agents/0")
        P.eval_js(cdp, "(() => { const s = document.createElement('style');"
                       "s.textContent = '.app{grid-template-columns:75px 1fr}';"
                       "document.head.appendChild(s); return 1; })()")
        overflow = P.eval_js(cdp, "(() => { const t = document.querySelector('.view.on "
                                  ".table-wrap'); return t ? t.scrollWidth - t.clientWidth : 0; })()")
        check("表格在窄容器里横向滚而不是撑破页面",
              isinstance(overflow, (int, float)), f"溢出 {overflow}px")
        page_errors = P.eval_js(cdp, "window.__agErrors || []")
        check("整页没有 JS 异常", not page_errors, str(page_errors)[:120])

    finally:
        for p in (proc, chrome):
            if p is not None:
                p.terminate()
        try:
            import urllib.request
            urllib.request.urlopen(f"{BASE}/api/health", timeout=1)
        except Exception:
            pass
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    for name in bad:
        print("  FAIL " + name)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
