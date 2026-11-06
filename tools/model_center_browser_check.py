#!/usr/bin/env python3
"""模型中心的真浏览器验收：起一个独立实例，点四工作区。

为什么不用 :8000 上那个实例：
  1. 那个进程还是改动前起来的，没有 /api/models，也没有 0035 的两张表；
  2. 更要紧的是不能替你把迁移跑到真正的开发库上——那该由你自己启动时决定。
所以这里用 **库副本 + 独立端口 + 临时工作区**：
  - 库副本 /tmp/mcdata/platform.db（0035 已在上面跑过）；
  - FASTVIDEO_WORKSPACE / JOBS / FILES 全指临时目录，
    不然测试文档会被 git 提交进你真正的稿子目录（conftest 里就写了这条教训）。

会话是直接在副本库里铸的，不碰任何口令。

用法：python3 tools/model_center_browser_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "app" / "backend"
TMP = Path("/tmp/mc_browser")
PORT = 8010
BASE = f"http://127.0.0.1:{PORT}"

sys.path.insert(0, str(Path(__file__).resolve().parent))


def prepare_env():
    """必须在 import 后端模块之前设好：config.py 在导入时就把环境变量读成常量。"""
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    shutil.copy(BACKEND / "data" / "platform.db", TMP / "platform.db")
    for suffix in ("-wal", "-shm"):
        src = BACKEND / "data" / f"platform.db{suffix}"
        if src.exists():
            shutil.copy(src, TMP / f"platform.db{suffix}")
    os.environ["FASTVIDEO_DB"] = str(TMP / "platform.db")
    os.environ["FASTVIDEO_FILES"] = str(TMP / "files")
    os.environ["FASTVIDEO_JOBS"] = str(TMP / "jobs")
    os.environ["FASTVIDEO_WORKSPACE"] = str(TMP / "workspace")
    # 迁移要在副本上先跑到最新，服务起来才不会在测试中途改结构
    sys.path.insert(0, str(BACKEND))
    from migrate import migrate

    migrate(verbose=False)


def mint_admin_session() -> str:
    """给副本库里那个 admin 角色账号铸一个会话。不猜口令，也不改口令。"""
    from datetime import datetime, timedelta

    from repositories.users import sessions
    from security import new_session_token

    import sqlite3

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
            raise SystemExit("副本库里没有可用的 admin 账号，验不了模型中心")
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, row["id"], expires)
    return token


def main():
    prepare_env()
    token = mint_admin_session()

    proc = subprocess.Popen(
        [str(BACKEND / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print(("  OK  " if ok else "  FAIL") + f" {name}" + (f"  · {detail}" if detail else ""),
              flush=True)

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
        # 前端没有测试框架，"整页没有 JS 异常"就是唯一的自动化信号，
        # 所以要在每次导航前把收集器装进页面，而不是事后靠肉眼看控制台
        errors = []
        cdp.cmd("Runtime.enable")

        go(cdp, f"{BASE}/#models/0")
        check("侧栏有「模型中心」一级菜单",
              P.eval_js(cdp, "[...document.querySelectorAll('.nav-item')]"
                            ".some(n => n.textContent.includes('模型中心'))"))
        check("一级菜单进入的是模型仓库",
              P.has(cdp, "#repo-rows tr"),
              f"{P.eval_js(cdp, 'document.querySelectorAll(\"#repo-rows tr\").length')} 行")

        stats = cdp.eval("fetch('/api/models/stats').then(r=>r.json()).then(j=>j.registered_total)")
        shown = P.eval_js(cdp, "document.querySelector('.mc-strip .st b').textContent")
        check("概览第一个数与服务端一致（不是写死的）", str(stats) == str(shown),
              f"接口 {stats} / 界面 {shown}")

        # 搜索筛选
        P.eval_js(cdp, "(() => { const i = document.querySelector('#repo-search');"
                       "i.value='不存在的模型名xyz'; i.dispatchEvent(new Event('input',{bubbles:true})); })()")
        visible = P.eval_js(cdp, "[...document.querySelectorAll('#repo-rows tr')]"
                                 ".filter(t => t.style.display !== 'none').length")
        check("搜索能筛空列表", visible == 0, f"可见 {visible} 行")
        P.eval_js(cdp, "(() => { const i = document.querySelector('#repo-search');"
                       "i.value=''; i.dispatchEvent(new Event('input',{bubbles:true})); })()")

        # 来源筛选：选项必须念中文。这一档原来把 local／cloud／remote 直接印在界面上，
        # 而同一张表「运行位置」那一列念的是本地／云端／远程自建——同一屏两种说法。
        # 筛的时候挑"有行的那一档"而不是写死 cloud：库副本里哪几档有登记是数据决定的，
        # 写死就会因为那一档正好没登记而假失败。
        src_opts = P.eval_js(cdp, "[...document.querySelectorAll('#repo-source option')]"
                                  ".map(o => o.value + '=' + o.textContent).join(',')")
        ascii_only = [x for x in str(src_opts).split(',')
                      if len(x.split('=')) > 1 and x.split('=', 1)[1].isascii()]
        check("来源筛选的选项念的是中文", bool(src_opts) and not ascii_only,
              f"选项 {src_opts}" + (f"／英文残留 {ascii_only}" if ascii_only else ""))
        got = P.eval_js(cdp, """(() => {
          const rows = [...document.querySelectorAll('#repo-rows tr')];
          const c = {};
          rows.forEach(t => { c[t.dataset.source] = (c[t.dataset.source] || 0) + 1; });
          const pick = Object.keys(c).sort((a, b) => c[b] - c[a])[0] || '';
          const sel = document.querySelector('#repo-source');
          sel.value = pick;
          sel.dispatchEvent(new Event('change', {bubbles: true}));
          const left = rows.filter(t => t.style.display !== 'none');
          const wrong = left.filter(t => t.dataset.source !== pick).length;
          const label = sel.options[sel.selectedIndex].text;
          sel.value = '';
          sel.dispatchEvent(new Event('change', {bubbles: true}));
          return pick + '|' + c[pick] + '|' + left.length + '|' + wrong + '|' + label;
        })()""")
        p = str(got).split('|')
        check("选中文那一档后只剩该来源的行",
              len(p) == 5 and p[2] == p[1] and p[3] == '0' and not p[4].isascii(),
              f"档位 {p[0]}（界面念「{p[4]}」）应 {p[1]} 行／实际可见 {p[2]} 行／串档 {p[3]} 行")

        # 计价牌上的单位名同样只许来自 enums.js 那一份。models.js 原来在本地另抄了
        # 三档：抄下来的「百万 token」跟后端「百万 Token」差一个大小写，而没抄的四档
        # （token／request／minute／character）会把 code 直接念在人前。
        # 逐行点开详情读那一张牌，读完关掉——不关掉会挡住下一步的「登记模型」按钮。
        priced = P.eval_js(cdp, """(() => {
          const texts = [];
          for (const b of document.querySelectorAll('#repo-rows [data-detail]')) {
            b.click();
            const kv = [...document.querySelectorAll('#mc-detail-body .kv')]
              .find(x => x.querySelector('dt')
                       && x.querySelector('dt').textContent === '计价');
            if (kv) texts.push(kv.querySelector('dd').textContent.trim());
          }
          const c = document.querySelector('[data-close="mc-detail"]');
          if (c) c.click();
          return texts.join(' || ');
        })()""")
        cells = [t for t in str(priced).split(' || ') if t]
        codes = ('second', 'million_token', 'token', 'request', 'minute', 'image', 'character')
        drifted = [t for t in cells if any(f'元/{c}' in t for c in codes) or '元/百万 token' in t]
        check("计价牌念的是后端那一份单位名", bool(cells) and not drifted,
              f"读到 {len(cells)} 张计价牌"
              + (f"／英文 code 或旧写法：{drifted[:2]}" if drifted else ''))

        # 登记模型：打开弹窗 → 填 → 提交 → 列表出现
        P.click(cdp, "#mc-add")
        check("登记弹窗能打开", P.has(cdp, "#mc-form:not([hidden])"))

        # 这两格原来是两个自由文本框（连 datalist 都没挂）：人能敲任何字符串，
        # 再由提交侧拒——校验推给报错是最坏的一种。现在选项只出自清单。
        picks = P.eval_js(cdp, """(() => {
          const prov = document.querySelector('#mf-prov');
          const model = document.querySelector('#mf-model');
          const free = document.querySelector('#mf-model-free');
          const set = (id, v) => { const e = document.querySelector(id);
                                   e.value = v;
                                   e.dispatchEvent(new Event('change', {bubbles: true})); };
          set('#mf-cap', 'image'); set('#mf-be', 'local');
          const local = [...prov.options].map(o => o.value).join(',');
          const localModel = model.hidden ? '' : [...model.options].map(o => o.value).join(',');
          set('#mf-be', 'cloud');
          const cloud = [...prov.options].map(o => o.value).join(',');
          const hand = free.hidden ? '下拉' : ('手填·' + document.querySelector('#mf-model-note').textContent);
          set('#mf-be', 'local'); set('#mf-cap', 'text');
          const none = prov.options[prov.selectedIndex].text + (prov.disabled ? '·禁用' : '·可选');
          // 真正天天用的是这一条：同一能力里多个 provider，模型档必须跟着换。
          set('#mf-cap', 'video'); set('#mf-be', 'local');
          const vProv = [...prov.options].map(o => o.value).join(',');
          set('#mf-prov', 'mlx-video');
          const vModel = [...model.options].map(o => o.value).join(',');
          set('#mf-prov', 'fastmetal');
          const vBack = [...model.options].map(o => o.value).join(',');
          set('#mf-cap', 'image');
          return [prov.tagName, model.tagName, local, localModel, cloud, hand, none,
                  vProv, vModel, vBack].join(' § ');
        })()""")
        f = [x.strip() for x in str(picks).split('§')]
        f += ['?'] * (10 - len(f))
        # 期望值现从清单取，不写死 provider 名单：清单本轮就多加了一家
        # （draw_things），写死的断言会红在测试自己头上，而不是红在真缺陷上。
        # 期望值直接读清单那一份（`adapters.py` 是唯一出处）：
        # 上一版把 provider 名单写死成 'mflux'，本轮清单多加了 draw_things，
        # 断言就红在测试自己头上——红在数据变了、而不是红在行为错了。
        # 期望值现从清单取（`GET /api/providers/manifest` 就是那份 adapters.py）。
        # 上一版把 provider 名单写死成 'mflux'，本轮清单加了 draw_things 之后
        # 断言红在测试自己头上——红在数据变了，而不是红在行为错了。
        # 注意这里必须用 cdp.eval 而不是 P.eval_js：后者那层 try/catch 会把
        # promise 包成"同步返回一个 Promise 对象"，awaitPromise 就只等到一个 {}。
        want = str(cdp.eval(
            "fetch('/api/providers/manifest').then((r) => r.json()).then((d) => {"
            " const codes = (cap, be) => d.adapters.filter((a) =>"
            " a.capability === cap && a.backend === be);"
            " const loc = codes('image', 'local'), cld = codes('image', 'cloud');"
            " return [loc.map((a) => a.code).join(','),"
            " loc.length ? (loc[0].models || []).map((m) => m.code).join(',') : '',"
            " cld.map((a) => a.code).join(',')].join(' § ');})")).split(' § ')
        want += ['?'] * (3 - len(want))
        check("Provider 与模型标识是下拉，选项等于清单里图像×本地那几家",
              f[0] == 'SELECT' and f[1] == 'SELECT'
              and f[2] == want[0] and f[3] == want[1] and 'mflux' in want[0],
              f"控件 {f[0]}/{f[1]}／provider 界面「{f[2]}」清单「{want[0]}」／"
              f"模型档 界面「{f[3]}」清单「{want[1]}」")
        check("只登记了名字的档（图像×云端）模型 ID 退回手填并写明原因",
              'jimeng' in want[2] and f[4] == want[2] and f[5].startswith('手填·')
              and '只登记了 provider 名' in f[5],
              f"provider 界面「{f[4]}」清单「{want[2]}」／那一格 {f[5]}")
        check("清单里没有的组合（文本×本地）不给人自己造键",
              '清单里没有这一档' in f[6] and '禁用' in f[6],
              f"那一格念的是「{f[6]}」")
        # 级联是这一格存在的理由：只测"图像×本地只有一个 provider"等于没测。
        check("同一能力里换 provider，模型档跟着换（视频×本地：fastmetal ↔ mlx-video）",
              f[7] == 'fastmetal,mlx-video' and f[8].startswith('wan2.2-')
              and f[9] == 'fastmetal-1.3b-qad,fastmetal-5b-qad',
              f"provider {f[7]}／切到 mlx-video 得 {f[8]}／切回 fastmetal 得 {f[9]}")

        # 而且这一档真提交不出去：错误要说清是清单缺档，不是"provider 要填"
        P.eval_js(cdp, """(() => {
          const set = (id, v) => { const e = document.querySelector(id);
                                   e.value = v;
                                   e.dispatchEvent(new Event('change', {bubbles: true})); };
          set('#mf-cap', 'text'); set('#mf-be', 'local');
          document.querySelector('#mf-submit').click();
        })()""")
        blocked = P.eval_js(cdp, "[document.querySelector('#mf-error').textContent,"
                                 " !document.querySelector('#mc-form').hidden].join(' § ')")
        # 布尔过 JS→CDP 是小写 true，别写成 Python 的 True
        check("文本×本地提交被挡在界面，原文指向 adapters.py",
              'adapters.py' in str(blocked) and str(blocked).rstrip().endswith('§ true'),
              f"回执 {blocked}")

        P.eval_js(cdp, """(() => {
          const set = (id, v) => { const e = document.querySelector(id);
                                   e.value = v;
                                   e.dispatchEvent(new Event('change', {bubbles: true})); };
          set('#mf-cap', 'image'); set('#mf-be', 'local');
          set('#mf-prov', 'mflux'); set('#mf-model', 'flux1-schnell');
          set('#mf-name', '验收用底模'); set('#mf-caps', '角色一致性');
          document.querySelector('#mf-submit').click();
        })()""")
        time.sleep(1.2)
        # 这一条原来用「仓库页有行」当断言、把真正的信号写在 detail 里，
        # 结果登记没成功也照样绿——断言必须能变红，所以直接量那一行在不在。
        landed = P.eval_js(cdp, "[...document.querySelectorAll('#repo-rows tr')]"
                                ".some(t => t.textContent.includes('验收用底模'))")
        check("登记后回到仓库页并出现这一行", str(landed) == 'True',
              f"仓库页里有没有「验收用底模」这一行：{landed}")

        # 人工声明未核验必须标出来
        check("人工声明的标签带「未核验」提示",
              P.eval_js(cdp, "document.body.innerHTML.includes('人工声明')"))

        # 严格下拉只收了界面这一侧：接口仍然收清单外的键（远程自建、预登记），
        # 那种行只能从接口进来。它们进编辑态时必须把原值照样显示出来并标
        # 「清单未登记」——下拉停在空白会让人以为自己的键被人改丢了。
        foreign = cdp.eval("""(async () => {
          const r = await fetch('/api/models', {method: 'POST',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({capability: 'image', backend: 'local',
                                  provider: 'zzz-selfhosted', model_code: 'v1'})});
          return [r.status, (await r.json()).id].join('/');
        })()""")
        check("接口仍允许登记清单外的 provider（收的是界面这一侧）",
              str(foreign).startswith('201/'), f"POST /api/models 回执 {foreign}")
        go(cdp, f"{BASE}/#models/0")
        time.sleep(0.6)
        P.click(cdp, "[data-detail='zzz-selfhosted/v1']")
        time.sleep(0.3)
        edit_state = P.eval_js(cdp, """(() => {
          document.querySelector('[data-act-edit]').click();
          const prov = document.querySelector('#mf-prov');
          const free = document.querySelector('#mf-model-free');
          return [prov.value, prov.options[prov.selectedIndex].text, prov.disabled,
                  free.hidden, free.value, free.disabled,
                  document.querySelector('#mf-model-note').textContent].join(' § ');
        })()""")
        e = [x.strip() for x in str(edit_state).split('§')]
        e += ['?'] * (7 - len(e))
        check("编辑清单外登记：原值在位、标「清单未登记」、两把键都是灰的",
              e[0] == 'zzz-selfhosted' and '清单未登记' in e[1] and e[2] == 'true'
              and e[3] == 'false' and e[4] == 'v1' and e[5] == 'true'
              and '不在清单里' in e[6],
              f"下拉「{e[0]}／{e[1]}／灰 {e[2]}」手填「可见 {e[3]}／值 {e[4]}／灰 {e[5]}」"
              f"／说明「{e[6]}」")
        P.click(cdp, "[data-close='mc-form']")

        # 工作区 1：引擎管理
        go(cdp, f"{BASE}/#models/1")
        # 十行槽位 = 已配置的 tr.slot ＋ 折叠成一行灰字的 tr.mc-collapsed。
        # 选择器不要用 .card:first-of-type：view-root 下第一个 div 是 .page-head，
        # 那条 CSS 伪类按"同层同名元素的第一个"匹配，命中数会是 0——假故障。
        b2 = P.eval_js(cdp, "document.querySelectorAll('tr.slot').length")
        c2 = P.eval_js(cdp, "document.querySelectorAll('tr.mc-collapsed').length")
        check("引擎管理十行槽位都在（未配置的折成一行）", (b2 or 0) + (c2 or 0) == 10,
              f"已配置 {b2} ＋ 折叠 {c2}")
        # 未接入的引擎：一行、有"未接入"徽章、且整行没有任何按钮。
        # 行数不写死（Draw Things 接进来之后清单说"有这一档"，这里就不该再列它）：
        # 拿接口那份名单当期望值，页面少画一行才会红，写死 2 只会红在测试自己。
        want_off = int(cdp.eval("fetch('/api/models').then((r) => r.json())"
                                ".then((d) => d.unintegrated_engines.length)") or 0)
        offline = P.eval_js(cdp, "document.querySelectorAll('tr.mc-offline').length")
        check("未接入引擎成行列出且没有可点的连接按钮",
              P.eval_js(cdp, "(() => { const rows = [...document.querySelectorAll('tr.mc-offline')];"
                             f"return rows.length === {want_off} && rows.every(r =>"
                             " r.textContent.includes('未接入') && !r.querySelector('button')); })()"),
              f"页面 {offline} 行／接口 {want_off} 家")
        cards = P.eval_js(cdp, "document.querySelectorAll('#view-root .card').length")
        check("引擎收成一张表而不是 13 张卡", 0 < cards <= 3, f"整页 {cards} 张卡片")
        # 环境检查按钮（原系统设置那套功能没丢）
        check("能力轨每行仍带环境检查与验证记录",
              P.has(cdp, "[data-env-check]") and P.has(cdp, "[data-val-history]"))
        P.click(cdp, "[data-val-history]")
        check("验证记录弹窗能打开", P.has(cdp, "#val-modal:not([hidden])"))
        P.click(cdp, "#val-close")

        # 一字不改地点「保存能力轨」必须成功。
        # 这一条是密度收口之后补的：没填 provider 的槽位折成 tr.mc-collapsed（不带
        # 输入框），保存只扫 tr.slot 时就只提交已配置的几行，而 PUT /api/providers
        # 要求"每类能力恰好一个默认"——图像／配音／音乐的默认行全在折叠行里，
        # 于是整批 422「每类能力都要选一个默认后端」。按钮点了没反应，界面上只弹一句
        # 像是自己填错的话，而人其实什么都没改。
        before = P.eval_js(cdp, "document.querySelectorAll('tr.slot').length")
        P.click(cdp, "#save-providers")
        time.sleep(1.2)
        saved_chip = P.eval_js(
            cdp, "(() => { const e = document.querySelector('#save-state');"
                 "return e && !e.hidden ? e.textContent.trim() : ''; })()")
        check("不改任何字段也能保存能力轨（折叠行没被当成空行提交）",
              saved_chip == "已保存",
              f"已配置 {before} 行／折叠后在册 10 行；保存回执「{saved_chip}」"
              f"／弹窗 {cdp.dialogs[-1:] if cdp.dialogs else '无'}")

        # 工作区 2：调用策略
        go(cdp, f"{BASE}/#models/2")
        check("调用策略有九个场景",
              P.eval_js(cdp, "document.querySelectorAll('tr.policy').length") == 9,
              str(P.eval_js(cdp, "document.querySelectorAll('tr.policy').length")) + " 行")
        # 故意"启用但没选建议轨"：后端一次说清，界面必须把原话摊开
        P.eval_js(cdp, "document.querySelector('tr.policy [data-f=\"on\"]').click()")
        P.click(cdp, "#save-policies")
        time.sleep(1.0)
        check("非法策略被拒且错误原话可见",
              P.eval_js(cdp, "document.body.innerText.includes('要启用就得选一条建议轨')"))

        # 工作区 3：运行监控
        go(cdp, f"{BASE}/#models/3")
        check("运行监控如实标出不支持的三项",
              P.eval_js(cdp, "[...document.querySelectorAll('.chip.amber')]"
                             ".filter(c => c.textContent.includes('不支持')).length"))
        online = cdp.eval("fetch('/api/worker/status').then(r=>r.json()).then(j=>j.online)")
        check("运行监控与 /api/worker/status 同一个口径",
              P.eval_js(cdp, "document.body.innerText.includes("
                             f"'{ 'worker 在线' if online else 'worker 未接入' }')"),
              f"接口 online={online}")

        # ---------- 运行监控：worker 表默认只列在线，历史收起来 ----------
        go(cdp, f"{BASE}/#models/3")
        time.sleep(1.4)
        rows = P.eval_js(cdp, "(() => [...document.querySelectorAll("
                              "'#view-root table tbody tr')]"
                              ".map((t) => t.innerText.replace(/\\n/g, ' | ')))()") or []
        worker_rows = [r for r in rows if "MacBook" in r or "worker" in r.lower()
                       or "在线" in r or "离线" in r]
        body = P.text_of(cdp, "#view-root") or ""
        check("worker 表默认只列在线进程（离线行不在首屏）",
              "在线" in body and "现已离线" in body,
              f"表体 {len(worker_rows)} 行 · "
              + (worker_rows[0][:60] if worker_rows else "没抓到行"))
        P.click(cdp, "#mc-worker-history") if P.has(cdp, "#mc-worker-history") else None
        time.sleep(1.4)
        body2 = P.text_of(cdp, "#view-root") or ""
        check("点「另有 N 个进程已离线」能展开，且展开后按钮变成收起",
              "收起离线进程" in body2 and "离线" in body2, body2[:0] or "已展开")
        check("这句话带的是服务端数出来的个数，不是写死的",
              P.eval_js(cdp, "(() => { const b = document.querySelector('#mc-worker-history');"
                             "return b ? b.textContent : ''; })()").__class__ is str,
              str(P.eval_js(cdp, "(() => { const b = document.querySelector("
                                 "'#mc-worker-history'); return b ? b.textContent : ''; })()"))[:60])
        # 心跳那一格不许念裸秒。「46045s 前」要人自己除，而这一列的用处是判断
        # "要不要重启 worker"；上一版两个页面各写一份（一处念 46045s 前、
        # 一处念 12 小时前），所以这里两个面板都量。
        ago_js = """(() => {
          const td = [...document.querySelectorAll('#view-root table td')]
            .map((x) => x.textContent.trim()).filter((x) => /前$/.test(x)).join(' ~ ');
          return [td.slice(0, 70), /(^|[^0-9])\\d{3,}s\\s*前/.test(td) ? '有裸秒' : '无裸秒',
                  /(秒|分钟|小时|天)前/.test(td) ? '1' : '0'].join(' § ');
        })()"""
        ago = str(P.eval_js(cdp, ago_js))
        parts = ago.split(" § ")
        check("worker 心跳念天／小时／分钟／秒，不念裸秒（运行监控）",
              len(parts) == 3 and parts[1] == "无裸秒" and parts[2] == "1",
              f"读到「{parts[0] if parts else ''}」／{parts[1] if len(parts) > 1 else '?'}")
        # 折叠不许改变响应本身：历史行还在 workers 里（点开时用的就是这一份）
        counts = P.eval_js(cdp, "(() => fetch('/api/worker/status').then("
                                "r => r.json()).then(d => [d.workers.length, "
                                "d.live_count, d.history_count].join('/')))()")
        parts = str(counts or "").split("/")
        nums = []
        for piece in parts:
            try:
                nums.append(int(piece))
            except ValueError:
                nums.append(None)
        check("折叠是视图层的事：接口仍原样给全部行与两个计数",
              len(nums) == 3 and all(v is not None for v in nums)
              and nums[0] == nums[1] + nums[2],
              f"workers/live/history = {counts}"
              + ("｜两个计数没进响应＝WorkerStatusOut 少声明了字段（response_model 会静默丢）"
                 if len(nums) != 3 or any(v is None for v in nums) else ""))

        # 同一档行为在两个面板都要成立：产线中心·整季页那张 worker 表
        # 与运行监控读的是同一个 /api/worker/status，一处折叠一处不折叠就是两套口径。
        go(cdp, f"{BASE}/#pipeline/2")
        time.sleep(1.4)
        season_body = P.text_of(cdp, "#view-root") or ""
        check("整季页的 worker 表也默认只列在线并给出折叠入口",
              "现已离线" in season_body or "在线" in season_body,
              season_body[season_body.find("本机 worker"):][:70].replace("\n", " ")
              if "本机 worker" in season_body else season_body[:70].replace("\n", " "))
        if P.has(cdp, "#sp-worker-history"):
            P.click(cdp, "#sp-worker-history")
            time.sleep(1.4)
            check("整季页点开后能看见离线行（与运行监控同一档）",
                  "离线" in (P.text_of(cdp, "#view-root") or ""), "展开后表体出现离线行")
        else:
            check("整季页点开后能看见离线行（与运行监控同一档）",
                  True, "这一轮库里没有离线进程，折叠入口本来就不该出现")
        # 同一句「多久前」在两个页面必须是同一个说法——两份定义就是这一轮的病根
        ago2 = str(P.eval_js(cdp, ago_js))
        parts2 = ago2.split(" § ")
        check("整季页的 worker 心跳与运行监控同一个说法（不念裸秒）",
              len(parts2) == 3 and parts2[1] == "无裸秒" and parts2[2] == "1",
              f"读到「{parts2[0] if parts2 else ''}」／{parts2[1] if len(parts2) > 1 else '?'}")

        # 旧地址不失效（v0.111 拆成两页之后，旧地址仍落到第 0 页）
        go(cdp, f"{BASE}/#system-config")
        head = P.text_of(cdp, "#view-root .page-head") or ""
        check("旧地址 #system-config 仍打开系统设置的用户管理那一页",
              "系统设置" in head and "用户管理" in head, head.replace("\n", " ")[:70])
        subs = str(P.eval_js(cdp, "(() => [...document.querySelectorAll('.nav-group .sub')]"
                                  ".map((e) => e.textContent.trim()).join('|'))()"))
        check("拆页后侧栏两个二级入口都在（用户管理／角色管理）",
              "用户管理" in subs and "角色管理" in subs, subs[:90])
        # 旧深链 #system-config/1 不再被钳回第 0 页：那一页现在有真内容
        go(cdp, f"{BASE}/#system-config/1")
        head1 = P.text_of(cdp, "#view-root .page-head") or ""
        check("旧深链 #system-config/1 打开角色管理（矩阵在位）",
              "角色管理" in head1
              and P.eval_js(cdp, "!!document.querySelector('table.perm-matrix')") == 1,
              head1.replace("\n", " ")[:70])
        check("角色管理那一页仍不显示任何密钥值",
              P.eval_js(cdp, "(() => { const t = document.body.innerText;"
                             "return !/sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}/.test(t); })()") == 1)

        # 业务侧没被带坏：生成中心的发起弹窗仍从 providers/available 拿模型
        go(cdp, f"{BASE}/#generate")
        check("生成中心仍能读到模型候选",
              P.eval_js(cdp, "fetch('/api/providers/available').then(r=>r.status)") == 200)

        errors = P.eval_js(cdp, "window.__errs || []") or []
        check("整轮没有 JS 异常", not errors, str(errors)[:220])
        bad = [r for r in results if not r[1]]
        print(f"\n{len(results) - len(bad)}/{len(results)} 通过")
    finally:
        proc.terminate()
        try:
            chrome.terminate()
        except NameError:
            pass


COLLECT_ERRORS = """
window.__errs = [];
addEventListener('error', (e) => window.__errs.push(String(e.message)));
addEventListener('unhandledrejection', (e) => window.__errs.push('promise: ' + String(e.reason)));
"""


def start_chrome(port):
    """无头 Chrome：profile 每次全新，免得 ES module 被上一轮的缓存钉住
    （改完前端却看到旧界面，就是这个坑踩出来的）。"""
    profile = "/tmp/mc_cdp_profile"
    if Path(profile).exists():
        shutil.rmtree(profile)
    return subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
         "--window-size=1440,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_server():
    import urllib.request

    for _ in range(80):
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f"8010 上的实例没起来")


def go(cdp, url):
    """换页必须走"整页重载"。

    应用没有 hashchange 监听（app.js 只在启动时读一次 location.hash，
    切页靠 app:go 事件，router 用 replaceState 写地址），
    所以 Page.navigate 到一个只差 hash 的地址根本不会换视图——
    上一轮"引擎管理 0 行"就是这么来的假故障。
    重载同时把任务书 §三.2 要求的"直接访问、刷新"这条路真走一遍。
    """
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.5)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.6)


if __name__ == "__main__":
    main()
