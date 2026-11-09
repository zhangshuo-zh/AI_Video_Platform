#!/usr/bin/env python3
"""创作中心文档页 ＋ 资产中心：真浏览器验收。

为什么单独补这一份：这两页在 2026-11-09 的一次覆盖面盘点里被发现
**从来没被任何浏览器门禁打开过**——`writing.js` 1897 行（全项目最大视图）、
`assets.js` 423 行，全部门禁的 `go()` 只到过 production／projects／shot／
generate／pipeline／composition／audio／review／agents／models／system-config。
后端有 `test_documents.py`／`test_assets.py` 守着逻辑，所以这是**渲染盲区
而不是逻辑盲区**：接口对不对有人管，"接口回来的东西有没有被画对"没人管。

优先挑的是**口算得出答案**的那几条——这类断言改坏了立刻红，
不依赖"看起来对"：

  * 字数口径：服务端 `documents._tend()` 保证正文以 `\\n` 收尾（空稿除外），
    前端 `writing.charCount()` 反过来把尾部换行去掉。两边对齐才不会出现
    "刚打完 5 个字，刷新后变 6 个字"。这条必须在真浏览器里量：它是两个
    文件之间的约定，pytest 各自都绿也证明不了它们对得上。
  * 版本冲突：拿一个过期的 `expected_version` 保存，必须出现冲突卡与两个
    处置按钮，**且草稿还在**（最坏的实现是覆盖掉或清空）。用哈希比对
    而不是 git commit 号，正是为了检出"有人在编辑器外面改过"。
  * 资产计数与本地搜索：`N / M 条` 的两个数必须跟着搜索词变，而搜索
    只筛已经拿到手的那批（不回库、不重渲染整页，焦点不能从输入框被摘走）。

只读纪律：不点「提交」、不建 Job、不发云端请求。写操作只发生在
`/tmp` 下的库副本与工作区副本里（`_harness.temp_dir()` 统一收尾）。

反向验证（每条都真做过变异，不是口头声明）：
  * `charCount` 去掉 `.replace(/\\n+$/, '')` → 字数那条红（尾部换行被算成字）；
  * 资产搜索不重画网格（`paintGrid` 早返回）→ 计数那条红；
  * 冲突卡不渲染两个按钮 → 冲突那条红。

用法：python3 tools/writing_assets_browser_check.py
（缺后端依赖时会自动用 `app/backend/.venv` 的解释器重执行自己）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H


def _ensure_backend_deps() -> None:
    """本脚本要在进程内直插仓储造种子，一路 import 到 pydantic。

    服务与 worker 用 `H.backend_python()` 起，那只管子进程；本机依赖只在
    `app/backend/.venv` 里，而 CI 装进系统解释器。缺依赖时用 venv 那只
    重执行自己一次，`python3 tools/...` 在哪都能直接跑。
    """
    try:
        import pydantic  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    target = Path(H.backend_python())
    # 比字符串而不是 resolve()：venv 的 bin/python 是软链，resolve() 后
    # 两个解释器会变成同一个真实文件，"已经在 venv 里"就永远判不出来。
    if Path(sys.executable) == target:
        raise SystemExit("这个解释器连 pydantic 都没有，后端 venv 里那份也一样："
                         "先按 requirements.txt 装依赖再跑")
    entry = sys.argv[0] if str(sys.argv[0]).endswith(".py") else __file__
    os.execv(str(target), [str(target), str(Path(entry).resolve()), *sys.argv[1:]])


_ensure_backend_deps()

BACKEND = H.BACKEND
TMP = H.temp_dir("fv-wa-")
PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

# 报告整页未捕获异常：前端没有测试框架，这是唯一的自动化信号。
COLLECT_ERRORS = """
window.__fvErrors = [];
window.addEventListener('error', e => window.__fvErrors.push(String(e.message)));
window.addEventListener('unhandledrejection',
  e => window.__fvErrors.push('unhandled: ' + String(e.reason)));
"""

# 种子里那份文档的正文：故意在库/盘上以 \n 收尾（服务端 _tend 的规范），
# 而字数是 11 个字——尾部换行不该被算成第 12 个字。
DOC_BODY = "第一集开场，雨落在旧码头"
DOC_BODY_TENDED = DOC_BODY + "\n"


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
    """造一份"用过一阵"的库：一个项目、一份有正文的文档、几个资产。

    直插仓储而不走 HTTP：服务还没起；而且这些是"库里的历史"，
    不是要验的写路径（写路径由脚本自己在页面上走）。
    返回后面断言要用的 id 与期望值。
    """
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    from repositories.content import assets as asset_repo
    from repositories.documents import documents as doc_repo
    from repositories.projects import projects as project_repo
    from services import documents as doc_service

    admin = _admin_id()
    project = project_repo.create("雨巷", "一条旧码头与它的雨", [], admin)
    project_id = int(project["id"])
    # 资产页看的是 `store.current`，而项目列表按 last_opened_at 倒序排——
    # 副本库里带着现役项目（真机上会是"大肥鱼の日常"那类），不把种子项目
    # 顶到最前，页面打开时选中的就是别人，资产计数永远是 0 / 0。
    project_repo.mark_opened(project_id)

    # 正文走服务层的 create_document：路径定法、落盘、_tend 规范化都在里面，
    # 自己拼 rel_path 就是在种子数据里复刻一份容易走样的规则。
    doc = doc_service.create_document(project_id, None, "novel", "第一集",
                                      DOC_BODY, actor="seed")
    doc_row = doc_repo.list_by_project(project_id)[0]

    # 资产：名称／别名／标签／简介都故意让"搜得到"与"搜不到"各有落点，
    # 这样"计数跟着搜索词变"那条才有真对象可筛。
    made = [
        asset_repo.create(project_id, "character", "阿雨", "撑伞的人",
                          "短发，米色风衣", aliases=["小雨"], tags=["主角"]),
        asset_repo.create(project_id, "scene", "旧码头", "雨里的码头",
                          "石阶，锈铁桩", aliases=["码头"], tags=["场景"]),
        asset_repo.create(project_id, "prop", "油纸伞", "一把旧伞",
                          "竹骨，褪色", aliases=[], tags=["道具"]),
    ]
    return {
        "project_id": project_id,
        "doc_id": int(doc_row["id"]),
        "doc_rel_path": doc_row["rel_path"],
        "doc_chars": len(DOC_BODY),
        "asset_total": len(made),
        # 搜"雨"能命中两个（阿雨、旧码头 的简介里有"雨"），搜"伞"只中一个。
        "search_hit": "雨",
        "search_narrow": "油纸伞",
        "project_title": "雨巷",
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
    """等某个选择器出现。列表是异步取的，直接断言会测到"还没渲染"。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval(f"!!document.querySelector({json.dumps(sel)})"):
            return True
        time.sleep(0.2)
    return False


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

        # ── 创作中心 · 文档页 ───────────────────────────────────────────
        # 默认 tab 是 2（分镜）；文档页地址里的 id 必须是**裸数字**且真的在
        # 列表里（`writing.js:170` 先 `Number(focus)` 再 `docs.some(...)`，
        # 解析不出数字就当没给，退回列表）。
        go(cdp, f"{BASE}/#writing/0/{seed['doc_id']}")
        wait_css(cdp, "#doc-text")

        body = P.eval_js(cdp, "document.querySelector('#doc-text')?.value")
        check("文档页把正文读进了编辑框",
              isinstance(body, str) and body.strip() == DOC_BODY.strip(),
              repr(body)[:70])

        # 服务的规范是"以 \n 收尾"（`documents._tend`），而字数不能把那个 \n
        # 算进去——否则同一段字"刚打完"与"刷新之后"会差一个字。
        # 这一条是跨文件的约定，pytest 两边各自绿也证明不了它们对得上。
        count_txt = P.text_of(cdp, "#doc-count")
        raw_len = len(body)
        check("字数不把结尾换行算进去",
              f"{seed['doc_chars']} 字" in count_txt
              and raw_len == seed["doc_chars"] + 1,   # 编辑框里确实带着那个 \n
              f"{count_txt}（编辑框 {raw_len} 字含收尾换行，正文 {seed['doc_chars']} 字）")

        dirty_txt = P.text_of(cdp, "#doc-dirty")
        check("刚打开的文档标「与盘上一致」",
              "与盘上一致" in dirty_txt, dirty_txt)

        # 打字 → 变「未保存」；这是"编辑态"唯一的外部信号。
        # 注意：编辑框里装的是盘上的原文，服务端 `_tend` 已经补了一个收尾 \n，
        # 直接在末尾追加字会插到这个 \n 之后（`…码头\n又起风了`），所以期望字数
        # 要按"原文 + 换行 + 新字"算，不能想当然写成 12+4。断言按真内容推出来。
        ADDED = "又起风了"
        P.eval_js(cdp, f"""(() => {{
          const ta = document.querySelector('#doc-text');
          ta.value = ta.value + {json.dumps(ADDED)};
          ta.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }})()""")
        time.sleep(0.4)
        check("改过之后标「未保存」",
              "未保存" in P.text_of(cdp, "#doc-dirty"))
        typed = P.eval_js(cdp, "document.querySelector('#doc-text')?.value") or ""
        check("改过之后字数跟着变",
              f"{len(typed)} 字" in P.text_of(cdp, "#doc-count"),
              f"{P.text_of(cdp, '#doc-count')}（编辑框里 {len(typed)} 字）")

        # ── 版本冲突 ───────────────────────────────────────────────────
        # 在页面之外把盘上正文改掉（模拟"有人在编辑器外面改过"），
        # 页面手里的令牌就过期了；此时保存必须报冲突，**且草稿不能丢**。
        # 这是"第二个编辑者不会静默覆盖"（W05 验收项）唯一的外部证据。
        doc_rel = seed["doc_rel_path"]
        (TMP / "workspace" / doc_rel).write_text(DOC_BODY + "\n外侧改了这句\n",
                                                 encoding="utf-8")

        P.eval_js(cdp, """(() => {
          const b = document.querySelector('#doc-save');
          if (b) b.click();
          return true;
        })()""")
        time.sleep(1.6)

        check("盘上被外部改过时保存报冲突",
              P.has(cdp, "#doc-conflict-use") and P.has(cdp, "#doc-conflict-refresh"),
              P.text_of(cdp, ".bar, .card-head", "-")[:60])
        draft_kept = P.eval_js(cdp, "document.querySelector('#doc-text')?.value") or ""
        check("冲突时我的草稿还在（没有被清空或覆盖）",
              "又起风了" in draft_kept, repr(draft_kept)[:70])

        # ── 资产中心 ───────────────────────────────────────────────────
        go(cdp, f"{BASE}/#assets")
        wait_css(cdp, "#asset-count")
        time.sleep(0.8)

        count_txt = P.text_of(cdp, "#asset-count")
        total = seed["asset_total"]
        check("资产计数显示「N / M 条」",
              f"/ {total} 条" in count_txt, count_txt)

        # 搜索只筛已拿到手的那批：计数要跟着变，且不回库。
        # 先 focus() 再打字：不先把焦点放进去就看 activeElement，量到的是
        # "没人点过这个框"，跟"重渲染把焦点摘走了"是两回事——差一步前置动作
        # 就会把一个恒假断言当成真缺陷。
        P.eval_js(cdp, """(() => {
          const box = document.querySelector('#asset-search');
          box.focus();
          box.value = '油纸伞';
          box.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()""")
        time.sleep(0.5)
        narrowed = P.text_of(cdp, "#asset-count")
        check("搜索后计数跟着收窄",
              narrowed != count_txt and f"/ {total} 条" in narrowed, narrowed)

        focus_kept = P.eval_js(
            cdp, "document.activeElement?.id === 'asset-search'")
        check("搜索不把焦点从输入框摘走", focus_kept)

        # 清空搜索要还原（清除键不能被"记住"成永久过滤）
        P.eval_js(cdp, """(() => {
          const box = document.querySelector('#asset-search');
          box.value = '';
          box.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()""")
        time.sleep(0.4)
        check("清空搜索后计数还原",
              P.text_of(cdp, "#asset-count").startswith(f"{total} / "),
              P.text_of(cdp, "#asset-count"))

        # 搜一个必然不中的词：空态要说话，不能是一片白
        P.eval_js(cdp, """(() => {
          const box = document.querySelector('#asset-search');
          box.value = '绝不可能出现的名字zzz';
          box.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()""")
        time.sleep(0.4)
        grid_txt = P.text_of(cdp, "#asset-grid-box")
        check("搜不到时给出空态说明而不是空白",
              bool(grid_txt and len(grid_txt.strip()) > 4), grid_txt[:70])

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


if __name__ == "__main__":
    sys.exit(main())
