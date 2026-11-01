"""FastAPI 入口：只做装配，业务都在 routers/ 里。

前后端分离的边界：
- 后端只管 /api 返回 JSON，不拼一行 HTML；
- 前端是纯静态文件（../frontend），由同一个进程兜底托管，
  以后要换 nginx 直接指过去即可，后端不用动。
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from bootstrap import ensure_admin
from config import CORS_ORIGINS, FILES_DIR, FRONTEND_DIR, HOST, PORT
from gitrepo import GitError
from migrate import migrate
from routers import auth, content, documents, jobs, projects, settings, system, users
from services import ensure_workspace


def create_app() -> FastAPI:
    migrate(verbose=False)          # 启动时把库补到最新，重复执行是安全的
    ensure_admin(verbose=False)     # 一个人都没有时建超级用户
    # 稿件工作区（Markdown + Git）准备好。这一步失败不拦启动：
    # 没装 git、工作区指向了一个只读盘，坏的只是"文档"这一条腿，
    # 项目/资产/分镜照常用；为了这一条让整个平台起不来，人反而不知道是哪坏了。
    try:
        ensure_workspace()
    except GitError as err:
        print(f"[启动] 文档工作区没准备好，创作中心的正文功能不可用：{err}")
    app = FastAPI(title="AI 视频生产平台 API", docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    # 同源部署（本进程托管静态文件）时不需要 CORS；前端搬去 nginx 再开
    if CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(auth.router)
    app.include_router(content.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)
    app.include_router(projects.router)
    app.include_router(settings.router)
    app.include_router(system.router)
    app.include_router(users.router)
    # 前端是单页应用：点侧栏不会重新拉 JS，所以静态文件一律不缓存。
    # 局域网内多传几十 KB 无所谓，让人看到旧界面才是真麻烦。
    @app.middleware("http")
    async def no_store_frontend(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    # 上传的定妆图等文件单独一个前缀，和前端分开
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/files", StaticFiles(directory=FILES_DIR), name="files")
    # 静态前端必须最后挂，否则 /api 会被通配吃掉
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT)
