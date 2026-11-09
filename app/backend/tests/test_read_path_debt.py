"""读路径不出网的**守城河**（原为债务台账，2026-11-09 已还清）。

## 这条测试守什么

"打开一页不出网"：列表／配置这类读接口不许替用户去连服务型引擎
（`SERVICE_ENGINES`：draw_things／ollama）。要现查必须由人点按钮，
走显式入口（`POST /api/models/probe`、`POST /api/engines/{id}/probe`、
「环境检查」）。

## 历史：这里原本是登记债，而且登记本身是坏的

2026-11-09 体检时 `adapters.health()` 的默认值是 `probe_service=True`，
于是**忘记传参就是出网**。当时把它登记成三条 `xfail(strict=True)` 的债，
理由是"翻默认值会动十几个调用点，要单独规划"。

还债时发现那三条登记**一条都没真跑过**，两个毛病叠在一起：

1. fixture 写的是 `as_admin`，而 conftest 里根本没有这个 fixture
   （只有 `as_role` 工厂）。于是 pytest 在 **setup 阶段**就报错，
   `xfail(strict=True)` 把 setup 错误也记成"预期的失败"——
   报出来是 XFAIL，看着像"如实地红着"，实际一条断言都没执行。
2. `test_智能体配置读路径不出网` 借 `draw_things_slot` 当靶子，
   但 `/api/agent/configs` 读的是 `text` 能力行，那个 fixture 改的是 `image`。
   靶子不在被测路径上——即使 fixture 修好、默认值没翻，它也照样绿。

两条合起来的教训：**`xfail` 会吞掉 setup 错误**，所以"台账里有几条红的"
不等于"那几条真的在被验证"。还债时必须先用 `--runxfail` 让它们真的跑一遍。

## 现在

`adapters.health()` 默认已翻成 `False`（默认安全，要现查必须自己写
`probe_service=True`），显式动作方（`validations.env_check`）已显式传 True。
这三条不再是 xfail，是正常护栏；把默认值翻回 True，三条都会红
（已反向验证：`draw_things` ×2、`ollama` ×1）。
"""
import pytest


def _spy_probe(monkeypatch, calls):
    import engines as engines_mod

    real = engines_mod.probe

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(engines_mod, "probe", spy)


@pytest.fixture
def draw_things_slot(app, provider_state):
    """把 image/local 槽位指到 draw_things 并启用——出网那一族要有靶子才测得出。

    provider_state 负责用完还原，不污染别的用例。
    """
    from db import get_conn

    with get_conn() as conn:
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'draw_things', model = 'noobai-xl-epsilon-11',
                      enabled = 1
                WHERE capability = 'image' AND backend = 'local'""")
    yield


@pytest.fixture
def ollama_slot(app, provider_state):
    """把 **text/local** 槽位指到 ollama 并启用。

    2026-11-09 补：`test_智能体配置读路径不出网` 原来借 `draw_things_slot`
    当靶子，但 `/api/agent/configs` 读的是 `text` 能力行，而那个 fixture 改的是
    `image`。靶子不在被测路径上，于是**默认值翻面前它也照样绿**——
    一条证明不了任何东西的登记。改成用 `text/local` 真正的服务型引擎
    （`SERVICE_ENGINES` 里的 `ollama`，`adapters.py:335`）当靶子。

    `provider_state` 负责用完还原。
    """
    from db import get_conn

    with get_conn() as conn:
        conn.execute(
            """UPDATE provider_config
                  SET provider = 'ollama', model = 'qwen2.5:14b',
                      enabled = 1
                WHERE capability = 'text' AND backend = 'local'""")
    yield


def test_提供商列表读路径不出网(as_role, draw_things_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_role("admin").get("/api/providers").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"


def test_可用槽位读路径不出网(as_role, draw_things_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_role("admin").get("/api/providers/available").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"


def test_智能体配置读路径不出网(as_role, ollama_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_role("admin").get("/api/agent/configs").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"

def test_预检不发请求(app, seeded, draw_things_slot, monkeypatch):
    """`precheck.py` 文件头第 1 条纪律："**只读、确定性**。不发请求、不加载模型"。

    这条纪律在 2026-11-09 之前是假的：`precheck.py:225` 直接调
    `adapters.health(route["row"])`，而当时的默认值是 `probe_service=True`——
    人打开预检弹窗就等于让平台替他连一次服务型引擎，而且预检会被界面
    "每次打开弹窗时跑一遍"（文件头原话）。默认翻面后这条才成立。

    与上面三条不同，这里用 `seeded` 里真实的镜：预检会把内容、资产、模型、
    环境、费用、资源六类各查一遍，是"读路径"里最重的一条。
    """
    import precheck

    calls = []
    _spy_probe(monkeypatch, calls)
    # 必须显式要 `image` 并让 `draw_things_slot` 把那个槽位指到服务型引擎：
    # 不加靶子的话预检拿到的是"没配"，一个请求都不发，这条测试就永远绿
    # ——第一版就是这样假绿的，翻回默认值也照样过。
    out = precheck.shot_precheck(seeded["shot_id"], capability="image")
    assert isinstance(out.get("items"), list) and out["items"], "预检没跑出结论"
    assert calls == [], f"预检替连了 {len(calls)} 次网：{calls}"
