#!/usr/bin/env python3
"""项目生产工作台与镜头工作台的真浏览器验收（0037 P0-5）。

为什么必须单独一个验收脚本而不是只看 pytest：
这一批改动的全部价值在于"人打开这一屏，看得见进度、点得动下一步"。
接口返回正确但页面渲染不出、按钮绑不上、窄屏溢出，pytest 一个都看不见。

复用 `model_center_browser_check` 的壳（库副本 + 独立端口 + 临时工作区）：
**不动开发库**，也不往真正的稿子目录写东西——conftest 里那条教训说过一次。

用法：python3 tools/production_browser_check.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import model_center_browser_check as mc     # noqa: E402  复用同一套壳
import proto_cdp_check as P                 # noqa: E402

BASE = mc.BASE
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  OK  " if ok else "  FAIL") + f" {name}"
          + (f"  · {detail}" if detail else ""), flush=True)


def api(path, token, method="GET", body=None):
    """用同一个 admin 会话打接口，把验收要的数据真建出来。

    不用 sqlite 直插：那样验的是"我造的数据"，不是"接口能不能造出这条路"。
    """
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          "Cookie": f"fv_session={token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def seed(token):
    project = api("/api/projects", token, "POST",
                  {"name": "验收·工作台", "description": "", "tags": []})
    episode = api(f"/api/projects/{project['id']}/episodes", token, "POST",
                  {"title": "第 1 集"})
    shots = [api(f"/api/episodes/{episode['id']}/shots", token, "POST",
                 {"summary": f"第 {i + 1} 镜", "prompt": "崖顶黄昏", "duration": 5,
                  "asset_ids": []}) for i in range(3)]
    asset = api(f"/api/projects/{project['id']}/assets", token, "POST",
                {"kind": "character", "name": "阿昭", "summary": "",
                 "consistency_prompt": "", "aliases": [], "tags": [],
                 "scope": "project"})
    api(f"/api/shots/{shots[0]['id']}", token, "PUT",
        {"summary": "第 1 镜", "prompt": "崖顶黄昏", "duration": 5,
         "asset_ids": [asset["id"]], "shot_size": "全景", "camera_move": "推",
         "aspect": "16:9", "style": "平涂", "negative_prompt": ""})
    # 把上下文钉在这次新建的项目×集上：副本库里还躺着开发库带来的别的项目，
    # 不钉住的话页面按"最近打开"选中的是旧项目，验收就会看到一片 0
    api("/api/context", token, "PUT",
        {"project_id": project["id"], "episode_id": episode["id"]})
    # 一条注定失败的真作业：真队列、真 worker、真错误码
    bad = api("/api/jobs", token, "POST", {
        "project_id": project["id"], "episode_id": episode["id"],
        "shot_id": shots[0]["id"], "capability": "video",
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "prompt": "这条要失败", "params": {"sleep": 0.1, "fail": True}})
    for _ in range(40):
        state = api(f"/api/jobs/{bad['id']}", token).get("status")
        if state in ("failed", "succeeded", "needs_attention"):
            break
        time.sleep(0.5)
    # 一条会成功的作业（假适配器产真 mp4）：审核队列只收「已成功且有产物」的行，
    # 没有它就验不到质量清单那一屏
    good = api("/api/jobs", token, "POST", {
        "project_id": project["id"], "episode_id": episode["id"],
        "shot_id": shots[1]["id"], "capability": "video",
        "execution_mode": "local", "provider_code": "mock", "model_code": "mock-1",
        "prompt": "这条要成功", "params": {"sleep": 0.1, "media": "mp4",
                                            "color": "blue", "seconds": 1.0}})
    for _ in range(60):
        state = api(f"/api/jobs/{good['id']}", token).get("status")
        if state in ("failed", "succeeded", "needs_attention"):
            break
        time.sleep(0.5)
    # 一条台词素材：音频中心那页要验「能力状态透明化」，台账里得先有东西
    api(f"/api/projects/{project['id']}/audio-clips", token, "POST",
        {"episode_id": episode["id"], "shot_id": shots[0]["id"],
         "kind": "dialogue", "speaker": "掌灯人", "text": "你迟了三年。"})
    # 产线这一腿要验两件事，数据得先摆好：
    #   1) 发起弹窗读的是**统一预检**——所以种一集「有镜头但没提示词」的，
    #      那条提醒才会出现，而它带的是「去这一镜」的跳转；
    #   2) 节点失败要给同一张七字段目录——`check_shots` 在建运行时就会当场判失败
    #      （不需要 worker），于是运行详情里那一格是现成的对象。
    ep2 = api(f"/api/projects/{project['id']}/episodes", token, "POST",
              {"title": "第 2 集"})
    api(f"/api/episodes/{ep2['id']}/shots", token, "POST",
        {"summary": "没有提示词的一镜", "prompt": "", "duration": 5, "asset_ids": []})
    templates = api("/api/pipeline/templates", token)
    tpl = next(t for t in templates
               if t.get("name") == "标准漫剧单集生产" and t.get("version") == 2)
    run = api(f"/api/projects/{project['id']}/pipeline/runs", token, "POST",
              {"episode_id": ep2["id"], "template_id": tpl["id"],
               "idempotency_key": "browser-p1-run"})
    # 第 3 集一条镜头都没有 ⇒ 预检只会给「不能发起」；第 4 集有镜头但缺提示词
    # ⇒ 只给警告。两集对着验才看得出那颗钮真是按 `can_start` 走的，
    # 而不是"清单里只要有一行就禁用"。
    ep3 = api(f"/api/projects/{project['id']}/episodes", token, "POST",
              {"title": "第 3 集"})
    ep4 = api(f"/api/projects/{project['id']}/episodes", token, "POST",
              {"title": "第 4 集"})
    bare = api(f"/api/episodes/{ep4['id']}/shots", token, "POST",
               {"summary": "没有提示词的一镜", "prompt": "", "duration": 5,
                "asset_ids": []})
    return {"project": project, "episode": episode, "shots": shots, "asset": asset,
            "failed_job": bad, "good_job": good, "ep2": ep2, "ep3": ep3, "ep4": ep4,
            "bare_shot": bare, "run": run, "template_id": tpl["id"]}


def main():
    mc.prepare_env()
    token = mc.mint_admin_session()
    # 假适配器轨（只在 ALLOW_MOCK=1 时存在）：本轮要现场验一条真失败的作业在界面上
    # 怎么说自己——`mock_failed` 是刻意没进异常目录的码，正好用来验
    # "认不出的码必须承认认不出"这条纪律在真界面上成不成立。
    os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"

    import subprocess

    proc = subprocess.Popen(
        [str(mc.H.backend_python(mc.BACKEND)), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(mc.PORT), "--log-level", "warning"],
        cwd=str(mc.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    data = None
    try:
        mc.wait_server()
        worker = subprocess.Popen(
            [str(mc.H.backend_python(mc.BACKEND)), "worker.py", "--poll", "0.2"],
            cwd=str(mc.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(1.2)
        # 建数据必须在服务起来之后：走的是真接口，不是直插 sqlite——
        # 那样验的才是"这条路真能走通"，而不是"我造的数据能被读出来"
        data = seed(token)
        chrome = mc.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=mc.COLLECT_ERRORS)
        cdp.cmd("Runtime.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=BASE, path="/", httpOnly=True)

        shot_id = data["shots"][0]["id"]
        ep_id = data["episode"]["id"]

        # ---------- 导航与上下文 ----------
        mc.go(cdp, f"{BASE}/#projects")
        titles = P.eval_js(cdp, "[...document.querySelectorAll('.nav-section-t')]"
                                ".map(e => e.textContent)")
        check("侧栏按五组排（工作台／创作准备／生产执行／质量与交付／平台能力）",
              isinstance(titles, list) and len(titles) == 5, "、".join(titles or []))
        # 分组标签本身就在那道分隔上，条目之间不该再画线。`utility` 那条上边框比
        # 0037 的分组标签早，留着它会在「平台能力」标签下面与两项之间各画一条横线，
        # 把同一组里的两项硬切开（使用侧指出的就是这个）。
        # 这条量的是线宽，不是"看起来对不对"；顺手把条数也钉住——
        # 万一整组因为权限变化消失了，它不会拿着空列表假装通过。
        utility_lines = P.eval_js(cdp, """(() => {
          const items = [...document.querySelectorAll('.nav-group.utility')];
          const lined = items.filter(e => getComputedStyle(e).borderTopWidth !== '0px').length;
          return items.length + '|' + lined;
        })()""")
        parts = str(utility_lines).split("|")
        # 「2」是这一组当时的条数；0041 加了「智能体中心」变成三条。
        # 这条验收要守的是"一条横线都不画"，不是条数——条数交给下面那条去数名字。
        check("平台能力组里一条横线都不画（标签下面与条目之间都不画）",
              len(parts) == 2 and int(parts[0]) >= 2 and parts[1] == "0",
              f"utility 条目 {parts[0] if parts else '?'} 个／还画着线的 "
              f"{parts[1] if len(parts) > 1 else '?'} 个")

        # 二级菜单的展开／缩回必须是插值。原来 `.nav-sub` 是 `display:none` ↔
        # `display:grid` 两态硬切，浏览器不对 display 插值，侧栏于是"啪"地跳开。
        # 这里不比较两个终态（终态对了也可能是瞬变），而是**每 16ms 采一次高度、
        # 数中间帧**：真在动就会留下一串介于 0 与满高之间的采样值。
        # 为什么用采样而不是"点完立刻量一次"：点击要走一次异步渲染，
        # 类名什么时候翻过去不由这里决定，掐 60ms 量会因渲染快慢时红时绿。
        nav_anim = cdp.eval("""(async () => {
          const wait = (ms) => new Promise(r => setTimeout(r, ms));
          const at = (v) => [...document.querySelectorAll('.nav-item')]
            .find(e => e.dataset.v === v);
          const item = at('assets'), plain = at('projects');
          const sub = item && item.nextElementSibling;
          if (!sub || !plain) return 'no-sub';
          const sample = async (ms) => {
            const vals = [];
            const iv = setInterval(
              () => vals.push(Math.round(sub.getBoundingClientRect().height)), 16);
            await wait(ms); clearInterval(iv); return vals;
          };
          plain.click(); await wait(500);
          const closed = Math.round(sub.getBoundingClientRect().height);
          const visClosed = getComputedStyle(sub).visibility;
          item.click(); const up = await sample(700);
          const opened = Math.round(sub.getBoundingClientRect().height);
          const visOpen = getComputedStyle(sub).visibility;
          plain.click(); const down = await sample(700);
          const top = Math.max(...up, ...down);
          return JSON.stringify({
            closed, opened, top,
            midUp: up.filter(v => v > 0 && v < top).length,
            midDown: down.filter(v => v > 0 && v < top).length,
            visClosed, visOpen,
            prop: getComputedStyle(sub).transitionProperty,
          });
        })()""")
        na = json.loads(nav_anim) if str(nav_anim).startswith("{") else {}
        check("二级菜单展开与缩回都是插值（中间帧采得到）",
              bool(na) and na["closed"] == 0 and na["opened"] > 0
              and na["midUp"] >= 2 and na["midDown"] >= 2,
              f"收起 {na.get('closed')}／展开 {na.get('opened')} 高／展开途中 "
              f"{na.get('midUp')} 帧中间高度、缩回途中 {na.get('midDown')} 帧"
              if na else str(nav_anim))
        # 收起时高度是 0，但节点还在文档里——`display:none` 时代它不在无障碍树里，
        # 读屏不会念这排看不见的路径。换成高度插值后必须用 visibility 把这件事补回来。
        check("过渡挂在高度上，且收起态对读屏不可见（与旧的 display:none 同义）",
              bool(na) and "grid-template-rows" in na["prop"]
              and na["visClosed"] == "hidden" and na["visOpen"] == "visible",
              f"transition-property={na.get('prop','?')}／收起 visibility="
              f"{na.get('visClosed','?')}／展开 visibility={na.get('visOpen','?')}"
              if na else str(nav_anim))
        labels = P.eval_js(cdp, "[...document.querySelectorAll('.nav-item')]"
                                 ".map(e => e.textContent.trim())")
        # 按名字逐条点名，不按条数判：上一版写死 13，加「智能体中心」那天
        # 这条就红了——红的是"数字变了"，而不是"少了菜单"，那种红只会让人学会忽略它。
        must_have = ["模块地图", "项目工作台", "项目中心", "资产中心", "创作中心",
                     "设定中心", "生成中心", "产线中心", "音频中心", "成片中心",
                     "审核中心", "模型中心", "智能体中心", "系统设置"]
        missing = [n for n in must_have if n not in (labels or [])]
        check("一级菜单点名点得齐（含项目工作台与智能体中心），一个都没少",
              not missing and len(labels or []) == len(must_have),
              f"缺 {missing}｜共 {len(labels or [])} 项：{'、'.join(labels or [])}")
        check("顶栏出现当前集上下文", P.has(cdp, "#ep-pick"),
              P.text_of(cdp, "#cur-episode"))

        # 选集：点顶栏 → 出菜单 → 选第 1 集
        P.click(cdp, "#ep-pick")
        check("集菜单能打开并列出这一集", P.has(cdp, "#ep-menu .pm-item"))
        P.click(cdp, "#ep-menu .pm-item")
        time.sleep(1.2)
        shown = P.text_of(cdp, "#cur-episode")
        back = api(f"/api/context", token)
        check("选完集：顶栏写的与服务端存的是同一集",
              shown.startswith("第 1 集") and back.get("episode_id") == ep_id,
              f"顶栏「{shown}」／接口 {back}")

        # ---------- 项目生产工作台 ----------
        mc.go(cdp, f"{BASE}/#production")
        rows = P.eval_js(cdp, "document.querySelectorAll('#view-root table tbody tr').length")
        # 九阶段 9 行 ＋ 各集进度按集数加行；这里只验阶段表本身
        stage_rows = P.eval_js(cdp, """(() => {
          const card = [...document.querySelectorAll('#view-root .card')]
            .find(c => c.textContent.includes('这一集的生产阶段'));
          return card ? card.querySelectorAll('tbody tr').length : 0; })()""")
        check("九阶段一次列全", stage_rows == 9, f"{stage_rows} 行（总 {rows} 行含各集表）")
        head = P.eval_js(cdp, "[...document.querySelectorAll('.mc-strip .st')]"
                              ".map(e => e.textContent.replace(/\\s+/g,' ').trim())")
        check("概览四个数是现算的（与接口同一份）", len(head or []) >= 4,
              " | ".join(head or [])[:120])
        stats = api(f"/api/projects/{data['project']['id']}/production?episode_id={ep_id}", token)
        check("阶段顺序与键名和后端一致",
              [s["key"] for s in stats["board"]["stages"]]
              == ["script", "parse", "assets", "storyboard", "video",
                  "audio", "review", "compose", "deliver"])
        check("分镜阶段数字对得上（3 镜）",
              [s for s in stats["board"]["stages"] if s["key"] == "storyboard"][0]["total"] == 3)
        tasks = P.eval_js(cdp, "document.querySelectorAll('#view-root li').length")
        check("「下一步」有真实任务而不是空壳", tasks > 0, f"{tasks} 条")
        audio_in_tasks = P.eval_js(cdp, "(() => {const t=[...document.querySelectorAll("
                                        "'#view-root [data-goto]')];return t.length})()")
        check("任务条目都带跳转按钮", audio_in_tasks > 0, f"{audio_in_tasks} 个入口")

        # 点阶段"进入"要真的换页
        P.eval_js(cdp, """(() => {
          const tr=[...document.querySelectorAll('#view-root tbody tr')]
            .find(r => r.textContent.includes('分镜制作'));
          tr.querySelector('[data-goto]').click(); })()""")
        time.sleep(1.4)
        check("点阶段「进入」跳到创作中心·分镜",
              P.has(cdp, "#view-root [data-shot-wb]") or "分镜" in (
                  P.eval_js(cdp, "document.querySelector('.page-head h1')?.textContent") or ""),
              P.eval_js(cdp, "location.hash"))

        # ---------- 镜头工作台 ----------
        mc.go(cdp, f"{BASE}/#shot/0/a{shot_id}")
        sections = P.eval_js(cdp, "[...document.querySelectorAll('#view-root .card-head h2')]"
                                  ".map(e => e.textContent.trim())")
        check("镜头工作台八段齐全", len(sections or []) == 8, "／".join(sections or []))
        check("设定段把 0037 五栏显示出来",
              P.body_has(cdp, "全景") and P.body_has(cdp, "画面比例"))
        check("引用段点名了这条资产", P.body_has(cdp, "阿昭"))
        check("没有产物时不假装：预览区写「还没有签过通过的正式视频」",
              P.body_has(cdp, "还没有签过通过的正式视频"))
        audio_engines = api(f"/api/shots/{shot_id}/workbench", token)["audio"]["engines"]
        tts = audio_engines.get("tts") or {}
        music = audio_engines.get("music") or {}
        if tts.get("provider") and tts.get("runner_wired"):
            expected_audio = "配音与音乐都能提交执行" if music.get("provider") and music.get("runner_wired") else "配音能提交执行"
        elif tts.get("provider"):
            expected_audio = "配音默认轨配了，但执行器没有这一条分支"
        else:
            expected_audio = "音频执行这一腿还没接线"
        check("音频段结论与后端默认轨及执行分支一致",
              P.body_has(cdp, expected_audio), expected_audio)
        check("音频段不给提交入口（只有跳转，没有会失败的按钮）",
              P.eval_js(cdp, """(() => {
                const card = [...document.querySelectorAll('#view-root .card')]
                  .find(c => (c.querySelector('h2')?.textContent || '').includes('⑤'));
                if (!card) return false;
                return ![...card.querySelectorAll('button')].some(
                  b => /提交|生成|开始/.test(b.textContent)); })()"""))
        check("可合成段列出阻塞项",
              P.eval_js(cdp, "document.querySelectorAll('#view-root li').length") > 0)

        # 编辑设定：改景别保存后回读
        P.click(cdp, "#sw-edit")
        check("编辑设定弹窗能打开", P.has(cdp, "#sw-modal:not([hidden])"))
        P.eval_js(cdp, "(() => { const e=document.querySelector('#sw-size');"
                       "e.value='特写'; })()")
        P.click(cdp, "#sw-save")
        time.sleep(1.4)
        check("保存后回到工作台并刷新了景别",
              P.body_has(cdp, "特写"), P.eval_js(cdp, "location.hash"))

        # 提交生成前先预检：默认轨没配 → 阻塞 → 不跳转，只出轻提示
        before_hash = P.eval_js(cdp, "location.hash")
        P.click(cdp, "#sw-generate")
        time.sleep(1.6)
        check("预检阻塞时不把人送去生成中心",
              P.eval_js(cdp, "location.hash") == before_hash, str(before_hash))
        check("预检阻塞时给了可读的轻提示",
              P.eval_js(cdp, "!!document.querySelector('.chip.amber')"))

        # ---------- 成片中心：合成前检查 ----------
        mc.go(cdp, f"{BASE}/#composition")
        P.eval_js(cdp, "(() => { const b=document.querySelector('[data-new-comp]');"
                       "if (b) b.click(); })()")
        time.sleep(1.8)
        check("新建成片弹窗里有合成前检查",
              P.has(cdp, "#cp-pre") and P.eval_js(
                  cdp, "document.querySelector('#cp-pre').textContent.length") > 4,
              (P.eval_js(cdp, "document.querySelector('#cp-pre')?.textContent") or "")[:80])
        check("检查为阻塞时「开始合成」不发起",
              P.eval_js(cdp, """(() => {
                const b=document.querySelector('[data-cp-ok]'); b.click();
                const e=document.querySelector('#cp-modal-err');
                return !e.hidden && e.textContent.includes('阻塞'); })()"""))

        # ---------- 异常恢复目录在真界面上的落地（P1 第 7 项）----------
        # 点队列里的第一行（副本库里带着开发库的历史作业，什么状态都有）。
        # 抽屉每两秒会被列表重绘收回去一次，所以按"点→读→再点"最多试四次：
        # 读到的必须是人在真界面上真能看到的那一屏，不是 DOM 里的一份影子。
        mc.go(cdp, f"{BASE}/#generate")
        drawer = ""
        for _ in range(4):
            P.eval_js(cdp, "(() => { const tr = document.querySelector('tr[data-job]');"
                           " if (tr) tr.click(); })()")
            time.sleep(0.8)
            drawer = P.text_of(cdp, "#gen-drawer-body") or ""
            if "为什么这样" in drawer:
                break
        probe = P.eval_js(cdp, "(() => JSON.stringify({hash: location.hash,"
                               " rows: document.querySelectorAll('tr[data-job]').length,"
                               " hidden: document.querySelector('#gen-drawer')?.hidden}))()")
        check("点开队列里的作业看得到「这一条为什么这样」", "为什么这样" in drawer,
              f"{probe}｜{drawer[:70]}")
        check("异常目录七栏齐（类型／发生了什么／影响范围／建议做法／技术详情／原话）",
              all(k in drawer for k in ("类型", "发生了什么", "影响范围", "建议做法",
                                        "技术详情", "执行器原话")), drawer[:80])
        check("三个判断章都在（可否重试／是否额外费用／是否需人工）",
              ("可以直接重试" in drawer or "不要直接重试" in drawer)
              and ("不产生额外费用" in drawer or "可能再产生费用" in drawer or "可能已计费" in drawer),
              "")
        # 认不出的码必须承认认不出——这条只在点到的作业恰好是未登记码时才要求，
        # 所以按界面实际显示的状态来判，不硬要历史数据配合脚本。
        known_ok = ("还没进目录" not in drawer) or ("未登记" in drawer)
        check("未登记的码不会被编出解释（按这一条作业的实际状态判）", known_ok, "")

        # ---------- 对外五档（任务书 §12.3）在真界面上的落地 ----------
        five = ["待处理", "进行中", "需要处理", "已完成", "失败"]
        fine = ["排队中", "启动中", "运行中", "取消中", "待核对"]
        check("队列列表只给对外五档，不漏细粒度词", P.eval_js(cdp, """(() => {
          const t = [...document.querySelectorAll('tr[data-job] .chip')]
            .map(e => e.textContent.trim());
          return t.length > 0
            && !t.some(x => %s.some(w => x === w))
            && t.some(x => %s.includes(x)); })()""" % (fine, five)))
        # 一行里有好几枚 chip（能力、计费都各一枚），要挑的是"写着五档词的那一枚"
        check("细粒度原词留在悬停里（信息不丢，只是不再抢主位）",
              P.eval_js(cdp, """(() => {
                const chips = [...document.querySelectorAll('tr[data-job] .chip')]
                  .filter(e => %s.includes(e.textContent.trim()));
                return chips.length > 0 && chips.every(e => (e.title || '').length > 0);
              })()""" % (five,)))

        # ---------- 审核质量清单与批量签字（P1 第 4、5 项）----------
        good_id = data["good_job"]["id"]
        check("种下的那条作业真成功了（清单要有可审对象）",
              api(f"/api/jobs/{good_id}", token).get("status") == "succeeded")
        mc.go(cdp, f"{BASE}/#review/1")
        # 说明行的样式得真的落到元素上。`p.modal-meta` 那条规则原来带 `.modal-body`
        # 一层，卡片里用同一个类名拿不到样式，`<p>` 落回正文的 13px＋继承色（白），
        # 于是这行本该是灰的小字在审核中心比正文还亮。这类"类名写了、样式没落"
        # 读代码看不出来，只能量计算样式——所以这里现取一个 ink-3 的参照色来比。
        meta_style = P.eval_js(cdp, """(() => {
          const p = document.querySelector('.card p.modal-meta');
          if (!p) return 'no-meta-line';
          const probe = document.createElement('p');
          probe.style.color = 'var(--ink-3)';
          document.body.appendChild(probe);
          const want = getComputedStyle(probe).color;
          probe.remove();
          const s = getComputedStyle(p);
          return [s.fontSize, s.color === want ? '灰' : '还是白的 ' + s.color,
                  s.marginTop].join('|');
        })()""")
        check("卡片里的说明行拿到的是摘要行样式（不是默认的白正文）",
              str(meta_style).startswith("11.5px|灰|"), str(meta_style))
        P.click(cdp, f'[data-open-job="{good_id}"]')
        time.sleep(1.6)
        detail = P.text_of(cdp, "#job-review-detail") or ""
        check("打开作业后看得见质量清单", "质量清单" in detail, detail[:60])
        check("自动项每次现算（媒体读得出来是 ffprobe 给的）",
              "媒体读得出来" in detail and "产物文件在盘上" in detail, "")
        check("人工项三枚按钮齐（合格／不合格／不适用）",
              all(k in detail for k in ("合格", "不合格", "不适用")), "")
        before_missing = P.eval_js(cdp, "(() => { const m = (document.querySelector("
                        "'#job-review-detail')?.textContent || '').match(/已看 (\\d+) 项/);"
                        " return m ? Number(m[1]) : -1; })()")
        P.eval_js(cdp, """(() => { const seen = new Set();
          [...document.querySelectorAll('#job-review-detail [data-check]')]
            .forEach(b => { if (!seen.has(b.dataset.check)) { seen.add(b.dataset.check); b.click(); } });
        })()""")
        time.sleep(2.2)
        detail2 = P.text_of(cdp, "#job-review-detail") or ""
        after_missing = P.eval_js(cdp, "(() => { const m = (document.querySelector("
                         "'#job-review-detail')?.textContent || '').match(/已看 (\\d+) 项/);"
                         " return m ? Number(m[1]) : -1; })()")
        check("勾完人工项后「已看」计数真涨上去（断言能变红：靠实测两个数比）",
              isinstance(before_missing, int) and isinstance(after_missing, int)
              and after_missing > before_missing,
              f"已看 {before_missing} → {after_missing}")
        check("清单看齐后结论改成可签字", "可签字" in detail2, detail2[:80])

        # 批量：勾上这一行 → 工具条出现 → 批量通过 → 队列结论变已通过
        P.eval_js(cdp, f"""(() => {{
          const box = document.querySelector('input[data-pick="{good_id}"]');
          if (box && !box.checked) {{ box.checked = true;
            box.dispatchEvent(new Event('change', {{ bubbles: true }})); }} }})()""")
        time.sleep(1.6)
        check("勾一条作业后批量工具条出现",
              P.body_has(cdp, "已选 1 条"), "")
        P.eval_js(cdp, """(() => { const b = document.querySelector('[data-batch="approved"]');
          if (b) b.click(); })()""")
        time.sleep(2.6)
        check("批量通过把这条签成已通过（队列结论跟着变）",
              P.body_has(cdp, "已通过"), "")
        refused_hint = P.eval_js(cdp, "(() => (document.querySelector('.bar.bad')?.textContent) || '')")
        check("批量结果的反馈条只在该有拒绝时出现",
              (not refused_hint) or "未签" in refused_hint, str(refused_hint)[:70])

        # ---------- 音频能力状态透明化（P1 第 8 项）----------
        mc.go(cdp, f"{BASE}/#audio/0")
        time.sleep(1.2)
        audio_text = P.text_of(cdp, "#view-root") or ""
        check("音频中心顶上摆的是服务端能力表，不是四行写死的话",
              "这一页现在能干什么" in audio_text
              and ("没有一类能真正执行" in audio_text
                   or "已经能提交执行" in audio_text),
              audio_text[:70].replace("\n", " "))
        check("五类能力逐条在表里（配音／旁白／音乐／音效／口型）",
              all(k in audio_text for k in ("配音（台词）", "旁白", "背景音乐", "音效", "口型同步")), "")
        check("未接线的能力在页面上写明「未接线」（不让人点一次吃一次 422）",
              "未接线" in audio_text, "")
        check("口型那一档明说不存在，而不是藏起来",
              "口型" in audio_text and "清单里没有这个能力的适配器" in audio_text, "")

        # ---------- 问题定位到具体那一镜（任务书 §7.2／§12.1）----------
        blocked_shot = data["shots"][2]["id"]      # 场景里没有片的那一镜（第 3 镜）
        mc.go(cdp, f"{BASE}/#composition")
        P.click(cdp, "[data-new-comp]") if P.has(cdp, "[data-new-comp]") else None
        time.sleep(0.8)
        jump = P.eval_js(cdp, "(() => [...document.querySelectorAll('#cp-modal [data-goto]')]"
                               ".map(b => b.textContent.trim()))()")
        check("合成前检查的阻塞项带着「去这一镜」", isinstance(jump, list) and "去这一镜" in jump,
              str(jump)[:80])
        P.eval_js(cdp, """(() => { const b = [...document.querySelectorAll('#cp-modal [data-goto]')]
          .find(x => x.textContent.includes('去这一镜')); if (b) b.click(); })()""")
        time.sleep(1.6)
        after = P.eval_js(cdp, "location.hash")
        check("点「去这一镜」后弹窗收掉并落到那一镜的工作台",
              str(after).startswith("#shot/0/a") and P.has(cdp, "#view-root .card"),
              f"hash={after}")
        mc.go(cdp, f"{BASE}/#review/1")
        time.sleep(1.0)
        rows = P.eval_js(cdp,
                         "(() => Array.from(document.querySelectorAll("
                         "'[data-goto-shot]')).map((b) => b.dataset.gotoShot))()")
        check("审核中心的产物行有「去这一镜」（不必先点开详情）",
              isinstance(rows, list) and len(rows) >= 1, f"{str(rows)[:80]}")
        if isinstance(rows, list) and rows:
            P.eval_js(cdp, "(() => { const b = document.querySelector('[data-goto-shot]');"
                           " if (b) b.click(); })()")
            time.sleep(1.6)
            check("从审核页跳过去确实打开了那一镜的工作台",
                  P.eval_js(cdp, "location.hash").endswith(f"a{rows[0]}")
                  and "生产完成条件" in (P.text_of(cdp, "#view-root") or ""),
                  f"hash={P.eval_js(cdp, 'location.hash')}")

        # ---------- 产线：发起弹窗与节点失败都读同一份 ----
        mc.go(cdp, f"{BASE}/#pipeline")
        time.sleep(1.4)
        P.eval_js(cdp, "(() => { const b = document.querySelector('[data-new-run]');"
                       " if (b) b.click(); })()")
        time.sleep(0.5)
        pick = lambda ep_id: P.eval_js(
            cdp, f"""(() => {{ const el = document.querySelector('#pl-ep'); if (!el) return 'NO';
              el.value = String({ep_id}); el.dispatchEvent(new Event('change'));
              return el.value; }})()""")
        check("发起弹窗里那一集能选到（弹窗与预检是同一个入口）",
              str(pick(data["ep3"]["id"])) == str(data["ep3"]["id"]))
        time.sleep(2.2)
        groups3 = P.eval_js(cdp, "(() => [...document.querySelectorAll('#pl-pre .pl-pre-group b')]"
                                 ".map(b => b.textContent))()")
        check("产线发起弹窗摆的是统一预检的三档条目", isinstance(groups3, list)
              and any("不能发起" in str(g) for g in groups3), str(groups3)[:90])
        disabled3 = P.eval_js(cdp, "(() => { const b = document.querySelector('[data-pl-ok]');"
                                   " return b ? !!b.disabled : null; })()")
        check("只有阻塞时发起钮禁用（判据来自服务端的 can_start）", disabled3 is True)
        pick(data["ep4"]["id"])
        time.sleep(2.2)
        pl_jump = P.eval_js(cdp, "(() => [...document.querySelectorAll('#pl-pre [data-goto]')]"
                                 ".map(b => b.textContent.trim()))()")
        check("预检条目里那条镜头问题给的是「去这一镜」",
              isinstance(pl_jump, list) and "去这一镜" in pl_jump, str(pl_jump)[:60])
        disabled4 = P.eval_js(cdp, "(() => { const b = document.querySelector('[data-pl-ok]');"
                                   " return b ? !!b.disabled : null; })()")
        check("只有警告时不拦发起（钮跟着 can_start 放开，不是一有条目就禁）",
              disabled4 is False)
        P.eval_js(cdp, """(() => { const b = [...document.querySelectorAll('#pl-pre [data-goto]')]
          .find(x => x.textContent.includes('去这一镜')); if (b) b.click(); })()""")
        time.sleep(1.8)
        jumped = str(P.eval_js(cdp, "location.hash"))
        check("点弹窗里的「去这一镜」先收弹窗再落到那一镜",
              jumped.startswith("#shot/0/a") and jumped.endswith(str(data["bare_shot"]["id"])),
              f"hash={jumped}")
        mc.go(cdp, f"{BASE}/#pipeline")
        time.sleep(1.4)
        P.eval_js(cdp, f"""(() => {{ const b =
          document.querySelector('[data-open-run="{data["run"]["id"]}"]');
          if (b) b.click(); }})()""")
        time.sleep(2.2)
        detail_text = P.text_of(cdp, "#view-root") or ""
        check("运行详情里失败那一格给的是七字段目录",
              "这一格为什么停在这里" in detail_text and "建议做法" in detail_text,
              f"运行 #{data['run']['id']}")
        node_chips = P.eval_js(cdp, "(() => [...document.querySelectorAll('.pl-diag .chip')]"
                                   ".map(c => c.textContent.trim()))()")
        check("节点判断章说清可否重试与要不要人工（不新造判据）",
              isinstance(node_chips, list) and any("重试" in str(c) for c in node_chips)
              and any("人工" in str(c) for c in node_chips), str(node_chips)[:90])

        # ---------- P2 生产统计（项目工作台页内，不新增菜单） ----------
        mc.go(cdp, f"{BASE}/#production")
        time.sleep(1.4)
        pid = data["project"]["id"]
        ep_id = data["episode"]["id"]
        stats = api(f"/api/projects/{pid}/production?episode_id={ep_id}",
                    token)["stats"]
        dom_total = P.eval_js(cdp, """(() => {
          const head = [...document.querySelectorAll('#view-root .card h2')]
            .find((h) => h.textContent.includes('生产统计'));
          if (!head) return -1;
          const host = head.closest('.card');
          // 只数能力×状态那一张表：同一张卡里还有协作小表，它的第二列是「提交」条数，
          // 一起加就变成 4——上一轮这个断言把两张表的数混着比，比的是测试自己的错
          const table = host.querySelector('table');
          if (!table) return -1;
          return [...table.querySelectorAll('tbody tr')]
            .reduce((a, tr) => a + Number((tr.children[1] || {}).textContent || 0), 0);
        })()""")
        check("项目工作台里那一张统计表的条数＝接口的 jobs_total（实测两个数比）",
              int(dom_total or -1) == int(stats["jobs_total"]),
              f"界面 {dom_total}｜接口 {stats['jobs_total']}")
        texts = P.text_of(cdp, "#view-root") or ""
        check("统计把『算不出来』也写在屏上，而不是缺省不显示",
              "这一屏不算的" in texts and "质量标准" in texts, texts[texts.find("这一屏不算的"):][:70]
              if "这一屏不算的" in texts else "没找到那一行")
        check("钱与进度那一行说清口径（本地作业不计价）",
              "计费" in texts and ("不计价" in texts or "CNY" in texts or "USD" in texts),
              ",".join([w for w in ("计费", "未定价", "不给推算") if w in texts]))
        disk_text = P.text_of(cdp, "#view-root") or ""
        check("磁盘那一行与预检同句：有样本说预计、没样本退回提醒线",
              ("作业目录剩" in disk_text or "读不到作业目录" in disk_text)
              and ("预计" in disk_text or "提醒线" in disk_text),
              disk_text[disk_text.find("磁盘还够不够"):][:110]
              if "磁盘还够不够" in disk_text else "没找到那一行")

        # 协作读数那一格：只数提交与签字，两条限制必须与数字同时出现
        team_texts = P.eval_js(cdp, """(() => {
          const host = [...document.querySelectorAll('#view-root .card h2')]
            .find((h) => h.textContent.includes('生产统计'));
          if (!host) return [];
          const card = host.closest('.card');
          return [String(card.querySelectorAll('tbody tr').length),
                  [...card.querySelectorAll('th')].map((t) => t.textContent.trim()).join('/'),
                  (card.querySelector('p.hint') || {}).textContent || ''];
        })()""")
        check("协作读数就在统计那张卡里（不另开页面），且表头写清数的是提交与签字",
              isinstance(team_texts, list) and "提交" in str(team_texts[1])
              and "签字" in str(team_texts[1]), str(team_texts[1])[:70])
        check("协作读数把两条限制显示在同一张表下面",
              "不等于产量" in str(team_texts[2]) and "操作审计" in str(team_texts[2]),
              str(team_texts[2])[:80])

        # ---------- P2 批量重投卡住的镜头 ----------
        retry_seen = P.eval_js(cdp, "(() => { const b = document.querySelector('#pb-retry-failed');"
                                    " return b ? b.textContent.trim() : ''; })()")
        check("失败镜头存在时才有「重投卡住的镜头」那颗钮（只读账号与零失败时不摆）",
              "重投卡住的镜头" in str(retry_seen), retry_seen)
        queued_path = f"/api/jobs?project_id={pid}&limit=200"
        before_ids = sorted(j["id"] for j in api(queued_path, token))
        P.eval_js(cdp, "(() => { const b = document.querySelector('#pb-retry-failed');"
                       " if (b) b.click(); })()")
        time.sleep(0.6)
        dialog_ok = P.has(cdp, "[data-d-ok]")
        check("确认走的是应用内弹窗（原生 confirm 被守卫用例禁掉）", dialog_ok)
        P.click(cdp, "[data-d-ok]")
        time.sleep(1.6)
        bar = P.text_of(cdp, "#pb-retry-out") or ""
        check("重投结果条把逐条原因原话摆出来（重投几条、跳过几条、为什么跳过）",
              "重投 1 条" in bar and "跳过 1 条" in bar and "已经成功" in bar, bar[:120])
        after = api(queued_path, token)
        fresh = [j for j in after if j["id"] not in before_ids]
        check("批量重投真的多排了一条作业，且它认得出自己是重投",
              len(fresh) == 1 and fresh[0].get("retry_of_job_id"),
              f"新作业 {[(j['id'], j['status']) for j in fresh]}｜"
              f"retry_of={fresh[0].get('retry_of_job_id') if fresh else '—'}")
        check("原行的账不动：被顶掉那一行的状态与错误码还在",
              any(j["id"] in before_ids and j["status"] in ("failed", "succeeded")
                  for j in after),
              f"原有 {len(before_ids)} 条仍可查")

        mem_line = P.text_of(cdp, "#view-root") or ""
        # mock 作业走的是假适配器，没有按秒采样——这一栏必须写「没读数」，
        # 而不是把 worker 生命周期那个最大值挂在这一条作业头上
        check("没有采样读数的能力显示「没读数」，不拿生命周期峰值冒充",
              "没读数" in mem_line and "还没有按秒采到的内存读数" in mem_line,
              "找到" if "没读数" in mem_line else mem_line[-120:])

        est = stats["estimate"]
        # 推算那一句要么按样本给数、要么明说不给；两种都必须出现其中一种原话，
        # 不能靠页面上别处有个「分钟」就判过。
        phrase = "有样本原话" if est["minutes"] is not None else "没样本原话"
        need = ("按本项目已完成视频作业的均值" if est["minutes"] is not None
                else "不给推算")
        check(f"推算值那一句跟着样本走（{phrase}）", need in texts,
              f"接口 minutes={est['minutes']}｜剩余 {est['remaining_shots']} 镜｜要找「{need}」")
        nav_counts = P.eval_js(cdp, """(() => {
          const items = [...document.querySelectorAll('.nav-item')];
          const hit = items.filter((a) => a.textContent.includes('统计')).length;
          return String(items.length) + '/' + String(hit);
        })()""")
        total_nav, hits = (str(nav_counts).split('/') + ['0', '0'])[:2]
        # 两个数都要看：选择器一个都没抓到时，「没有统计入口」这句结论是空的
        check("P2 这一项没有新增菜单（统计住在原有那一页里）",
              int(total_nav or 0) >= 13 and int(hits or 0) == 0,
              f"侧栏入口 {total_nav} 个、其中含『统计』{hits} 个")

        # ---------- 成片尺寸口径：留空按素材众数，不写死放大 ----------
        # `output_plan` 是时间线**真能拼起来**时才有的读数，所以先把这一集推到
        # 「三镜都有签过通过的正式视频」：提交→worker 真跑→勾清单→签字，全走真接口。
        # 未签完时界面本来就不该出现这句话（出现了就是界面自己在猜尺寸）。
        for sh in data["shots"]:
            j = api("/api/jobs", token, "POST", {
                "project_id": data["project"]["id"],
                "episode_id": data["episode"]["id"],
                "shot_id": sh["id"], "capability": "video",
                "execution_mode": "local", "provider_code": "mock",
                "model_code": "mock-1", "prompt": "尺寸口径验收",
                "params": {"sleep": 0.2, "media": "mp4", "color": "blue",
                           "seconds": 1.0}})
            st = ""
            for _ in range(60):
                st = api(f"/api/jobs/{j['id']}", token).get("status")
                if st in ("succeeded", "failed", "needs_attention"):
                    break
                time.sleep(0.5)
            check(f"尺寸验收作业真的成功（镜 {sh.get('seq', '?')}）", st == "succeeded", st)
            cl = api(f"/api/jobs/{j['id']}/review-checklist", token)
            items = [{"code": m["code"], "state": "pass", "note": "验收逐条看过"}
                     for m in (cl.get("human") or []) if m.get("required")]
            api(f"/api/jobs/{j['id']}/review-checklist", token, "POST", {"items": items})
            anc = (api(f"/api/jobs/{j['id']}/review", token).get("anchors") or [])
            api(f"/api/jobs/{j['id']}/review-sign", token, "POST",
                {"anchor": anc[0] if anc else "artifact",
                 "decision": "approved", "note": "尺寸口径验收"})
        plan = api(f"/api/projects/{data['project']['id']}/composition-precheck"
                   f"?episode_id={data['episode']['id']}",
                   token).get("output_plan") or {}
        mc.go(cdp, f"{BASE}/#composition")
        P.click(cdp, "[data-new-comp]") if P.has(cdp, "[data-new-comp]") else None
        time.sleep(1.4)
        modal = P.text_of(cdp, "#cp-modal") or ""
        check("签齐后合成前检查不再阻塞（界面与服务端同结论）",
              "阻塞 0" in modal,
              modal[modal.find("合成前检查"):][:60].replace("\n", " "))
        check("分辨率留空时的落点写在弹窗里，尺寸由服务端众数给出",
              "留空就与素材同尺寸" in modal
              # 界面上是乘号「×」不是字母 x：两种写法都认，免得把断言修成假通过
              and (f"{plan.get('width')}×{plan.get('height')}" in modal
                   or f"{plan.get('width')}x{plan.get('height')}" in modal.lower()),
              f"服务端 output_plan={plan}")
        # 反向也判：服务端没给 plan，界面就不许凭空出现这句话（防"界面自己算尺寸"）
        check("这句话只在服务端真给出 plan 时出现",
              bool(plan) == ("留空就与素材同尺寸" in modal), f"plan={bool(plan)}")

        # ---------- 窄屏与异常 ----------
        for hash_, name in (("#production", "项目工作台"), ("#shot/0/a%s" % shot_id, "镜头工作台")):
            cdp.cmd("Emulation.setDeviceMetricsOverride", width=380, height=760,
                    deviceScaleFactor=1, mobile=True)
            mc.go(cdp, f"{BASE}/{hash_}")
            overflow = P.eval_js(cdp, "document.documentElement.scrollWidth - window.innerWidth")
            check(f"{name} 380px 无横向溢出", int(overflow or 0) <= 0, f"溢出 {overflow}px")
            cdp.cmd("Emulation.clearDeviceMetricsOverride")

        errs = P.eval_js(cdp, "window.__errs || []")
        check("整轮没有 JS 异常", not errs, str(errs)[:200])
        P.eval_js(cdp, "window.__errs = []")

        chrome.terminate()
    finally:
        try:
            worker.terminate()
            worker.wait(timeout=25)
        except Exception:
            pass
        proc.terminate()
        time.sleep(0.4)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 通过")
    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
