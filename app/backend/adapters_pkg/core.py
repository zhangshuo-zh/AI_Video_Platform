"""Adapter 能力清单（W06 建立，W09 接执行）。

这张表回答的是"平台**凭什么**认为某个 provider 能用哪些参数"。

规格 01-4.8.2 的那条硬要求：**能力不能靠模型名猜**。所以每个档位都带
`source`——它要么来自本机 CLI 的 `--help`/参数表，要么来自模型卡或设计稿
§7.1 的硬规格；没有核过的一律不填参数，只写"未核验"。界面拿到空参数时
只会显示"该 provider 参数未核验"，不会自己脑补宽高或步数。

三条边界，改这张表之前先读：

1. **这里不执行任何模型**。清单与健康只回答"凭什么认为它能用、缺什么"。
   W09 起健康检查从"运行时目录在不在"升级为 `runtime.py` 的逐项探测
   （解释器、入口脚本、权重、CLI），仍然不加载权重、不发网络请求；
   真起子进程的是 `worker.py` + `runners.py`。
2. **本地运行时路径必须由环境变量给**（`FASTVIDEO_<CODE>_HOME`）。写死
   /Users/... 会让部署到 Studio 后健康检查永远显示"坏"；给不出来就诚实地
   回 unknown，而不是假装 ok。
3. **密钥只认变量名**。清单里出现的 key_env 是"建议用哪个变量"，值永远
   只从进程环境读，回给前端的最多是一个布尔值。
"""
from __future__ import annotations

from log import get_logger as _get_logger
import sqlite3


from .types import Param as Param, Price as Price, ModelSpec as ModelSpec, Adapter as Adapter
from .video import _FASTMETAL as _FASTMETAL, _MLX_VIDEO as _MLX_VIDEO, _WAN as _WAN, _VIDEO_CLOUD as _VIDEO_CLOUD
from .image import _MFLUX as _MFLUX, _DRAW_THINGS as _DRAW_THINGS, _COMFYUI as _COMFYUI
from .text import _OLLAMA as _OLLAMA, _DEEPSEEK as _DEEPSEEK, _TEXT_CLOUD as _TEXT_CLOUD
from .audio import _VOXCPM as _VOXCPM, _ACE_STEP as _ACE_STEP
from .cloud_other import _CLOUD_OTHER as _CLOUD_OTHER
import os
import hashlib
import re
from dataclasses import dataclass
import engines
import runtime

_audit_log = _get_logger(__name__)

ADAPTERS: tuple[Adapter, ...] = (
    _FASTMETAL, _MLX_VIDEO, _MFLUX, _DRAW_THINGS, _COMFYUI, _OLLAMA, _VOXCPM,
    _ACE_STEP, *_TEXT_CLOUD, *_VIDEO_CLOUD, *_CLOUD_OTHER,
)

# 清单的版本号：Job 建单时冻结它，将来改清单能看出这条作业是按哪一版参数放行的。
# 2026-11-06.1＝W-B：新增本机服务型引擎 draw_things（执行形状与 mflux 那一族不同）。
# 2026-11-06.2＝智能体中心 A4：新增本地文本腿 ollama（平台第一条 text×local）。
# 2026-11-13.1＝ComfyUI **只接探测**：新增一条 models=() 的壳，让引擎页看得见它
#   通不通；没有 workflow 契约，所以不进 runners.build()、不进提交框。
# 2026-11-16.1＝ComfyUI 接通工作流执行契约（comfyui-workflow）。
MANIFEST_VERSION = "2026-11-16.1"

# 假适配器只给测试与演示用（config.ALLOW_MOCK 控制），不进清单。
# 它不执行任何命令，只按参数睡一会儿、写一个文本结果——这样队列语义可以
# 在没有模型权重的机器上被测穿，而不会假装 FastMetal 已经接通。
MOCK_PARAMS: tuple[Param, ...] = (
    Param("sleep", "float", 0.6, 0.0, 30.0, note="秒"),
    Param("fail", "bool", False, note="置 true 走失败分支"),
    Param("text", "str", "mock output", note="写进结果文件的文本"),
    # media 决定产物是**什么文件**。默认 text 只证明队列语义；mp4／png 让 worker
    # 用本机 ffmpeg 现造一段可播放的纯色片或一张图。为什么要这一档：
    # "生成 → 审核签字 → 时间线取片 → 合成 → 导出"这条链要求产物能被 ffprobe 读出
    # 视频流，文本文件走到底只会卡在合成前检查，业务验收就只能停在"作业成功了"。
    # 它仍然是假适配器——不做任何模型计算，只把产物做成真的媒体；
    # 因此 `model_validation` 依旧不给它记真实调用，production_ready 不亮。
    Param("media", "str", "text", choices=("text", "mp4", "png"),
          note="产物类型：text／mp4／png（后两者用本机 ffmpeg 现造）"),
    # 底色只给枚举，不给自由文本：lavfi 的 `color=c=<x>` 会拼进源串，
    # 收死成六色就不用在这里再写一套转义校验。
    Param("color", "str", "red",
          choices=("red", "green", "blue", "black", "white", "gray"),
          note="mp4／png 的底色"),
    Param("seconds", "float", 1.0, 0.2, 5.0, note="mp4 时长（秒）"),
)
MOCK_MODEL = "mock-1"

# 同一 capability+backend 下 provider 允许用别名找（用户可能填 mlx_video）。
_INDEX: dict[tuple[str, str, str], Adapter] = {}
for _a in ADAPTERS:
    for _name in (_a.code, *_a.aliases):
        _INDEX[(_a.capability, _a.backend, _name.strip().lower())] = _a


def find(capability: str, backend: str, provider: str) -> Adapter | None:
    """按"能力 + 后端 + provider 标识"找登记项；找不到就是没登记，不猜。"""
    key = (capability, backend, (provider or "").strip().lower())
    return _INDEX.get(key)


def effective_key_env(provider: str, configured: str = "",
                      capability: str = "", backend: str = "cloud") -> str:
    """这条槽位到底去哪个环境变量找密钥。

    优先级：用户填的名字 > 清单给的建议名 > FV_KEY_<PROVIDER>。
    中间那一档是为了 DEEPSEEK_API_KEY 这种既有名字：选 deepseek 就自动用它，
    不必逼人再去 ~/.zshrc 里多加一份同值不同名的变量。
    """
    name = (configured or "").strip()
    if name:
        return name
    adapter = find(capability, backend, provider)
    if adapter is not None and adapter.key_env:
        return adapter.key_env
    provider = (provider or "").strip()
    if not provider:
        return ""
    return "FV_KEY_" + provider.upper().replace("-", "_").replace(" ", "_")


def resolved_key_env(provider: str, capability: str, backend: str,
                     row=None) -> str:
    """这条槽位到底从哪个环境变量读密钥——展示与执行的唯一出处。

    `health()`／`has_key()` 已经拿着配置行，直接把 row 传进来；执行链
    （`runners._cloud`）只有 provider，就按 (capability, backend) 现查槽位。
    过去执行链把 configured 写死成空串，于是管理员填的自定义变量名只在
    界面生效，真正发请求时回落到清单建议名：界面说「密钥就绪」、作业却
    起不来；两个变量都有值时更会用上一个没被确认过的密钥。
    """
    if row is None:
        from repositories.providers import providers   # 延迟导入，避开 adapters ↔ repositories 环
        row = providers.slot(capability, backend)
    configured = ""
    if row is not None and "key_env" in row.keys():
        # 槽位换了 provider 时，旧行里那个变量名不属于新 provider，回落清单名。
        if (row["provider"] or "").strip() == (provider or "").strip():
            configured = row["key_env"] or ""
    return effective_key_env(provider, configured, capability, backend)


def key_present(env_name: str) -> bool:
    """只看有没有，不看值；空名字自然算没有。

    2026-11-11 起也认**库里的密文**：`secret_store.present()` 不解密，
    所以它回答的是"配过没有"而不是"现在能不能用"。要后者用
    `secret_store.resolve()`（它会把解不开变成异常）。

    这个区分是有意的：这里被探测/健康检查高频调用，每次解密既慢又
    把明文摊得更开；而"配了没配"正是健康徽章要答的问题。
    """
    if not env_name:
        return False
    if os.environ.get(env_name):
        return True
    try:
        import secret_store
        return secret_store.present(env_name)
    except (ImportError, sqlite3.Error) as exc:
        _audit_log.debug("key_present_fallback error=%s", type(exc).__name__)
        return False


def key_fingerprint(env_name: str) -> dict:
    """密钥的"指纹"：**能用来比对是哪一把，但推不回原值**。

    为什么要它（2026-11-11）：配置文件里可能同时存在好几把 key（旧的没删、
    新加的、按项目分开的），人在供应商后台看到一串 key 时无法确认"这台机器
    上配的是哪一把"，而让他去 `cat ~/.zshrc` 看等于给了 shell。指纹解决的就
    这一个问题：把 `sha256(值)` 取前 8 位印在页面上，人对着后台一比就知道。

    三个必须守住的边界：

    1. **不落库、不落日志**。每次现算现扔。认证 `security.py` 里存的是 scrypt
       派生值（那是为了验口令）；这里连派生值都不存——它只需要活在一次响应里。
    2. **只给 `system:config` 的人看**。它推不回原值，但仍能让拿到泄漏 key 的人
       确认"是不是这一把"，且露出的前 3 位也是真值。路由层挡权限。
    3. **值本身一个字符都不出现**。`preview` 只露供应商前缀（`sk-`／`sk-ant-`
       这类公开约定）与哈希前 8 位，中间用省略号——**不是值的前 8 位**。
       这一点最容易写错：顺手写成 `value[:8]` 就把它变成了"泄漏 1/6 的密钥"。

    返回 `{"present": False}` 表示没配；配了则给长度与指纹。**长度**是有用的：
    复制粘贴时多一个换行或少几个字符，人对一下长度就能发现。
    """
    value = os.environ.get(env_name) or ""
    if not value:
        return {"present": False}
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8].lower()
    # 供应商前缀按公开约定露出来（`sk-`、`sk-ant-`、`sk-proj-` 之类）。
    # 只认"字母数字加连字符、以 sk- 开头、长度 ≤ 12"的那种；认不出就不露。
    prefix = ""
    m = _KEY_PREFIX_RE.match(value)
    if m:
        prefix = m.group(0)
    return {
        "present": True,
        "length": len(value),
        "fingerprint": digest,
        # 示例：`sk-…a3f9c1d2`。**中间是省略号 + 哈希，不是值本身**
        "preview": f"{prefix}…{digest}" if prefix else f"…{digest}",
    }


def _preview_for_db(digest: str, family: str = "") -> str:
    """按**指纹**（不含明文）拼出页面用的 preview：`sk-…a3f9c1d2`。

    为什么单独开一个：库里只有指纹，没有明文，所以走不了 `key_fingerprint`。
    家族标记（`sk-` 那类）在库里也没存，默认省略——**省略比猜错好**：
    猜成 `sk-` 而实际是 AWS 风格会让人比对错后台。

    这是本模块唯一"只吃指纹就产出显示值"的函数，刻意做得很小，
    免得有人当成"能还原明文"的路子。
    """
    return f"{family}…{digest}" if family else f"…{digest}"


# **只认"公开约定的家族标记"，绝不匹配值本身**（2026-11-11 自查修的真错）。
#
# 第一版写成 `^(sk-[a-z0-9-]{0,9}-?)`，本意是"露出供应商前缀"，
# 实测发现它把真实密钥的前 12 个字符印在了页面上（`sk-a1b2c3d4e…748986e2`）——
# 那不是"前缀"，是**泄漏了一部分密钥**。攻击者拿到这 12 个字符可以直接去
# 暴力补全剩下的部分，比纯哈希弱得多。
#
# 所以收紧成一张**封闭白名单**：只认几家已知供应商的家族标记（它们本身就是
# 公开文档里写着的名字，不含任何随机部分）。认不出的，一个字符都不露。
_KEY_PREFIX_RE = re.compile(
    r"^(sk-ant-|sk-proj-|sk-or-|sk-)")


def _type_ok(kind: str, value) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        # bool 是 int 的子类，不单独排除的话 true 会被当成 1 混过去
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind in ("str", "image"):
        return isinstance(value, str) and value != ""
    return False


def _check_one(param: Param, value) -> list[str]:
    if not param.required and (value is None or value == ""):
        return []
    if not _type_ok(param.type, value):
        return [f"参数 {param.name} 的类型应为 {param.type}"]
    out: list[str] = []
    if param.choices and value not in param.choices:
        out.append(f"参数 {param.name} 只能是：{'、'.join(param.choices)}")
    if param.type in ("int", "float"):
        if param.minimum is not None and value < param.minimum:
            out.append(f"参数 {param.name} 不能小于 {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            out.append(f"参数 {param.name} 不能大于 {param.maximum}")
        if param.multiple_of and param.multiple_of > 1 and value % param.multiple_of:
            out.append(f"参数 {param.name} 必须是 {param.multiple_of} 的倍数")
    return out


def validate_params(adapter: Adapter | None, model_code: str, params) -> list[str]:
    """按清单白名单校验参数；返回人话错误列表，空列表＝通过。

    未核验的云端 provider（models 为空）**不许带参数**：没有白名单就等于
    "模型名听着像"也能塞东西进去，规格 01-4.8.2 明令禁止。
    """
    if not isinstance(params, dict):
        return ["params 必须是一个对象"]
    if adapter is None:
        return ["provider 没有登记适配器，无法校验参数"]
    if not adapter.models:
        if params:
            return ["该 provider 的参数尚未核验，W07 不接受任何参数"]
        return []

    model = next((m for m in adapter.models if m.code == model_code), None)
    if model is None:
        known = "、".join(m.code for m in adapter.models)
        return [f"模型 {model_code} 不在清单里；可选：{known}"]

    spec = {p.name: p for p in model.params}
    errors: list[str] = []
    for name, value in params.items():
        param = spec.get(name)
        if param is None:
            errors.append(f"参数 {name} 不在 {model.code} 的白名单里")
        else:
            errors.extend(_check_one(param, value))
    for param in model.params:
        if param.type == "image" and param.required and param.name not in params:
            errors.append(f"缺少必填参数：{param.name}")
    return errors


def validate_mock_params(params) -> list[str]:
    """假适配器自己的白名单；它不进清单，所以单独校验。"""
    if not isinstance(params, dict):
        return ["params 必须是一个对象"]
    spec = {p.name: p for p in MOCK_PARAMS}
    errors: list[str] = []
    for name, value in params.items():
        param = spec.get(name)
        if param is None:
            errors.append(f"参数 {name} 不在 mock 的白名单里")
        else:
            errors.extend(_check_one(param, value))
    return errors


def health(row, *, probe_service: bool = False,
           probe_timeout: float | None = None) -> dict:
    """实时健康：不加载权重；除服务型引擎那一族外不发请求（例外见下面第三段）。

    `probe_service` 默认 **False**（2026-11-09 翻面，原为 True）。

    为什么翻面：默认 True 时，**忘记传参就是出网**——十来个调用点里任何一个
    漏传，打开一页就顺带扫一遍网，而"漏传"这种错误不会有人发现（页面只是慢）。
    默认 False 之后语义反过来：默认安全，要现查的人**必须自己写出来**。
    这也让"读接口不出网、探测走显式入口"从一条要靠人记住的规矩
    变成函数的默认行为。

    四种状态分开，是因为人要做的事不同：
    unconfigured＝先选 provider；unregistered＝清单没这个 provider（不要硬填）；
    missing＝该配的东西没配；unknown＝平台检查不了，需要人去确认。

    **唯一的例外是服务型引擎**（Draw Things／Ollama 这一类）：它们的真相在
    "端点连不连得上"，只能发一次 HTTP 才答得出。那一次网络请求现在要**显式要**
    （`probe_service=True`），否则返回 `unknown` 并把"该怎么现查"写进 detail。
    为什么要这个开关：列表接口（模型仓库、目录、场景候选）一次要拼几十行，
    里面每一行都去连一次服务，等于"打开一页"变成"打开一页顺带扫一遍网"——
    一台卡住的服务就能把整页拖到超时。读接口不出网，探测走显式入口
    （`POST /api/models/probe`、`POST /api/engines/{id}/probe`、那一行的
    「环境检查」），这条边界与 `runtime.py` 模块头那句"不发网络请求"是同一个道理。
    """
    provider = (row["provider"] or "").strip()
    capability = row["capability"]
    backend = row["backend"]
    if not provider:
        return {"state": "unconfigured", "detail": "还没选 provider",
                "adapter": None, "key_env": ""}

    adapter = find(capability, backend, provider)
    if adapter is None:
        return {"state": "unregistered",
                "detail": "这个 provider 没有登记适配器，平台不会猜它的参数与运行方式",
                "adapter": None, "key_env": ""}

    base = {"adapter": {"code": adapter.code, "label": adapter.label,
                        "source": adapter.source, "params_source": adapter.params_source},
            "key_env": ""}
    if backend == "cloud":
        env = resolved_key_env(provider, capability, backend, row)
        base["key_env"] = env
        if not env:
            return {**base, "state": "unconfigured", "detail": "没有可用的密钥变量名"}
        if key_present(env):
            return {**base, "state": "ok", "detail": f"密钥来自环境变量 {env}"}
        return {**base, "state": "missing",
                "detail": f"环境变量 {env} 没有值；改完要重启服务"}

    # 本地不再只看"目录在不在"：W09 真要起子进程，解释器、入口脚本、
    # 权重四件套缺一件都得提前说清。runtime.py 只做文件系统检查，不加载权重。
    base["key_env"] = ""
    model = (row["model"] or "").strip()
    if not model:
        return {**base, "state": "missing", "detail": "选了 provider 但还没填模型档"}
    if adapter.code in engines.SERVICE_ENGINES:
        # 本机服务型引擎（Draw Things 这一类）没有"仓库四件套"可查：它的真相是
        # 端点连不连得上、引擎当前选的是哪一档权重。探测住在 engines.py 而不是
        # runtime.py——后者的模块头明写着"不发网络请求"，那条边界正是设置页
        # 敢随手调用它的前提，破了它"点开一页设置会不会联网"就变成要看代码才知道。
        endpoint = engines.resolve_endpoint(adapter.code)
        if not probe_service:
            # 不代连，但也不装死：三条出路各说各的。
            # ① 有 15 秒内的现查结论 → 用它，并把"多久前查的"一起带出去
            #    （拿旧结论冒充现在才是最坏的那种诚实）；
            # ② 没实例 → 明确说去哪儿加；
            # ③ 有实例但没查过/查过期了 → 说清楚"要连一次才知道"，并指出入口。
            # `probe_deferred` 是给调用方看的：平台**会**查，只是这条读路径不该
            # 替人连一次网。它与真·unknown（平台自己也查不了）不是同一档。
            cached = engines.peek(adapter.code, endpoint)
            if cached is not None and cached[1] <= engines.PROBE_TTL_SECONDS:
                status, age = cached
                return {**base, "state": status.state,
                        "detail": f"{status.detail}（{age:.0f} 秒前查的）",
                        "engine_model": status.values.get("model") or "",
                        "endpoint": status.values.get("endpoint") or endpoint,
                        "probed_seconds_ago": round(age, 1)}
            return {**base, "state": "unknown", "probe_deferred": True,
                    "detail": ("还没登记启用中的引擎实例：在「引擎管理 · 引擎实例」里"
                               "加一条端点后才会检查") if not endpoint else
                              ("这一档要连一次服务才知道状态；打开页面不代你连——"
                               "点页头的「检查引擎连接」或这一行的「探测」现查"),
                    "engine_model": "", "endpoint": endpoint}
        checked = engines.probe(adapter.code, timeout=probe_timeout)
        return {**base, "state": checked.state, "detail": checked.detail,
                "engine_model": checked.values.get("model") or "",
                "endpoint": checked.values.get("endpoint") or ""}
    runtime_status = runtime.probe(adapter.code, model)
    return {**base, "state": runtime_status.state, "detail": runtime_status.detail}


# 登记层的运行位置 → 清单层的轴。**这是折回判据的唯一一份。**
#
# 清单与 provider_config 的 CHECK 都只有 local／cloud 两轴（0003），而登记层
# （0035）多了 `remote`——远程自建跑的是同一套参数、同一套运行方式，只是端点
# 在别人的机器上，所以查清单要折回 local。
#
# 2026-11-07：这个折回原来有三份实现——`routers/model_center.py` 建登记时一份、
# `model_center._manifest_axis()` 一份、前端 `models.js` 的 `manifestProviders()`
# 第三份。三份都在回答同一个问题，改一处就漂一次（上一轮 remote 登记在列表里
# 显示"清单未登记"，正是路由折了、读路径没折）。现在收在这里，
# 并通过 `manifest_payload()["axis"]` 下发给界面——界面按数据折，不再抄判据。
MANIFEST_AXIS: dict[str, str] = {"local": "local", "cloud": "cloud", "remote": "local"}


def manifest_axis(backend: str) -> str:
    """登记层取值折回清单轴。不认识的取值原样返回（交给调用方去拒）。"""
    return MANIFEST_AXIS.get(backend, backend)


# 选路策略的**唯一出处**。原来这四行写在 `manifest_payload()` 的函数体里，
# 于是任何想在别处念一句"到底会不会自动切"的代码都只能自己再写一遍字面量——
# 模型中心的路由就这么抄了一份 `"no_auto_fallback": True`，两份一起漂移的可能
# 恰恰存在于这种"看着不像配置、其实是口径"的地方。
POLICY = {
    "local_default_capabilities": ["image", "video", "tts", "music"],
    "video_cloud_must_be_explicit": True,
    "no_auto_fallback": True,
    "note": "云端视频只能逐镜头显式选择；本地失败不会自动转云端。",
}


def manifest_payload() -> dict:
    """给界面和 W07 的唯一清单出口。

    `axis` 是登记层→清单层的折回表（见 `MANIFEST_AXIS`）。下发它是因为界面也要
    回答同一个问题——「这条 remote 登记对应清单里哪一档」。原来是前端自己写
    `backend === "remote" ? "local" : backend`，那是第二份判据：后端哪天多折一档
    （比如把 `ssh` 也折过去），界面会安静地按老口径算，症状是"清单里有、下拉不显示"。
    """
    return {
        "policy": POLICY,
        "axis": MANIFEST_AXIS,
        "adapters": [a.as_dict() for a in ADAPTERS],
    }

__all__ = [
    'annotations',
    'os',
    'hashlib',
    're',
    'dataclass',
    'engines',
    'runtime',
    'Param',
    'Price',
    'ModelSpec',
    'Adapter',
    '_FASTMETAL',
    '_MLX_VIDEO',
    '_MFLUX',
    '_DRAW_THINGS',
    '_OLLAMA',
    '_COMFYUI',
    '_VOXCPM',
    '_ACE_STEP',
    '_DEEPSEEK',
    '_TEXT_CLOUD',
    '_WAN',
    '_VIDEO_CLOUD',
    '_CLOUD_OTHER',
    'ADAPTERS',
    'MANIFEST_VERSION',
    'MOCK_PARAMS',
    'MOCK_MODEL',
    '_INDEX',
    'find',
    'effective_key_env',
    'resolved_key_env',
    'key_present',
    'key_fingerprint',
    '_preview_for_db',
    '_KEY_PREFIX_RE',
    '_type_ok',
    '_check_one',
    'validate_params',
    'validate_mock_params',
    'health',
    'MANIFEST_AXIS',
    'manifest_axis',
    'POLICY',
    'manifest_payload',
    '_a',
    '_name',
]
