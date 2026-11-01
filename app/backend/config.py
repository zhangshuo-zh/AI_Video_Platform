"""集中配置：路径、端口一律走环境变量，仓库里不留死值。

为什么单独一个文件：部署到 Studio 之后改端口/换数据目录只动这里，
不用在业务代码里找常量。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # app/backend
FRONTEND_DIR = BASE_DIR.parent / "frontend"         # app/frontend

# SQLite 库文件与上传文件都在 backend/data/ 下，方便整体搬走
DB_PATH = Path(os.environ.get("FASTVIDEO_DB", BASE_DIR / "data" / "platform.db"))
FILES_DIR = Path(os.environ.get("FASTVIDEO_FILES", BASE_DIR / "data" / "files"))

# 文档正文的工作区：一个 Git 仓库，小说/剧本的 Markdown 都落在这里。
# 规格 02 §6.1 要求"路径必须限制在配置的根目录内"——根目录就是这一条常量，
# workspace.py 里每一次读写都拿它做边界校验，所以它可以指向已有的旧目录
# （设 FASTVIDEO_WORKSPACE=/path/to/原来的稿子目录 即可，不强制搬迁，规格 02 §2 同口径）。
WORKSPACE_DIR = Path(os.environ.get("FASTVIDEO_WORKSPACE", BASE_DIR / "data" / "workspace"))

# 编辑锁的心跳时长（分钟）。取 15 是"人去倒杯水不算丢锁、真走了锁会自己放开"的折中；
# 保存正文时会顺带续期（规格 02 §6.1「心跳/续租或在保存时延长」），不需要前端定闹钟。
LOCK_MINUTES = int(os.environ.get("FASTVIDEO_LOCK_MINUTES", "15"))

# 单份正文的字节上限。没有这一条，粘一本三百万字的书进来会把工作区和 Git 都拖垮；
# 一集剧本十万字用不到 1MB，留足余量按 1MB 拦（超了让人分文档，而不是默默截断）。
DOC_MAX_BYTES = int(os.environ.get("FASTVIDEO_DOC_MAX_BYTES", str(1024 * 1024)))

# 生成作业的日志与产物目录：data/jobs/<job_id>/{logs,outputs}。
# 与文件资产（FILES_DIR）分开，删项目时这两处都要清；路径换算统一走 jobs_store.py。
JOBS_DIR = Path(os.environ.get("FASTVIDEO_JOBS", BASE_DIR / "data" / "jobs"))

# 独立 worker 的节拍与租约。租约必须大于"最长一次无心跳的间隔"，
# 否则正常跑着的长任务会被下一轮恢复逻辑误判成"worker 死了"。
WORKER_POLL = float(os.environ.get("FASTVIDEO_WORKER_POLL", "1.0"))
WORKER_LEASE_SECONDS = int(os.environ.get("FASTVIDEO_WORKER_LEASE", "60"))
# 心跳超过这个年龄就算离线（顶栏那个小圆点按它亮/灭）
WORKER_ONLINE_SECONDS = int(os.environ.get("FASTVIDEO_WORKER_ONLINE", "20"))
# 一条作业最长允许占着 worker。租约只证明"worker 还活着"，不证明"模型还在前进"；
# 没有这条上限，子进程挂住时会每 0.2 秒续一次租约，永远停在运行中。
WORKER_MAX_RUNTIME_SECONDS = int(os.environ.get("FASTVIDEO_WORKER_MAX_RUNTIME", "7200"))
# 队列里有排队项、worker 在线却连续这么久没领到新作业，界面按"停滞"提示。
# 这不改变 Job 状态，只是把"没人干活"和"刚入队"区分开。
QUEUE_STALL_SECONDS = int(os.environ.get("FASTVIDEO_QUEUE_STALL_SECONDS", "120"))
# 日志文件多久没有字节增长就提示可能卡住。本地模型加载权重时可以静默一阵，
# 默认给 10 分钟；它只是观测阈值，不会自动杀进程（杀进程仍由运行超时负责）。
LOG_SILENCE_SECONDS = int(os.environ.get("FASTVIDEO_LOG_SILENCE_SECONDS", "600"))

# 测试/演示用的假适配器（只 sleep、写一个文本结果）。默认关；
# 打开后 API 与 worker 才认识 provider=mock，生产环境不该出现它。
ALLOW_MOCK = os.environ.get("FASTVIDEO_ALLOW_MOCK") == "1"

# 默认只监听本机；平台要供局域网用时设 FASTVIDEO_HOST=0.0.0.0
# （一期还没做登录，整个网段都能建项目，别提前暴露）
HOST = os.environ.get("FASTVIDEO_HOST", "127.0.0.1")
PORT = int(os.environ.get("FASTVIDEO_PORT", "8000"))

# 登录会话有效期（小时）。到期后要重新登录，库里的行同时失效
SESSION_HOURS = int(os.environ.get("FASTVIDEO_SESSION_HOURS", "12"))

# 前端换 nginx 托管时（与 API 不同源）把来源填进来，逗号分隔；留空＝不开 CORS
CORS_ORIGINS = [o.strip() for o in os.environ.get("FASTVIDEO_CORS", "").split(",") if o.strip()]
