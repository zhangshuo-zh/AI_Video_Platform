#!/usr/bin/env python3
"""金额格式化门禁：在真浏览器里跑 `util.formatMoney()` 与 `pipeline.money()`。

为什么要有这一条：`test_infra.py::test_金额格式化只有util一处写法` 只扫源码文本，
它守得住"又写了一份"，守不住"那份写得对不对"。金额是本项目里最容易被
"看起来更整齐"改坏的一格——`toFixed(2)` 比 `formatMoney` 短、更适合贴在一起看，
但它会把 ¥0.0036 显示成 ¥0.00、把 null 显示成 ¥0.00。

判据全是**用户能看见的字符串**，不是实现细节：
  1. `null` 念「—」，不造一个 0；
  2. 小额留 4 位（一笔 ¥0.0036 不能显示成 ¥0.00）；
  3. 大额带千分位；
  4. 币种从数据来（USD 念 $，认不出的币种原样念 code，不猜近义符号）；
  5. 整季 `money()`：无币种念「—」（本地作业不按量计费，不是"花了 0 元"）；
  6. 整季 `money()`：多币种不挑一个贴符号。

`safenet.safe_urlopen` 那批测试的教训是"只测辅助函数等于没测"；这里直接
import 页面真正的模块并调它的函数，不复制一份逻辑到测试里。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "app" / "frontend"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness as H

PORT = H.free_port()
DEBUG_PORT = H.free_port()
while DEBUG_PORT == PORT:
    DEBUG_PORT = H.free_port()
BASE = f"http://127.0.0.1:{PORT}"

# 在页面里执行：真 import 两个模块，把要断言的字符串取回来。
# 全部走 `import()` 而不是读文件拼字符串——那样才能证明"运行时是这个行为"。
PROBE = """
(async () => {
  const util = await import(%(base)s + '/js/util.js?v=money');
  const pipe = await import(%(base)s + '/js/views/pipeline.js?v=money');
  const out = {};

  // 1) null 不许变成 0
  out.null_text = util.formatMoney(null, 'CNY');

  // 2) 小额：¥0.0036 必须留 4 位
  out.tiny_text = util.formatMoney(3600, 'CNY');

  // 3) 大额：千分位
  out.big_text = util.formatMoney(1234567890, 'CNY');

  // 4) 币种从数据来
  out.usd_text = util.formatMoney(5000000, 'USD');
  out.unknown_currency = util.formatMoney(5000000, 'GBP');

  // 5) 整季 money()：无币种（全本地作业）念「—」
  //    pipeline.js 的 money 没有 export，用页面上真正的输出路径验证：
  //    直接调它导出的渲染函数不现实，所以退一步——从模块源码里取函数体求值。
  const src = await (await fetch(%(base)s + '/js/views/pipeline.js?v=money2')).text();
  const m = src.match(/function money\\(cost\\) \\{[\\s\\S]*?\\n\\}/);
  if (!m) { out.pipeline_missing = true; return out; }
  // 函数体里引用的 formatMoney 由 util 提供，其余是纯逻辑
  const make = new Function('formatMoney', m[0] + '; return money;');
  const pmoney = make(util.formatMoney);
  // 2026-11-09：后端改成按币种分桶（`seasons.py` 的 `_season_cost`），
  // 这里跟着换成新入参 `{by_currency, cost_micros, currency, currencies}`。
  // 断言也**加强**了：原来多币种只查"没贴 ¥ 或 $"，那对"什么都不显示"
  // 也成立；现在要求逐桶列出，每个数都得是真和。
  out.season_no_currency = pmoney({by_currency: {}});
  out.season_local_null = pmoney({by_currency: {}, cost_micros: null});
  out.season_null_cost = pmoney(null);
  out.season_cny = pmoney({by_currency: {CNY: 112000}});
  out.season_tiny = pmoney({by_currency: {CNY: 3600}});
  // 两种币种：必须分别念出来，谁也不加谁（3.00 CNY + 2.00 USD ≠ 5.00 任何币）
  out.season_multi = pmoney({by_currency: {CNY: 3000000, USD: 2000000}});
  return out;
})()
"""

# (键, 期望值, 人话说明)
EXPECT = [
    ("null_text", "—", "金额为 null 念「—」，不造一个 ¥0.00"),
    ("tiny_text", "¥0.0036", "小额留 4 位（¥0.0036 不许显示成 ¥0.00）"),
    ("big_text", "¥1,234.57", "大额带千分位"),
    ("usd_text", "$5.00", "币种从数据来（USD 念 $）"),
    ("unknown_currency", "GBP 5.00", "认不出的币种原样念 code，不猜近义符号"),
    ("season_no_currency", "—", "整季无币种念「—」（本地作业不按量计费）"),
    ("season_local_null", "—", "整季金额为 null 且无币种，仍是「—」"),
    ("season_cny", "¥0.11", "整季有 CNY 金额照常显示"),
    ("season_tiny", "¥0.0036", "整季小额同样留 4 位"),
    ("season_null_cost", "—", "整个 cost 为空也不许崩在 / 造数字"),
    ("season_multi", "¥3.00 + $2.00", "多币种逐桶列出，不跨币种相加"),
]


def main() -> int:
    from proto_cdp_check import CDP

    httpd = H.serve_static(FRONTEND, PORT)[0]
    profile = H.temp_dir("moneycheck-")
    chrome = H.launch_chrome(DEBUG_PORT, profile)
    passed = failed = 0
    try:
        cdp = CDP(H.wait_cdp(DEBUG_PORT))
        cdp.cmd("Page.enable")
        cdp.cmd("Runtime.enable")
        # 必须落在同源页面里：about:blank 上 import 相对 URL 会被 CORS 挡
        cdp.cmd("Page.navigate", url=f"{BASE}/index.html")
        time.sleep(1.5)

        expr = PROBE % {"base": json.dumps(BASE)}
        got = cdp.eval(expr)
        if not isinstance(got, dict):
            print(f"✗ 探针没返回对象：{got!r}")
            return 1
        if got.get("pipeline_missing"):
            print("✗ 在 pipeline.js 里找不到 money(cost) 函数")
            return 1

        for key, want, desc in EXPECT:
            if key not in got:
                print(f"  ✗  {desc}  · 探针没取到 {key}")
                failed += 1
                continue
            actual = got[key]
            if actual == want:
                print(f"  OK   {desc}  · {actual!r}")
                passed += 1
            else:
                print(f"  ✗  {desc}  · 期望 {want!r}／实际 {actual!r}")
                failed += 1

        # 多币种：两个币种都得出现，且不许出现一个把它们加起来的数。
        #
        # 这条的前身是「多币种不许贴 ¥ 或 $」——那是 2026-11-09 之前的口径：
        # 后端只能给一个跨币种的假和，界面唯一的正确做法就是什么都不显示。
        # 后端改成按币种分桶之后（`seasons.py` 的 `_season_cost`），正确做法
        # 变成**逐桶列出**，"一个符号都不出现"反而成了漏显示的理由。
        # 所以判据翻转：两个符号都要在，且不许冒出 ¥5.00（= 3 CNY + 2 USD 的假和）。
        multi = got.get("season_multi")
        ok = (isinstance(multi, str) and "¥" in multi and "$" in multi
              and "¥5.00" not in multi and "$5.00" not in multi)
        if ok:
            print(f"  OK   多币种逐个念出、不相加  · {multi!r}")
            passed += 1
        else:
            print(f"  ✗  多币种要逐桶列出且不许相加  · 实际 {multi!r}")
            failed += 1

        print(f"\n{passed}/{passed + failed} 通过")
        return 0 if failed == 0 else 1
    finally:
        try:
            chrome.terminate()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
