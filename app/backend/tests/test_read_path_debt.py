"""读路径出网的**登记债**（2026-11-09 体检发现，本轮不修，只钉住）。

`adapters.health()` 的默认值是 `probe_service: bool = True`（adapters.py:728），
于是每一个忘记传参的调用点都默认出网。模型仓库那一族在 2026-11-07 已经收口
（`GET /api/models` 走 `probe_service=False`，由 test_model_center 的
「目录读路径不出网」守着），但同一把尺子量不到的地方还有一片：

    GET /api/providers            routers/settings.py:53（_row_out）
    GET /api/providers/available  routers/settings.py:104
    GET /api/agent/configs        agents.py:548
    GET /api/precheck/*           precheck.py:225（三个端点共用）

它们都没有传 `probe_service=False`，服务型引擎那一格每次打开页面都真发 HTTP
（超时走 0.8 秒列表档，而 engines.py:181 对短超时结果**不写缓存**，所以连
15 秒 TTL 都不享受）。`agents.py` 文件头那句"本文件一次都不发 HTTP"因此
与代码矛盾——矛盾本身由这条测试说明，修代码之前不改那句注释。

为什么这轮不动它：正确修法是**把默认翻成 False**、让显式动作方
（validate/probe 那两条路由）主动传 True——这会一次改变十几个调用点的行为，
必须先把上面每一族的界面表现核对一遍（"设置页那一格从此显示 probe_deferred"
是不是人要的），属于要单独规划的批次，不是体检顺带能安全合入的量。

机制说明：`xfail(strict=True)` 是**债务台账**——现在它如实记红（XPASS 不会
静默发生）；将来把默认翻过来、这些读路径真不出网了，测试转绿会让 XPASS
报错，提醒来把这条标记摘掉。摘掉后这条就该并进 test_model_center 那支护城河。
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


@pytest.mark.xfail(strict=True, reason=(
    "A1 债：/api/providers* 等读路径仍走 adapters.health 的默认 "
    "probe_service=True，每次打开页面真发 HTTP。修法是翻默认值+显式动作传 True，"
    "见本文件头。转绿时记得摘掉本标记并并入目录侧的出网守卫。"))
def test_提供商列表读路径不出网(as_admin, draw_things_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_admin.get("/api/providers").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"


@pytest.mark.xfail(strict=True, reason="A1 债，同上（提交弹窗那一页）")
def test_可用槽位读路径不出网(as_admin, draw_things_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_admin.get("/api/providers/available").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"


@pytest.mark.xfail(strict=True, reason="A1 债，同上（agents.py 文件头那句承诺）")
def test_智能体配置读路径不出网(as_admin, draw_things_slot, monkeypatch):
    calls = []
    _spy_probe(monkeypatch, calls)
    assert as_admin.get("/api/agent/configs").status_code == 200
    assert calls == [], f"读路径替连了 {len(calls)} 次网：{calls}"
