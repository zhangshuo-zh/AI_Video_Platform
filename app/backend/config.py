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

# 默认只监听本机；平台要供局域网用时设 FASTVIDEO_HOST=0.0.0.0
# （一期还没做登录，整个网段都能建项目，别提前暴露）
HOST = os.environ.get("FASTVIDEO_HOST", "127.0.0.1")
PORT = int(os.environ.get("FASTVIDEO_PORT", "8000"))

# 登录会话有效期（小时）。到期后要重新登录，库里的行同时失效
SESSION_HOURS = int(os.environ.get("FASTVIDEO_SESSION_HOURS", "12"))

# 前端换 nginx 托管时（与 API 不同源）把来源填进来，逗号分隔；留空＝不开 CORS
CORS_ORIGINS = [o.strip() for o in os.environ.get("FASTVIDEO_CORS", "").split(",") if o.strip()]
