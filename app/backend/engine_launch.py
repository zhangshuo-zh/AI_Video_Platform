"""本机服务型引擎的「启动」动作（2026-11-11 加）。

它要解决的问题很小：运行监控/引擎列表里两台本机服务显示「连不上」，
人得离开页面、去终端或启动台把它拉起来、再回来点探测。既然平台本身
就跑在这台机器上（`run.sh` 默认 `HOST=127.0.0.1`），这个来回可以省掉。

**这不是一个通用的「执行命令」接口**，边界比功能重要：

1. **配方是常量，不是数据。** 命令写死在 `RECIPES` 里，按 `engine_code` 查表。
   绝不接受从请求里传来的可执行路径或参数——`0040_engine_instance.sql:34`
   那句「地址或可执行入口这一格会变成任意命令的注入口（CR-001 明令禁止）」
   是这条模块存在的理由。加一个新引擎要改代码 + 过 review，不能靠改库。
2. **只认回环来源。** 平台支持 `FASTVIDEO_HOST=0.0.0.0` 开放到局域网；
   那种部署下「点一下按钮在你机器上起进程」必须挡住。判法与
   `routers/auth.py` 的登录限速取 client.host 同一套。
3. **先探再起。** 端口已经在答话就直接拒绝，一条命令也不执行——
   重复点击不该起第二个进程（`ollama serve` 会真的去抢 11434）。
4. **起完要复探。** 「进程拉起来了」与「端口答话了」是两件事：
   Draw Things 的 API Server 是应用内开关，默认关；应用起来照样连不上。
   这一层要如实分开报，不能拿 exit code 0 当「能用了」。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import engines

# 启动配方。键是 engines.SERVICE_ENGINES 里的 engine_code，值是固定命令。
#
# 为什么两支形式不同（一支 open -a、一支直接跑可执行文件）：
#   * Draw Things 只有 .app，`open -a` 是唯一入口；而且它有幂等性——
#     应用已经在跑时执行它只是把窗口提到前台，不会起第二个实例。
#   * Ollama 在开发机上常见的是 Homebrew 版（`/opt/homebrew/bin/ollama`），
#     没有 .app 可开，只能 `ollama serve`。它是**非幂等**的（已在跑时
#     再跑一次会抢 11434 报 address in use），所以这一支的重复点击风险
#     由上面第 3 条「先探再起」兜住，而不是靠命令自己。
RECIPES: dict[str, dict] = {
    "draw_things": {
        "argv": ["open", "-a", "Draw Things"],
        "label": "Draw Things 应用",
        # 起完之后需要人做的事：这些不是失败，是产品本身的形状。
        "note": "Draw Things 的 API Server 要在应用内设置里手动开启（默认关）。"
                "应用起来了但端口不答话，通常是这个开关没打开。",
    },
    "ollama": {
        "argv": ["ollama", "serve"],
        "label": "ollama serve",
        "note": "",
    },
}

# 起完之后等多久再复探。本机服务从进程起来到 listen 一般在一两秒内；
# 给 6 秒是因为首次加载模型/大应用冷启动会慢一些，而人在按钮前面等
# 这个量级还能接受。等不到就如实说「起来了但没答话」。
WAIT_SECONDS = 6.0
POLL_EVERY = 0.4

# 允许的客户端来源。`::1` 是 IPv6 回环，macOS 上 127.0.0.1 有时会以它出现。
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", ""})


@dataclass(frozen=True)
class LaunchResult:
    """一次启动尝试的结论。

    `started` 说的是「命令执行了且没报错」，**不是**「服务能用了」——
    后者看 `reachable`。界面必须把这两件事分开说，
    否则 exit code 0 会被读成「好了」。
    """

    ok: bool
    engine_code: str
    started: bool
    reachable: bool
    message: str


def recipe_for(engine_code: str) -> dict | None:
    """这一档有没有启动配方。没有就返回 None——不猜、不兜底执行别的。"""
    return RECIPES.get(engine_code)


def _on_path(argv0: str) -> bool:
    """第一条命令在不在 PATH 上。起之前先判，能给出「没装」这种能动手的提示，
    而不是等 `subprocess` 抛 FileNotFoundError 让界面显示一串栈。"""
    if argv0.startswith("/"):
        return Path(argv0).exists()
    return shutil.which(argv0) is not None


def _already_answering(engine_code: str, endpoint: str) -> bool:
    """端口是不是已经在答话。用与列表同一个探测口径（短超时），
    避免「列表说连不上、启动却认为在跑」这种两套判据打架。"""
    status = engines.probe(engine_code, endpoint, fresh=True,
                           timeout=engines.LIST_TIMEOUT_SECONDS)
    # 字段叫 `state`（engines.EngineStatus），三档 ok/missing/unknown。
    # `unknown` 是「还没登记实例」那一档，不是「连不上」——但它同样说明
    # 这个端点现在没有东西在答话，所以只有 ok 才算已在跑。
    return status.state == "ok"


def launch(engine_code: str, endpoint: str) -> LaunchResult:
    """按配方启动这一档本机引擎，起完复探一次。

    调用方负责**权限与来源**两道校验（见 `routers/model_center.py`）：
    这个函数只做「按配方执行 + 复探」，把鉴权混进来会让它难测也不好复用。
    """
    recipe = recipe_for(engine_code)
    if recipe is None:
        return LaunchResult(False, engine_code, False, False,
                            f"{engine_code} 没有内置启动配方：平台不会执行"
                            "未登记的命令。要支持请改 engine_launch.RECIPES 并过 review。")

    # 第 3 条边界：已经在跑就别再起一个。
    if _already_answering(engine_code, endpoint):
        return LaunchResult(True, engine_code, False, True,
                            "服务已经在答话，没有重复启动。若状态看着不对，"
                            "直接点「探测」复查即可。")

    argv = recipe["argv"]
    if not _on_path(argv[0]):
        return LaunchResult(False, engine_code, False, False,
                            f"找不到 {recipe['label']}（{argv[0]} 不在 PATH 上）。"
                            "请先安装，或手动启动后回来点探测。")

    try:
        # `start_new_session=True`：把子进程放进自己的会话，API 进程重启或
        # 退出时不会顺带把它杀掉——人拉起来的是要长期驻留的服务。
        # stdout/stderr 不接管道：`ollama serve` 会一直往终端写日志，
        # 接 pipe 又没人读，写满缓冲区后进程会卡住（比不启动更糟）。
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError as exc:
        return LaunchResult(False, engine_code, False, False,
                            f"启动 {recipe['label']} 失败：{exc}")

    # 第 4 条边界：起完之后复探，把「起来了」和「答话了」分开报。
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_EVERY)
        if _already_answering(engine_code, endpoint):
            return LaunchResult(True, engine_code, True, True,
                                f"已启动 {recipe['label']}，端口开始答话。")

    note = recipe["note"]
    msg = (f"已启动 {recipe['label']}，但 {WAIT_SECONDS:.0f} 秒内端点没有答话。")
    return LaunchResult(False, engine_code, True, False,
                        msg + ((" " + note) if note else
                               " 应用可能还在冷启动，稍后再点一次探测。"))
