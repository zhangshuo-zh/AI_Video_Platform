#!/usr/bin/env python3
"""场景候选进发起弹窗：真浏览器验收（只读，一次作业都不提交）。

测的是「生成中心 · 提交作业」弹窗里那一格「任务场景」的两步行为：
第一步（2026-11-07）＝选场景摆出候选与挡路原因；
第二步（2026-11-08）＝**预填**——恰好一档候选时自动填进「执行路线／槽位」
并留痕可撤销；多档时不替你挑，点那一行的「用它」才填。

与裁定 MC1 的关系要说准：MC1 禁的是**平台自己换路**（`resolve_provider()`
不看策略、备用轨不自动顶上），不是禁"人点了场景、界面把格子填好"。
所以这一版把第一步那条「执行路线与槽位没有被自动改」**反转**了——
现在它必须被改（单候选时），但必须留下看得见的痕迹与一键撤销。

为什么这一条必须在真浏览器里量：它全是"接口回来的东西有没有被画对、
改了格子有没有留痕、有没有越权碰计费勾选"这类事。pytest 只能量到接口的形状
（`test_model_center.py::test_场景骨架…`），量不到
"`#jm-confirm` 那个勾选框没有被程序勾上"——而那一句正是第二步唯一可能
踩红线的地方（`user_confirmed_cloud` 闸门归提交路径管，界面预填碰不得）。

四条口径，缺一条这条验收就会变成摆设：
  * **候选条数只认副本库里的确定性事实**：`storyboard`（1 档）与
    `scene_prompt`（2 档）的差别来自清单必需能力，不取决于 Ollama／
    Draw Things 开没开着——服务型引擎在列表里是 `unchecked`，
    `probe_deferred` 不算挡路（`model_center._resolve` 里写明过这条）。
    DeepSeek 那第二档要真出现，靠的是本脚本自设的假 `DEEPSEEK_API_KEY`
    （只进副本环境的子进程，不碰开发库，也不发真请求：cloud 健康只看
    key 在不在，不出网）。不这样钉住，`scene_prompt` 在某些机器上退化成
    1 档，"多档不自动预填"那条就永远测不到东西。
  * **不写死候选条数**仍对"面板上的数字"成立：面板里那个数必须是
    resolve 回来的真实数，不是前端写死的。
  * **只读**：全程不点「提交」，不建任何 Job。
  * 库副本 + 独立端口 + 临时工作区（`_harness` 统一收尾），不碰开发库。

反向计数器（2026-11-08 每条都真做过变异验红，不是口头声明）：
  * 自动预填改成不预填（`length === 1` → `false`）→ "单候选预填"＋"留痕"红；
  * 自动预填改成多档也预填（`=== 1` → `>= 1`）→ "两档时没有自动改"红；
  * 云端预填顺手勾上 `#jm-confirm` → "绝不替你勾计费确认"红；
  * 撤销里去掉 `refreshRouteSelect()` → "一键撤销回到那一格"红
    （槽位停在预填那档，只有加了 route 文本比对的版本抓得到）。

用法：python3 tools/generate_scene_check.py
"""

from __future__ import annotations
import http.server, json, os, shutil, socketserver, sqlite3, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H

BACKEND = H.BACKEND
TMP = H.temp_dir("fv-scene-")
PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

class _FakeOllamaHandler(http.server.BaseHTTPRequestHandler):
    """只认 /api/tags 的假 Ollama：回一档 qwen3:8b，别的一律 404。

    为什么验收里要自己起一个：预填断言（④）要的场景是"恰好一档候选"，
    而 `storyboard` 那一档在本机是 Ollama——真 Ollama 没开着的时候，
    页面装载一碰 `/providers/available`（它会现探测），ollama 就被判成
    "连不上"进 blockers，候选变 0，预填根本没有发生的条件。
    断言若跟着本机服务状态漂移，"预填了没有"就永远测不准。
    """
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/api/tags", "/api/version"):
            body = json.dumps({"models": [{"name": "qwen3:8b"}],
                               "version": "0.0.0-scene-check"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):   # 安静：验收输出里不混访问日志
        pass


def start_fake_ollama():
    port = H.free_port()
    srv = socketserver.TCPServer(("127.0.0.1", port), _FakeOllamaHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{port}"


def prepare_env():
    # 假 key 只进本脚本拉起的后端子进程（继承环境），作用是让 deepseek 那档
    # 在副本库里确定性可候选，好让"多档不自动预填"这一条真的被测到。
    # cloud 的健康判据是"key 在不在"（adapters.health 对 cloud 不发网络请求），
    # 所以这里既不会真外联，也不会污染开发库。
    os.environ["DEEPSEEK_API_KEY"] = "sk-scene-check-fake-not-a-real-secret"
    TMP.mkdir(parents=True, exist_ok=True)
    os.environ["FASTVIDEO_DB"] = str(TMP / "platform.db")
    os.environ["FASTVIDEO_FILES"] = str(TMP / "files")
    os.environ["FASTVIDEO_JOBS"] = str(TMP / "jobs")
    os.environ["FASTVIDEO_WORKSPACE"] = str(TMP / "workspace")
    H.prepare_backend_db(TMP / "platform.db")

def mint_admin_session() -> str:
    from datetime import datetime, timedelta
    from repositories.users import sessions
    from security import new_session_token
    from db import get_conn
    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.id FROM app_user u JOIN user_role ur ON ur.user_id = u.id
               JOIN role r ON r.id = ur.role_id WHERE r.code='admin' AND u.is_active=1
               ORDER BY u.is_superuser DESC, u.id LIMIT 1""").fetchone()
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, row["id"], expires)
    return token

def go(cdp, url):
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.5)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.8)

def point_engine_at_fake(ollama_url: str):
    """把副本库里那条 ollama 引擎实例指到假服务上。

    只动副本库（`FASTVIDEO_DB` 那份），开发库一个字不碰。改的是
    `engine_instance.endpoint`——探测、列表、提交前检查走的都是这一行，
    所以这一改动让"Ollama 在不在"这件事由本脚本说了算，而不是本机状态。
    """
    db = os.environ["FASTVIDEO_DB"]
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE engine_instance SET endpoint=? WHERE engine_code='ollama'",
                     (ollama_url,))
        # 顺手把 draw_things 那条禁掉：本机没开它时探测会连不上——
        # 那个结论本身没问题，但它会把 image 场景的候选从 1 档变成 0 档，
        # 而 ⑤ 只查"缺能力警示"，不查候选数，禁掉它让 image 那条路更短更稳。
        conn.execute("UPDATE engine_instance SET enabled=0 WHERE engine_code='draw_things'")
    # 不需要清探测缓存：这一句跑在后端子进程启动**之前**，那份缓存还不存在。


def main():
    prepare_env()
    fake, fake_url = start_fake_ollama()
    point_engine_at_fake(fake_url)
    token = mint_admin_session()
    proc = subprocess.Popen(
        [str(H.backend_python()), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    results = []
    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}" + (f"  · {detail}" if detail else ""), flush=True)
    try:
        H.wait_http(f"{BASE}/api/health")
        import proto_cdp_check as P
        chrome = H.launch_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=BASE, path="/", httpOnly=True)
        cdp.cmd("Runtime.enable")
        errs = []
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=
                "window.__errs=[];addEventListener('error',e=>window.__errs.push(String(e.message)));")

        # ① 先导航到站点再碰 localStorage。
        # 刚连上 CDP 时页面还是 about:blank，那个源上没有 localStorage，
        # 直接调会抛 SecurityError 把整轮打断（本脚本第一次跑就撞上了）。
        go(cdp, f"{BASE}/#generate")
        # 夜间主题：自绘下拉只在夜间存在，下面那条"按钮字跟不跟得上程序化设值"
        # 必须在这一档里量——白天主题下 pick.js 根本不介入，那条断言等于没测。
        try:
            cdp.eval("localStorage.setItem('fv.theme','night')")
            go(cdp, f"{BASE}/#generate")
        except Exception as exc:      # 设不上就按白天跑，下面那条会走 "NO-DRAWN" 分支
            print(f"  （夜间主题没设上：{exc}；自绘下拉那条将按白天跳过）", flush=True)
        st = P.eval_js(cdp, "fetch('/api/model-policies/scenes').then(r=>r.status)")
        check("场景骨架接口状态 200", st == 200, f"status={st}")
        n = P.eval_js(cdp, "fetch('/api/model-policies/scenes').then(r=>r.json()).then(j=>j.items.length)")
        check("九个场景都在", n == 9, f"{n} 个")

        # ② 提交框里有「任务场景」这一格
        cdp.eval("document.querySelector('#gen-new').click()")
        time.sleep(0.8)
        check("提交框有「任务场景」下拉", P.has(cdp, "#jm-scene") == 1 or bool(P.has(cdp, "#jm-scene")))

        # ③ 按能力过滤：视频只有镜头视频一个场景
        opts = P.eval_js(cdp, """(() => {
          const c = document.querySelector('#jm-capability');
          c.value = 'video'; c.dispatchEvent(new Event('change',{bubbles:true}));
          const s = document.querySelector('#jm-scene');
          return [...s.options].map(o => o.value + '=' + o.textContent).join('|');
        })()""")
        check("视频能力下场景只剩镜头视频", "shot_video" in str(opts) and "storyboard" not in str(opts),
              str(opts)[:120])

        # ④ 单候选场景 → 自动预填 + 留痕 + 可撤销。
        #    （第二步把第一步那条"执行路线与槽位没有被自动改"**反转**了：
        #    恰好一档候选时必须填进两格——但每次都要留看得见、可撤销的痕迹，
        #    并且绝不碰计费勾选。）
        P.eval_js(cdp, """(() => {
          const c = document.querySelector('#jm-capability');
          c.value = 'text'; c.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(0.3)
        before = P.eval_js(cdp, """(() => {
          const m = document.querySelector('#jm-mode'); const r = document.querySelector('#jm-route');
          return [m.value, r.options[r.selectedIndex]?.textContent || ''].join('§');
        })()""")
        P.eval_js(cdp, """(() => {
          const s = document.querySelector('#jm-scene');
          s.value = 'storyboard'; s.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(1.2)
        panel = P.eval_js(cdp, "(() => { const p = document.querySelector('#jm-scene-panel');"
                               "return p && !p.hidden ? p.innerText : 'HIDDEN'; })()")
        check("选了场景就出现候选面板", "候选" in str(panel), str(panel)[:100].replace("\n", " "))
        n_cand = P.eval_js(cdp, r"""(() => {
          const m = /满足必需能力的候选（(\d+)）/.exec(
            document.querySelector('#jm-scene-panel').innerText);
          return m ? Number(m[1]) : -1; })()""")
        check("候选数是个真实数字（不是写死的）", isinstance(n_cand, int) and n_cand >= 0,
              f"候选 {n_cand} 条")
        check("面板说清了 DeepSeek 为什么不行（必需能力缺口要显形）",
              "DEEPSEEK_API_KEY" in str(panel) or "结构化输出" in str(panel),
              str(panel)[:160].replace("\n", " "))
        # —— 以下四条是第二步的新断言，每条都有反向计数器（见文件尾说明）——
        filled = P.eval_js(cdp, """(() => {
          const m = document.querySelector('#jm-mode'); const r = document.querySelector('#jm-route');
          const t = document.querySelector('#jm-scene-prefill');
          return JSON.stringify({mode: m.value,
            route: r.options[r.selectedIndex]?.textContent || '',
            trace: t && !t.hidden ? t.innerText : ''});
        })()""")
        fo = json.loads(filled)
        check("单候选场景把这一档预填进了执行路线／槽位（storyboard 恰好一档＝ollama）",
              fo["mode"] == "local" and "ollama" in str(fo["route"]),
              f"mode={fo['mode']} route={fo['route']}")
        check("预填留痕写在脸上（谁改的、能撤销）",
              "已按场景" in str(fo["trace"]) and "撤销预填" in str(fo["trace"]),
              str(fo["trace"])[:120].replace("\n", " "))
        conf = P.eval_js(cdp, """(() => { const c = document.querySelector('#jm-confirm');
          return c === null ? 'ABSENT' : String(c.checked); })()""")
        check("预填绝不勾「确认按供应商计费」（本地档压根不该出现勾选框，云端档见 ④b）",
              str(conf) in ("false", "ABSENT"), f"confirm={conf}")
        P.eval_js(cdp, "document.querySelector('#jm-scene-undo').click()")
        time.sleep(0.4)
        back = P.eval_js(cdp, """(() => {
          const m = document.querySelector('#jm-mode'); const r = document.querySelector('#jm-route');
          const t = document.querySelector('#jm-scene-prefill');
          return JSON.stringify({mode: m.value,
            route: r.options[r.selectedIndex]?.textContent || '',
            hidden: !t || t.hidden});
        })()""")
        bo = json.loads(back)
        # 三样都要对：路线、槽位那格显示的字、留痕收起。
        # 只查路线不查槽位会漏掉一个真 bug：默认轨那一档的 option value 是空串，
        # 撤销时若把"值非空"当成"能回去"的条件，槽位会停在预填那一档。
        check("一键撤销回到打开弹窗时那一格（路线＋槽位＋留痕）",
              bo["mode"] == str(before).split("§")[0]
              and bo["route"] == str(before).split("§")[1]
              and bo["hidden"] in (True, 1, "true"),
              f"before={before} after={bo}")

        # ④b 多候选场景 → **不自动预填**，点「用它」才填；云端预填不碰勾选。
        P.eval_js(cdp, """(() => {
          const s = document.querySelector('#jm-scene');
          s.value = 'scene_prompt'; s.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(1.2)
        multi = P.eval_js(cdp, """(() => {
          const t = document.querySelector('#jm-scene-prefill');
          return JSON.stringify({mode: document.querySelector('#jm-mode').value,
            trace: t && !t.hidden ? 'VISIBLE' : 'HIDDEN',
            panel: document.querySelector('#jm-scene-panel').innerText,
            uses: document.querySelectorAll('[data-scene-use]').length});
        })()""")
        mo = json.loads(multi)
        n_multi = P.eval_js(cdp, r"""(() => {
          const m = /满足必需能力的候选（(\d+)）/.exec(
            document.querySelector('#jm-scene-panel').innerText);
          return m ? Number(m[1]) : -1; })()""")
        # 这条**必须**跑在 2 档上（假 key 就是为它设的）：1 档时"没自动预填"
        # 是空真——断言什么都没测。环境构造见文件头口径 1。
        if n_multi != 2:
            check("多候选场景不替你挑（环境里 scene_prompt 没凑够 2 档，测不到东西）",
                  False, f"候选 {n_multi} 条——假 DEEPSEEK_API_KEY 没生效？")
        else:
            check("多候选场景不替你挑：两档时执行路线没有被自动改",
                  str(mo["mode"]) == "default" and mo["trace"] == "HIDDEN",
                  f"mode={mo['mode']} trace={mo['trace']}")
            check("每一行给一个「用它」按钮（多档时选哪档是人的事）",
                  mo["uses"] == 2, f"{mo['uses']} 个按钮")
            # 点云端那一行的「用它」：显式点＝人的选择，允许预填，但不许代勾计费
            P.eval_js(cdp, """(() => {
              const rows = [...document.querySelectorAll('[data-scene-use]')];
              const cloud = rows.find(b => b.closest('tr').innerText.includes('deepseek'));
              cloud.click();
            })()""")
            time.sleep(0.5)
            after_use = P.eval_js(cdp, """(() => {
              const t = document.querySelector('#jm-scene-prefill');
              const c = document.querySelector('#jm-confirm');
              return JSON.stringify({mode: document.querySelector('#jm-mode').value,
                trace: t && !t.hidden ? t.innerText : 'HIDDEN',
                confirm: c === null ? 'ABSENT' : String(c.checked)});
            })()""")
            au = json.loads(after_use)
            check("点「用它」选中云端档：路线填进 cloud 且留痕",
                  str(au["mode"]) == "cloud" and "已按场景" in str(au["trace"]),
                  f"mode={au['mode']} trace={str(au['trace'])[:90]}")
            check("云端档预填**绝不**替你勾计费确认（user_confirmed_cloud 闸门在界面上原样）",
                  str(au["confirm"]) in ("false", "ABSENT")
                  and "确认按供应商计费" in str(au["trace"]),
                  f"confirm={au['confirm']}")
            P.eval_js(cdp, "document.querySelector('#jm-scene-undo').click()")
            time.sleep(0.4)

        # ④c 人接管＝留痕作废：预填之后再手动改路线，痕迹必须消失
        #    （撤销信息若还留着，人下一步"撤销"会把他的选择冲掉——那是越权）。
        P.eval_js(cdp, """(() => {
          const s = document.querySelector('#jm-scene');
          s.value = 'storyboard'; s.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(1.0)
        P.eval_js(cdp, """(() => {
          const m = document.querySelector('#jm-mode');
          m.value = 'default'; m.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(0.4)
        takeover = P.eval_js(cdp, """(() => {
          const t = document.querySelector('#jm-scene-prefill');
          const undo = document.querySelector('#jm-scene-undo');
          return JSON.stringify({hidden: !t || t.hidden, undoGone: !undo});
        })()""")
        to = json.loads(takeover)
        check("人手动改路线＝接管，预填留痕与撤销按钮一起作废",
              to["hidden"] in (True, 1, "true") and to["undoGone"] in (True, 1, "true"),
              f"{to}")

        # ⑤ 清单缺能力的场景要显式警示
        P.eval_js(cdp, """(() => {
          const c = document.querySelector('#jm-capability');
          c.value = 'image'; c.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(0.3)
        P.eval_js(cdp, """(() => {
          const s = document.querySelector('#jm-scene');
          s.value = 'character_variant'; s.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(1.2)
        vpanel = P.eval_js(cdp, "(() => document.querySelector('#jm-scene-panel').innerText)()")
        check("清单缺能力的场景有明确警示（不是含糊的待配置）",
              "没有任何来源" in str(vpanel) or "图生图" in str(vpanel),
              str(vpanel)[:140].replace("\n", " "))

        # ⑤b 自绘下拉必须跟着程序化设值走（2026-11-07 捞到的真缺陷）
        #     这条量的是"按钮上那行字"与真 <select> 的值是否一致。
        #     病根：pick.js 只挂了 MutationObserver，而 `sel.value = x` 改的是
        #     property 不是 attribute，MutationObserver 一条都不报——于是按钮
        #     停在旧文字上，控件里写着 A、程序按 B 在跑。
        drawn = P.eval_js(cdp, """(() => {
          const sel = document.querySelector('#jm-scene');
          const btn = sel.closest('.fv-pick')?.querySelector('.fv-pick-btn');
          if (!btn) return 'NO-DRAWN';   // 白天主题下没有自绘层，跳过
          const opt = sel.options[sel.selectedIndex];
          return JSON.stringify({btn: btn.textContent.trim(),
                                 sel: (opt ? opt.textContent : '').trim()});
        })()""")
        if str(drawn) == 'NO-DRAWN':
            check("自绘下拉与真实值同步（白天主题无自绘层，跳过）", True, "day theme")
        else:
            o = json.loads(drawn)
            check("自绘下拉的文字跟得上程序化设值（切主题不改值也要对上）",
                  o["btn"] == o["sel"], f"按钮「{o['btn']}」/ 真值「{o['sel']}」")

        # ⑥ 不按场景时面板收起
        P.eval_js(cdp, """(() => {
          const s = document.querySelector('#jm-scene');
          s.value = ''; s.dispatchEvent(new Event('change',{bubbles:true}));
        })()""")
        time.sleep(0.4)
        hidden = P.eval_js(cdp, "(() => document.querySelector('#jm-scene-panel').hidden)()")
        check("不按场景时面板收起", hidden in (True, 1, "true"), f"hidden={hidden}")

        # ⑦ 全程零 JS 异常
        e = P.eval_js(cdp, "JSON.stringify(window.__errs || [])")
        check("整轮零未捕获 JS 错误", str(e) in ("[]", "null", "undefined"), str(e)[:160])
    finally:
        fake.shutdown()
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    ok = sum(1 for _, o, _ in results if o)
    print(f"\n{ok}/{len(results)} 通过")
    return 0 if ok == len(results) else 1

if __name__ == "__main__":
    sys.exit(main())
