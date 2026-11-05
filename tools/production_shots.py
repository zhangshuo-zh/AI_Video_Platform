#!/usr/bin/env python3
"""生产主线那几屏的说明书截图（任务书 §18 的"文档与截图"一项）。

为什么要有这个脚本：上一版《生产工作台使用说明书》只有文字口径，一张图没有；
而这一批改动的全部价值恰恰在"人打开这一屏看得见什么"——统计卡、就地勾的清单、
失败节点的七字段目录、弹窗里那句"留空就与素材同尺寸"。文字写对了但图是旧的，
比没图更难查（模型中心那册就撞过一次，所以那边把截图写成了脚本）。

三条纪律，照 `model_center_shots.py`：
* **库副本＋独立端口＋临时工作区**，不往开发库写一行；会话在副本库里铸；
* **只拍界面上真能走到的状态**：数据由 `production_browser_check.seed()` 通过真接口建
  （与真浏览器验收同一批数据），弹窗靠点按钮打开，不注入 DOM、不摆假数字；
* 主题固定白天，与既有截图同一套底色。

每一步（滚到哪、点哪一下）都判找没找到，找不到就直接退出——
一张拍空了的图混进文档，比缺一张图更难发现。

用法：
    python3 tools/production_shots.py
    python3 tools/production_shots.py --only stats,workbench
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import model_center_browser_check as mc      # noqa: E402  同一套壳（副本库、端口、chrome）
import production_browser_check as bc        # noqa: E402  复用它的 seed/api，数据形状与验收一致
import proto_cdp_check as P                  # noqa: E402

ASSETS = ROOT / "文档" / "assets"

# 每一步是一个 (动作, 参数)：
#   click   点第一枚匹配的按钮（开弹窗、进详情都靠它）
#   anchor  滚到标题对得上的那张卡（先精确再前缀：「③ 关键帧（1 条记录）」这种带计数的靠前缀）
#   sel     滚到第一个匹配的选择器（用在没有标题的局部，比如失败节点的 .pl-diag）
#   wait    多等这么久
# 末项是临时视口高度：一屏装不下的长卡拉高了拍，拍完还原。
SHOTS = [
    ("v110-production-board.png", "#production", [], None),
    ("v110-production-stats.png", "#production", [("anchor", "生产统计")], 1240),
    # 「重投卡住的镜头」点完之后的结果条：逐条跳过原因只有点下去才看得见，
    # 而这一条要放在统计那张之后——它会真的重投那条注定失败的 mock 作业
    ("v110-production-retry-batch.png", "#production",
     [("click", "#pb-retry-failed"), ("click", "[data-d-ok]"),
      ("wait", 1.5), ("anchor", "下一步")], 1000),
    ("v110-shot-workbench.png", "{shot_main}", [], None),
    # 有签字对象的那一镜：⑦ 审核这一屏要拍到就地签字与共用清单
    ("v110-shot-workbench-review.png", "{shot_review}",
     [("anchor", "⑦ 审核")], 1300),
    ("v110-review-checklist.png", "#review/1",
     [("click", "[data-open-job]"), ("anchor", "质量清单")], 1300),
    # 产线失败节点：点开运行，滚到那一格的七字段目录
    ("v110-pipeline-node-diagnosis.png", "#pipeline",
     [("click", "[data-open-run]"), ("wait", 1.0), ("sel", ".pl-diag")], 1150),
    # 成片弹窗：打开时那份合成前预检，含"留空就与素材同尺寸"那一行
    ("v110-compose-modal.png", "#composition", [("click", "[data-new-comp]")], 1050),
]


def scroll_to_h2(cdp, want: str) -> bool:
    """滚到标题对得上的那张卡，往上留 70px 让卡片边框完整进画。

    先按整串精确比，比不中退到前缀——`③ 关键帧（3 条记录）` 这种带计数的标题
    写不死，但读的人认的还是开头那几个字。
    """
    return bool(cdp.eval(
        "(() => { const ws = " + json.dumps(want, ensure_ascii=False) + ";"
        "const hs = [...document.querySelectorAll('h2')];"
        "const hit = hs.find((x) => x.textContent.trim() === ws)"
        " || hs.find((x) => x.textContent.trim().startsWith(ws));"
        "if (!hit) return 0; const card = hit.closest('.card') || hit;"
        "card.scrollIntoView({block: 'start'}); window.scrollBy(0, -70); return 1; })()"))


def run_steps(cdp, name: str, steps: list) -> None:
    """按声明执行每一步；任何一步在界面上找不到，就叫这一轮停下来。"""
    for kind, arg in steps:
        if kind == "wait":
            time.sleep(arg)
        elif kind == "click":
            ok = cdp.eval("(() => { const el = document.querySelector("
                          + json.dumps(arg) + ");"
                          " if (el) { el.click(); return 1; } return 0; })()")
            assert ok == 1, f"{name} 找不到 {arg}，这一屏打不开"
            time.sleep(1.2)          # 等接口回来把弹窗／详情画满
        elif kind == "anchor":
            assert scroll_to_h2(cdp, arg), f"{name} 找不到标题「{arg}」"
            time.sleep(0.4)
        elif kind == "sel":
            ok = cdp.eval("(() => { const el = document.querySelector("
                          + json.dumps(arg) + ");"
                          " if (!el) return 0; el.scrollIntoView({block: 'center'});"
                          " return 1; })()")
            assert ok == 1, f"{name} 找不到 {arg}，这一格没出现在界面上"
            time.sleep(0.4)
        else:
            raise AssertionError(f"{name} 不认的动作 {kind}")


def approve_episode(token: str, data: dict) -> str:
    """把第 1 集三镜都推到「有签过通过的正式视频」，返回一句话说明。

    为什么要现场补这一步：成片弹窗里那句"分辨率两栏留空就与素材同尺寸"
    是 `composition_precheck` 在**时间线真能拼起来**时才带出来的读数
    （`output_plan` 取自 `timeline.build()` 的 `output_size`）。种下的数据只有
    一镜有产物，预检全篇阻塞，图上是"拦住了"；说明书 §9 讲的却是"能拼时怎么填"。
    所以这里走真接口把缺的补齐：提交→worker 真跑→勾清单→签字，一步都不注水。
    放在第 6 张之后执行，前面那两张"待签"的图才拍得到未签状态。
    """
    signed = []
    for shot in data["shots"]:
        job = bc.api("/api/jobs", token, "POST", {
            "project_id": data["project"]["id"], "episode_id": data["episode"]["id"],
            "shot_id": shot["id"], "capability": "video", "execution_mode": "local",
            "provider_code": "mock", "model_code": "mock-1",
            "prompt": f"截图用 · 第 {shot.get('seq', '?')} 镜",
            "params": {"sleep": 0.2, "media": "mp4", "color": "green", "seconds": 1.0}})
        for _ in range(60):
            state = bc.api(f"/api/jobs/{job['id']}", token).get("status")
            if state in ("succeeded", "failed", "needs_attention"):
                break
            time.sleep(0.5)
        if state != "succeeded":
            raise AssertionError(f"镜 #{shot['id']} 的截图作业没成功：{state}")
        cl = bc.api(f"/api/jobs/{job['id']}/review-checklist", token)
        items = [{"code": m["code"], "state": "pass", "note": "截图前逐条看过"}
                 for m in (cl.get("human") or []) if m.get("required")]
        if not items:
            raise AssertionError(f"作业 #{job['id']} 的人工清单读不到必看项，签不成")
        bc.api(f"/api/jobs/{job['id']}/review-checklist", token, "POST", {"items": items})
        anchors = (bc.api(f"/api/jobs/{job['id']}/review", token).get("anchors") or [])
        anchor = anchors[0] if anchors else "artifact"
        bc.api(f"/api/jobs/{job['id']}/review-sign", token, "POST",
               {"anchor": anchor, "decision": "approved", "note": "截图用·验收通过"})
        signed.append(job["id"])
    return f"已签 {len(signed)} 条（作业 {'、'.join(str(i) for i in signed)}）"


# 拍某一张之前要先用真接口把业务推到那个状态。顺序有意义：
# 签字会消掉"待签"，所以第 5、6 张必须在未签状态先拍完。
HOOKS = {"v110-compose-modal.png": approve_episode}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔关键词，只拍名字里带它的")
    args = ap.parse_args()
    wanted = [w.strip() for w in args.only.split(",") if w.strip()]

    # mock 轨只在 ALLOW_MOCK=1 时才注册，而 seed() 那两条作业走的就是它。
    # 必须在起后端之前设——config.py 导入时就把环境变量读成常量了。
    os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"

    mc.prepare_env()
    token = mc.mint_admin_session()
    proc = subprocess.Popen(
        [str(mc.BACKEND / ".venv/bin/python"), "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", str(mc.PORT), "--log-level", "warning"],
        cwd=str(mc.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    worker = None
    chrome = None
    try:
        mc.wait_server()
        worker = subprocess.Popen(
            [str(mc.BACKEND / ".venv/bin/python"), "worker.py", "--poll", "0.2"],
            cwd=str(mc.BACKEND), stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(1.2)
        data = bc.seed(token)      # 真接口建数据：与真浏览器验收同一批，形状变了这里先响
        # 镜头工作台要带具体 id（地址是三段式 #shot/0/a<id>）：
        #   shots[0] 设了资产与镜头语言 → 拍整屏结构
        #   shots[1] 有一条待签产物     → 拍 ⑦ 审核那一屏
        urls = {"{shot_main}": f"#shot/0/a{data['shots'][0]['id']}",
                "{shot_review}": f"#shot/0/a{data['shots'][1]['id']}"}

        chrome = mc.start_chrome(P.PORT)
        cdp = P.CDP(P.wait_for_target())
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        cdp.cmd("Network.enable")
        cdp.cmd("Page.addScriptToEvaluateOnNewDocument", source=mc.COLLECT_ERRORS)
        cdp.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                deviceScaleFactor=1, mobile=False)
        cdp.cmd("Network.setCookie", name="fv_session", value=token,
                url=mc.BASE, path="/", httpOnly=True)
        # 主题：先落首页把 localStorage 定死成白天，再进各页——
        # index.html 头部脚本按这个键决定 data-theme，晚设等于晚一页
        cdp.cmd("Page.navigate", url=f"{mc.BASE}/")
        time.sleep(1.2)
        cdp.eval("(() => { try { localStorage.setItem('fv.theme', 'day'); }"
                 " catch (e) {} return 1; })()")

        print("拍摄（只拍界面上真能走到的状态）：", flush=True)
        for spec in SHOTS:
            name, url, steps = spec[0], spec[1], spec[2]
            tall = spec[3] if len(spec) > 3 else None
            if wanted and not any(w in name for w in wanted):
                continue
            if name in HOOKS:
                print(f"  准备 {name}：{HOOKS[name](token, data)}", flush=True)
            mc.go(cdp, f"{mc.BASE}/{urls.get(url, url)}")
            theme = cdp.eval("(() => document.documentElement.dataset.theme || 'day')()")
            assert theme == "day", f"{name} 拍到了 {theme} 主题，与既有截图不一致"
            if tall:
                cdp.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=tall,
                        deviceScaleFactor=1, mobile=False)
                time.sleep(0.3)
            run_steps(cdp, name, steps)
            body = (P.text_of(cdp, "#view-root") or "")
            # 内容偏少＝这一屏是空态。空态拍进说明书会教人做出错误假设
            assert len(body) >= 200, f"{name} 页面文字只有 {len(body)} 字，疑似空态"
            raw = cdp.cmd("Page.captureScreenshot", format="png")["data"]
            if tall:
                cdp.cmd("Emulation.clearDeviceMetricsOverride")
                cdp.cmd("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                        deviceScaleFactor=1, mobile=False)
            out = ASSETS / name
            out.write_bytes(base64.b64decode(raw))
            print(f"  拍好 {name}  {out.stat().st_size // 1024} KB  · 页面文字 {len(body)} 字",
                  flush=True)
        errs = P.eval_js(cdp, "window.__errs || []")
        print(f"未捕获 JS 异常：{errs}")
        assert not errs, "截图过程中有未捕获的 JS 异常，这些图别进文档"
        print(f"共 {len(SHOTS)} 张的目标目录：{ASSETS}")
        return 0
    finally:
        if worker and worker.poll() is None:
            worker.terminate()
        if chrome:
            chrome.terminate()
        proc.terminate()
        time.sleep(0.4)


if __name__ == "__main__":
    sys.exit(main())
