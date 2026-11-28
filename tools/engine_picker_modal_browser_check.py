#!/usr/bin/env python3
"""两级引擎模型选择器与生成中心弹窗排版：真浏览器验收。

覆盖 2026-11-28 新增与修复的前端渲染盲区：
  1. `engine-model-picker.js`：`attachEngineFilter()` 在角色新建/抽卡页将单个
     `<select>` 升级为「引擎 (.emp-engine) + 模型」两级联动，精简冗余括号后缀，
     切换引擎后模型下拉同步过滤，且与「灵感词」输入框不重叠；
  2. `generate.js` + `app.css`：提交作业弹窗切到云端计费（显示 `#jm-cost` 与
     `#jm-effective`）时，`#jm-engine` / `#jm-route` 联动正常，且 `.gen-input-review`
     落在第 2 列，不把「提示词」「预计成本」挤进左列或撑爆横向滚动条。

用法：python3 tools/engine_picker_modal_browser_check.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness as H


def _ensure_backend_deps() -> None:
    try:
        import pydantic  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    target = Path(H.backend_python())
    if Path(sys.executable) == target:
        raise SystemExit("后端 venv 缺少依赖，请先按 requirements.txt 安装")
    entry = sys.argv[0] if str(sys.argv[0]).endswith(".py") else __file__
    os.execv(str(target), [str(target), str(Path(entry).resolve()), *sys.argv[1:]])


_ensure_backend_deps()

BACKEND = H.BACKEND
TMP = H.temp_dir("fv-emp-")
PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

COLLECT_ERRORS = """
window.__fvErrors = [];
window.addEventListener('error', e => window.__fvErrors.push(String(e.message)));
window.addEventListener('unhandledrejection',
  e => window.__fvErrors.push('unhandled: ' + String(e.reason)));
"""


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
        raise SystemExit("副本库里没有可用的 admin 账号")
    return int(row["id"])


def mint_admin_session(token_hash: str, admin_id: int) -> None:
    from datetime import datetime, timedelta

    from db import get_conn
    from repositories.users import sessions

    sessions.create(token_hash, admin_id,
                    (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"))
    with get_conn() as conn:
        conn.execute("UPDATE app_user SET must_change_password = 0 WHERE id = ?", (admin_id,))


def seed_db() -> dict:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))

    from repositories.projects import projects as project_repo

    admin = _admin_id()
    project = project_repo.create("门禁校验项目", "两级引擎与弹窗排版回归", [], admin)
    project_id = int(project["id"])
    project_repo.mark_opened(project_id)
    return {"project_id": project_id}


def wait_css(cdp, selector: str, timeout: float = 12.0):
    import proto_cdp_check as P

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if P.eval_js(cdp, f"Boolean(document.querySelector({selector!r}))"):
            return
        time.sleep(0.15)
    raise AssertionError(f"等待元素 {selector!r} 超时")


def go(cdp, url: str) -> None:
    cdp.cmd("Page.navigate", url=url)
    time.sleep(0.45)
    cdp.cmd("Page.reload", ignoreCache=True)
    time.sleep(1.2)


def main() -> int:
    import proto_cdp_check as P

    for key, val in {
        "FASTVIDEO_DB": str(TMP / "platform.db"),
        "FASTVIDEO_FILES": str(TMP / "files"),
        "FASTVIDEO_JOBS": str(TMP / "jobs"),
        "FASTVIDEO_WORKSPACE": str(TMP / "ws"),
    }.items():
        os.environ[key] = val

    H.prepare_backend_db(TMP / "platform.db")
    seed_db()

    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    import security

    token, token_hash = security.new_session_token()
    mint_admin_session(token_hash, _admin_id())

    proc = H.start_service(PORT)
    chrome = None
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}" + (f"  ({detail})" if detail else ""))

    try:
        H.wait_http(f"{BASE}/api/health")
        chrome = H.launch_chrome(P.PORT)
        ws = P.wait_for_target()
        cdp = P.CDP(ws)
        cdp.cmd("Page.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=COLLECT_ERRORS)
        cdp.cmd("Network.setCookie", name="fv_session", value=token, url=BASE, path="/")

        # 1) 资产新建/抽卡页：两级引擎联动 + 标签精简 + 无重叠
        go(cdp, f"{BASE}/#assets/0/anew")
        wait_css(cdp, "#ad-random-card")
        wait_css(cdp, "#ad-rand-model")

        picker_info = P.eval_js(cdp, """(() => {
          const randSel = document.querySelector('#ad-rand-model');
          const tvSel = document.querySelector('#ad-tv-model');
          const randWrap = randSel?.closest('.emp-pair');
          const tvWrap = tvSel?.closest('.emp-pair');
          const randEngine = randWrap?.querySelector('.emp-engine');
          const tvEngine = tvWrap?.querySelector('.emp-engine');
          const engTexts = randEngine ? [...randEngine.options].map(o => o.textContent) : [];
          const hasVerbose = engTexts.some(t => /（(?:官方\\s*API|本机模型服务)）/.test(t));
          const themeInput = document.querySelector('#ad-rand-theme');
          const labelBox = randSel?.closest('.ad-tv-label')?.getBoundingClientRect();
          const themeBox = themeInput?.getBoundingClientRect();
          const overlaps = Boolean(
            labelBox && themeBox &&
            !(labelBox.right <= themeBox.left + 2 || themeBox.right <= labelBox.left + 2 ||
              labelBox.bottom <= themeBox.top + 2 || themeBox.bottom <= labelBox.top + 2)
          );
          let switchedOk = false;
          if (randEngine && randEngine.options.length >= 2) {
            const targetIdx = randEngine.selectedIndex === 0 ? 1 : 0;
            const targetKey = randEngine.options[targetIdx].value;
            randEngine.value = targetKey;
            randEngine.dispatchEvent(new Event('change', { bubbles: true }));
            const vals = [...randSel.options].map(o => o.value);
            switchedOk = vals.length > 0 && vals.every(v => v.startsWith(targetKey + '|'));
          } else {
            switchedOk = Boolean(randEngine && randSel.options.length > 0);
          }
          return {
            hasRandEngine: Boolean(randEngine),
            hasTvEngine: Boolean(tvEngine),
            engCount: engTexts.length,
            hasVerbose,
            overlaps,
            switchedOk,
            engWidth: randEngine ? Math.round(randEngine.closest('.fv-pick')?.getBoundingClientRect().width || randEngine.getBoundingClientRect().width) : 0,
          };
        })()""") or {}

        check("抽卡页人设与生图模型均升级为两级引擎控件",
              picker_info.get("hasRandEngine") and picker_info.get("hasTvEngine"),
              str(picker_info))
        check("引擎下拉选项已去掉冗余括号后缀",
              picker_info.get("engCount", 0) > 0 and not picker_info.get("hasVerbose"),
              str(picker_info))
        check("切换引擎后人设模型下拉同步过滤",
              bool(picker_info.get("switchedOk")),
              str(picker_info))
        check("人设模型区域与灵感词输入框无重叠",
              not picker_info.get("overlaps"),
              str(picker_info))

        # 2) 生成中心弹窗：云端路线下的 #jm-engine 联动 + 两列网格对齐（无错位/无横向溢出）
        go(cdp, f"{BASE}/#generate")
        wait_css(cdp, "#gen-new")
        P.eval_js(cdp, "document.querySelector('#gen-new').click()")
        wait_css(cdp, "#job-modal:not([hidden])")
        time.sleep(0.4)

        modal_info = P.eval_js(cdp, """(() => {
          const cap = document.querySelector('#jm-capability');
          const mode = document.querySelector('#jm-mode');
          cap.value = 'text';
          cap.dispatchEvent(new Event('change', { bubbles: true }));
          const hasCloud = [...mode.options].some(o => o.value === 'cloud');
          mode.value = hasCloud ? 'cloud' : ([...mode.options].find(o => o.value !== 'default')?.value || 'default');
          mode.dispatchEvent(new Event('change', { bubbles: true }));

          const eng = document.querySelector('#jm-engine');
          const route = document.querySelector('#jm-route');
          const box = document.querySelector('#job-modal .modal-box');
          const projCtrl = document.querySelector('#jm-project')?.closest('.fv-pick') || document.querySelector('#jm-project');
          const eff = document.querySelector('#jm-effective');
          const prompt = document.querySelector('#jm-prompt');
          const cost = document.querySelector('#jm-cost');

          const projR = projCtrl?.getBoundingClientRect();
          const effR = eff?.getBoundingClientRect();
          const promptR = prompt?.getBoundingClientRect();
          const costR = cost?.getBoundingClientRect();

          let routeFilteredOk = true;
          if (eng && !eng.hidden && eng.options.length >= 2) {
            const nextIdx = eng.selectedIndex === 0 ? 1 : 0;
            const nextProv = eng.options[nextIdx].value;
            eng.value = nextProv;
            eng.dispatchEvent(new Event('change', { bubbles: true }));
            routeFilteredOk = route.options.length > 0;
          }

          return {
            modeVal: mode.value,
            engVisible: Boolean(eng && !eng.hidden),
            routeFilteredOk,
            overflowX: box ? (box.scrollWidth - box.clientWidth) : -1,
            effLeft: effR ? Math.round(effR.left) : 0,
            effWidth: effR ? Math.round(effR.width) : 0,
            projLeft: projR ? Math.round(projR.left) : 0,
            promptWidth: promptR ? Math.round(promptR.width) : 0,
            costHidden: Boolean(cost?.hidden),
            costWidth: costR ? Math.round(costR.width) : 0,
          };
        })()""") or {}

        check("提交弹窗非默认轨展示 #jm-engine 且可联动",
              modal_info.get("modeVal") == "default" or (modal_info.get("engVisible") and modal_info.get("routeFilteredOk")),
              str(modal_info))
        check("提交弹窗无横向溢出",
              0 <= modal_info.get("overflowX", -1) <= 2,
              str(modal_info))
        check("本次生成输入与提示词落在第 2 列（未错位进左列）",
              modal_info.get("effWidth", 0) >= 300
              and modal_info.get("promptWidth", 0) >= 300
              and abs(modal_info.get("effLeft", 0) - modal_info.get("projLeft", 0)) <= 24,
              str(modal_info))

        errors = P.eval_js(cdp, "(window.__fvErrors || [])")
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
MUTATIONS = """
1. app/frontend/js/engine-model-picker.js:92  shortEngineLabel 去掉 .replace(/（[^）]*）/g, '')
     → 引擎下拉选项已去掉冗余括号后缀                              红
2. app/frontend/css/app.css:4613  去掉 .gen-input-review { grid-column: 2; min-width: 0; }
     → 本次生成输入与提示词落在第 2 列（未错位进左列）              红
"""


if __name__ == "__main__":
    sys.exit(main())
