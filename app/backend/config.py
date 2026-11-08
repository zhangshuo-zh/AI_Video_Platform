"""集中配置：路径、端口一律走环境变量，仓库里不留死值。

为什么单独一个文件：部署到 Studio 之后改端口/换数据目录只动这里，
不用在业务代码里找常量。

环境变量一律经过 `_env_*` 解析：空串回退默认值、类型不对当场报出变量名。
原来十来处 `int(os.environ.get(...))` 散在模块顶层，写错一个字符就是在
**import 时**抛一句不带变量名的 ValueError；`FASTVIDEO_DB=`（空串）更糟，
会静默变成 `Path(".")`，把库建到当前目录。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # app/backend
FRONTEND_DIR = BASE_DIR.parent / "frontend"         # app/frontend


def _env(name: str, default: str) -> str:
    """读一个字符串变量：没设或只有空白都回默认值。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name} 要一个整数，现在是 {raw!r}") from None


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"环境变量 {name} 要一个数字，现在是 {raw!r}") from None


def _env_bool(name: str, default: bool) -> bool:
    """布尔只认一套口径：0/false/no/off 为假，其余非空为真。

    原来 `ALLOW_MOCK` 用 `== "1"`、`COMPOSE_INTERPOLATE` 用 not in (...)。
    按后者的习惯设 `FASTVIDEO_ALLOW_MOCK=true` 会静默不开 mock，
    表现是"测试用的假适配器怎么都找不到"。
    """
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _env_path(name: str, default: Path) -> Path:
    """路径必须是绝对路径：相对路径会跟着启动目录漂，换个人跑就是另一个库。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    path = Path(str(raw).strip()).expanduser()
    if not path.is_absolute():
        raise SystemExit(f"环境变量 {name} 要绝对路径，现在是 {raw!r}")
    return path


# SQLite 库文件与上传文件都在 backend/data/ 下，方便整体搬走
DB_PATH = _env_path("FASTVIDEO_DB", BASE_DIR / "data" / "platform.db")
FILES_DIR = _env_path("FASTVIDEO_FILES", BASE_DIR / "data" / "files")

# 文档正文的工作区：一个 Git 仓库，小说/剧本的 Markdown 都落在这里。
# 规格 02 §6.1 要求"路径必须限制在配置的根目录内"——根目录就是这一条常量，
# workspace.py 里每一次读写都拿它做边界校验，所以它可以指向已有的旧目录
# （设 FASTVIDEO_WORKSPACE=/path/to/原来的稿子目录 即可，不强制搬迁，规格 02 §2 同口径）。
WORKSPACE_DIR = _env_path("FASTVIDEO_WORKSPACE", BASE_DIR / "data" / "workspace")

# 编辑锁的心跳时长（分钟）。取 15 是"人去倒杯水不算丢锁、真走了锁会自己放开"的折中；
# 保存正文时会顺带续期（规格 02 §6.1「心跳/续租或在保存时延长」），不需要前端定闹钟。
LOCK_MINUTES = _env_int("FASTVIDEO_LOCK_MINUTES", 15)

# 单份正文的字节上限。没有这一条，粘一本三百万字的书进来会把工作区和 Git 都拖垮；
# 一集剧本十万字用不到 1MB，留足余量按 1MB 拦（超了让人分文档，而不是默默截断）。
DOC_MAX_BYTES = _env_int("FASTVIDEO_DOC_MAX_BYTES", 1024 * 1024)

# 生成作业的日志与产物目录：data/jobs/<job_id>/{logs,outputs}。
# 与文件资产（FILES_DIR）分开，删项目时这两处都要清；路径换算统一走 jobs_store.py。
JOBS_DIR = _env_path("FASTVIDEO_JOBS", BASE_DIR / "data" / "jobs")

# 独立 worker 的节拍与租约。租约必须大于"最长一次无心跳的间隔"，
# 否则正常跑着的长任务会被下一轮恢复逻辑误判成"worker 死了"。
WORKER_POLL = _env_float("FASTVIDEO_WORKER_POLL", 1.0)
WORKER_LEASE_SECONDS = _env_int("FASTVIDEO_WORKER_LEASE", 60)
# 心跳超过这个年龄就算离线（顶栏那个小圆点按它亮/灭）
WORKER_ONLINE_SECONDS = _env_int("FASTVIDEO_WORKER_ONLINE", 20)
# 一条作业最长允许占着 worker。租约只证明"worker 还活着"，不证明"模型还在前进"；
# 没有这条上限，子进程挂住时会每 0.2 秒续一次租约，永远停在运行中。
WORKER_MAX_RUNTIME_SECONDS = _env_int("FASTVIDEO_WORKER_MAX_RUNTIME", 7200)
# 队列里有排队项、worker 在线却连续这么久没领到新作业，界面按"停滞"提示。
# 这不改变 Job 状态，只是把"没人干活"和"刚入队"区分开。
QUEUE_STALL_SECONDS = _env_int("FASTVIDEO_QUEUE_STALL_SECONDS", 120)
# 日志文件多久没有字节增长就提示可能卡住。本地模型加载权重时可以静默一阵，
# 默认给 10 分钟；它只是观测阈值，不会自动杀进程（杀进程仍由运行超时负责）。
LOG_SILENCE_SECONDS = _env_int("FASTVIDEO_LOG_SILENCE_SECONDS", 600)

# ---------- 成片中心（阶段 D） ----------
# FFmpeg 由 worker 的子进程执行，绝不在请求线程里跑；路径可换（brew／自编译）。
FFMPEG_BIN = _env("FASTVIDEO_FFMPEG", "ffmpeg")
FFPROBE_BIN = _env("FASTVIDEO_FFPROBE", "ffprobe")
# 默认输出 720p：目标机与局域网预览够用，合成本身是 CPU 重活，1080p 留给显式选择。
COMPOSE_WIDTH = _env_int("FASTVIDEO_COMPOSE_WIDTH", 1280)
COMPOSE_HEIGHT = _env_int("FASTVIDEO_COMPOSE_HEIGHT", 720)
COMPOSE_FPS = _env_int("FASTVIDEO_COMPOSE_FPS", 24)
COMPOSE_CRF = _env_int("FASTVIDEO_COMPOSE_CRF", 20)
COMPOSE_PRESET = _env("FASTVIDEO_COMPOSE_PRESET", "veryfast")
# 防呆：一集几百镜还硬合成会把临时目录与内存拖死；超过就明确拒绝，不静默截断。
COMPOSE_MAX_SHOTS = _env_int("FASTVIDEO_COMPOSE_MAX_SHOTS", 200)

# 导出前的一道 RIFE 补帧（2026-11-05 裁定＝开）。
# 为什么要这道：FastMetal 1.3B 的产物实测是 16fps（`--fps` 默认 16），
# 而成片按 24fps 出，`ffmpeg_runner` 只挂了一个 `-r 24`——那是**复制帧**的
# 3:2 pulldown，动作会一顿一顿。补帧改成按时间轴合成中间帧，画质上是净收益。
# 关掉它请显式设 FASTVIDEO_COMPOSE_INTERPOLATE=0；开着但运行时缺东西时
# 这一道会**直接失败**，不会退回复制帧——"默默换了一种做法"是最难查的那类 bug。
COMPOSE_INTERPOLATE = _env_bool("FASTVIDEO_COMPOSE_INTERPOLATE", True)
RIFE_MODEL_VERSION = _env("FASTVIDEO_RIFE_VERSION", "4.25")
# 生产前检查里"作业目录还剩多少"的提醒线。5GB 是拍的：一集 720p 成片的临时文件
# 加逐镜产物大概在这个量级，但**没有按目标机实测标定过**（《发布基线》§3 挂着这条）。
# 做成可改的环境变量而不是写死在检查里——换机器、换分辨率时调这一档，
# 不用碰业务代码；它只产生一句提醒，不拦发起。
DISK_MIN_FREE_GB = _env_int("FASTVIDEO_DISK_MIN_FREE_GB", 5)

# ---------- 并发与整季（阶段 E） ----------
# 一条 worker 进程能领哪些资源类；默认全接，且全局只跑一条——与旧行为一致。
RESOURCE_CLASSES = ("text", "image", "video", "audio", "compose")
WORKER_MAX_GLOBAL = _env_int("FASTVIDEO_WORKER_MAX_GLOBAL", 1)
# 每类资源的默认上限。实际值要用目标机基准测出来，这里只是安全起点。
WORKER_MAX_PER_RESOURCE = {
    name: _env_int(f"FASTVIDEO_WORKER_MAX_{name.upper()}", 1)
    for name in RESOURCE_CLASSES
}
# 一整季同时最多推进几条单集运行。默认 1＝串行；调高前先看 worker 的并发上限。
SEASON_MAX_ACTIVE_RUNS = _env_int("FASTVIDEO_SEASON_MAX_ACTIVE", 1)
# 单季可设的并发上限（防呆）：再多也不该在一台 16GB 机器上同时推。
SEASON_MAX_ACTIVE_LIMIT = _env_int("FASTVIDEO_SEASON_MAX_ACTIVE_LIMIT", 8)
# 一次整季发起最多包含多少集（防呆，不是业务上限；要更多就分批）。
SEASON_MAX_EPISODES = _env_int("FASTVIDEO_SEASON_MAX_EPISODES", 200)


# 测试/演示用的假适配器（只 sleep、写一个文本结果）。默认关；
# 打开后 API 与 worker 才认识 provider=mock，生产环境不该出现它。
ALLOW_MOCK = _env_bool("FASTVIDEO_ALLOW_MOCK", False)

# 默认只监听本机。**平台早就做了登录与权限**（见 `deps.require_permission`），
# 原来那句"一期还没做登录，整个网段都能建项目"是 0010 之前的事实，早已过期——
# 留着它只会让部署的人以为可以放心把 0.0.0.0 开到公网。真要供局域网使用，
# 除了这里改地址，还应该把 COOKIE_SECURE 与 SESSION_HOURS 一起复核（见下）。
HOST = _env("FASTVIDEO_HOST", "127.0.0.1")
PORT = _env_int("FASTVIDEO_PORT", 8000)

# 登录会话有效期（小时）。到期后要重新登录，库里的行同时失效
SESSION_HOURS = _env_int("FASTVIDEO_SESSION_HOURS", 12)

# ---------- 首个超级用户（`bootstrap.ensure_admin` 用） ----------
# 这三个原来在 `bootstrap.py` 里裸读 `os.environ.get`，是"集中配置"里唯一的
# 漏网处：拿不到 `_env` 的空串回退（`FASTVIDEO_ADMIN_USER="  "` 会建出一个
# 用户名叫空白的账号）与统一的报错口径。
#
# **注意这三个是函数而不是常量**：`ensure_admin()` 只在库为空时跑一次，
# 而部署方（`run.sh`、容器 entrypoint、`FASTVIDEO_ADMIN_PASSWORD=xxx python main.py`）
# 是"在进程里**先设环境变量再拉起应用**"的用法，`test_infra.py` 也按这个口径
# 用 monkeypatch 验。写成 import 期常量会把这条路径锁死成"必须在 import 前设"，
# 那是把可用的部署方式改坏。所以这里保留取值函数、只把**解析规则**收进 config。
#
# `ADMIN_PASSWORD` 是**口令明文**：只在建号那一次用，不进库（库里存 scrypt
# 派生值）、不进日志；没设就由 bootstrap 随机生成。
def admin_username() -> str:
    return _env("FASTVIDEO_ADMIN_USER", "superadmin")


def admin_display() -> str:
    return _env("FASTVIDEO_ADMIN_DISPLAY", "超级管理员")


def admin_password() -> str:
    return _env("FASTVIDEO_ADMIN_PASSWORD", "")

# 会话 cookie 是否要求 HTTPS 传输。默认 0＝局域网明文 http 也能用；
# 一旦把平台挂到 HTTPS/反向代理后面，**必须**设 FASTVIDEO_COOKIE_SECURE=1，
# 否则 cookie 仍会随明文请求发出去（同网段可嗅探后直接接管会话）。
COOKIE_SECURE = _env_bool("FASTVIDEO_COOKIE_SECURE", False)

# 是否对外暴露 /api/docs 与 /api/openapi.json。默认关：这两个页面不用登录就能拿到
# 全部接口清单、参数模型与权限点名称，对"局域网里谁都能打开"的部署是白送的侦察材料。
# 本机调试需要时设 FASTVIDEO_DOCS=1。
DOCS_ENABLED = _env_bool("FASTVIDEO_DOCS", False)

# 登录失败限速：同一个"用户名+来源 IP"连续失败 N 次后锁 M 秒。
# 为什么需要：`/api/auth/login` 是全平台唯一不用登录的入口，没有计数就能一直撞库；
# 初始口令是随机 16 位不怕，但管理员自建的 6 位口令挡不住在线爆破。
LOGIN_MAX_FAILS = _env_int("FASTVIDEO_LOGIN_MAX_FAILS", 5)
LOGIN_LOCK_SECONDS = _env_int("FASTVIDEO_LOGIN_LOCK_SECONDS", 300)

# 前端换 nginx 托管时（与 API 不同源）把来源填进来，逗号分隔；留空＝不开 CORS
CORS_ORIGINS = [o.strip() for o in _env("FASTVIDEO_CORS", "").split(",") if o.strip()]
