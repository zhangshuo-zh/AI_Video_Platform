#!/usr/bin/env python3
"""模型中心的说明书截图：真浏览器、真数据、库副本，一屏一张。

为什么要有这个脚本而不是手点：说明书里的图必须与被描述的那一版界面一致。
上一版四张图（同日 11:42）拍的是密度收口之前的界面——概览还是一整张卡、
能力轨还是每槽两行，而正文已经改成"收成一条带／一槽一行"。图与文字对不上，
比没有图更难查。所以截图这件事写成脚本：改完界面重跑一条命令，图就追平。

隔离：库副本＋临时工作区＋独立端口（照 `model_center_browser_check.py` 那一套），
不往开发库写一行；会话直接在副本库里铸，不碰任何口令。主题固定白天——
与既有截图同一套底色，两套主题混排会在文档里显得像两个产品。

用法：python3 tools/model_center_shots.py            # 拍全部
      python3 tools/model_center_shots.py --only repo,engines
"""
from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "文档" / "assets"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_center_browser_check as mc      # noqa: E402

# (文件名, 二级页, 打开前要点的那一下, 等待秒数[, 视口高度])
# 第五项只在"长表要一张看完"时给：临时把视口拉高再拍，拍完还原。
# 只拍"界面上真能走到的状态"：弹窗一律靠 click 打开，不注入 DOM，不摆假数据。
SHOTS = [
    ("v99-model-repo.png", "#models/0", None, 0.4),
    # v0.117：登记弹窗的 provider／模型标识改成严格下拉，旧的 v99-model-register.png
    # 拍的是两个自由文本框——图文不符比没图更难查，所以这一张重拍，并且补拍一张
    # 「清单只登记了名字、模型 ID 退回手填」的那一档：那句话光写在正文里看不见。
    ("v117-model-register.png", "#models/0", "#mc-add", 0.6),
    ("v117-model-register-hand.png", "#models/0",
     "@js:(() => { document.querySelector('#mc-add').click();"
     " const set = (id, v) => { const e = document.querySelector(id);"
     " e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); };"
     " set('#mf-cap', 'image'); set('#mf-be', 'cloud'); return 1; })()", 0.6),
    ("v99-model-detail.png", "#models/0", "[data-detail]", 0.9),
    ("v99-model-engines.png", "#models/1", None, 0.4),
    # 引擎页下半：清单里登记过的适配器＋未接入那两排。一屏装不下，必须滚过去再拍一张，
    # 否则说明书里"未接入的 ComfyUI 没有按钮"这句话没有图可对
    ("v99-model-engines-table.png", "#models/1", "@anchor:引擎", 0.5, 1420),
    ("v99-model-validation.png", "#models/1", "[data-val-history]", 0.9),
    ("v99-model-policies.png", "#models/2", None, 0.4),
    ("v99-model-resolve.png", "#models/2", "[data-resolve]", 1.0),
    ("v99-model-monitor.png", "#models/3", None, 0.4),
    # 监控页下半：模型维度（登记/可用/需要看一眼）与"三项做不到"的如实标注
    ("v99-model-monitor-models.png", "#models/3", "@anchor:模型维度", 0.5, 470),
    # v0.111：系统设置的「用户与角色」拆成两页。旧的 v86-users.png 拍的是
    # 一页装三块的样子，拆完再拿它配图就是图文不符——所以这两张必须重拍。
    ("v111-system-users.png", "#system-config/0", None, 0.5, 1150),
    ("v111-system-roles.png", "#system-config/1", None, 0.5, 1250),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的关键词，只拍名字里带它的")
    args = ap.parse_args()
    wanted = [w.strip() for w in args.only.split(",") if w.strip()]

    mc.prepare_env()
    token = mc.mint_admin_session()
    proc = subprocess.Popen(
        [str(mc.H.backend_python(mc.BACKEND)), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(mc.PORT), "--log-level", "warning"],
        cwd=str(mc.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    chrome = None
    try:
        mc.wait_server()
        import proto_cdp_check as P

        chrome = mc.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=mc.BASE, path="/", httpOnly=True)
        # 主题：先落到首页把 localStorage 定死成白天，再进各页——
        # index.html 的头部脚本按这个键决定 data-theme，晚设就等于晚一页
        cdp.cmd("Page.navigate", url=f"{mc.BASE}/")
        time.sleep(1.0)
        cdp.eval("(() => { try { localStorage.setItem('fv.theme', 'day'); } catch (e) {} return 1; })()")

        for shot in SHOTS:
            name, sub, click_sel, wait = shot[:4]
            tall = shot[4] if len(shot) > 4 else None
            if wanted and not any(w in name for w in wanted):
                continue
            mc.go(cdp, f"{mc.BASE}/{sub}")
            theme = cdp.eval("(() => document.documentElement.dataset.theme || 'day')()")
            assert theme == "day", f"{name} 拍到了 {theme} 主题，与既有截图不一致"
            if click_sel and click_sel.startswith("@anchor:"):
                # 滚到某张小卡的抬头（按 h2 文案找），再往上留 70px 让卡片边框完整。
                # 直接滚到底会把这小节拽掉半截——截图上从半行开始，读的人不知道这是哪一段。
                want = click_sel.split(":", 1)[1]
                ok = cdp.eval(
                    "(() => { const h = [...document.querySelectorAll('h2')]"
                    ".find((x) => x.textContent.trim() === %r);"
                    "if (!h) return 0; const card = h.closest('.card') || h;"
                    "card.scrollIntoView({block: 'start'}); window.scrollBy(0, -70); return 1; })()"
                    % want)
                assert ok == 1, f"{name} 找不到标题「{want}」"
            elif click_sel and click_sel.startswith("@js:"):
                # 有些状态要点开弹窗再改一格才出现（登记弹窗的运行位置就是那一格）。
                # 这里发的是人选那一项时浏览器自己会派的 change 事件，
                # 不注入 DOM、不摆假数据、也不绕过任何校验。
                ok = cdp.eval(click_sel.split(":", 1)[1])
                assert ok, f"{name} 那段操作没走通：{ok}"
            elif click_sel == "@bottom":
                cdp.eval("(() => { window.scrollTo(0, document.body.scrollHeight); return 1; })()")
            elif click_sel:
                ok = cdp.eval("(() => { const el = document.querySelector(%r); "
                              "if (el) { el.click(); return 1; } return 0; })()" % click_sel)
                assert ok == 1, f"{name} 找不到 {click_sel}，这一屏打不开"
            if tall:
                cdp.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=tall,
                        deviceScaleFactor=1, mobile=False)
                time.sleep(0.3)
            time.sleep(wait)
            data = cdp.cmd("Page.captureScreenshot", format="png")["data"]
            if tall:
                cdp.cmd("Emulation.clearDeviceMetricsOverride")
            out = ASSETS / name
            out.write_bytes(base64.b64decode(data))
            print(f"  拍好 {name}  {out.stat().st_size // 1024} KB")
        print(f"共 {len(SHOTS)} 张的目标目录：{ASSETS}")
        return 0
    finally:
        if chrome:
            chrome.terminate()
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
