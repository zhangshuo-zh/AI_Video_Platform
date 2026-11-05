"""FastAPI 入口：只做装配，业务都在 routers/ 里。

前后端分离的边界：
- 后端只管 /api 返回 JSON，不拼一行 HTML；
- 前端是纯静态文件（../frontend），由同一个进程兜底托管，
  以后要换 nginx 直接指过去即可，后端不用动。
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import pipeline
from bootstrap import ensure_admin
from config import CORS_ORIGINS, FILES_DIR, FRONTEND_DIR, HOST, PORT
from gitrepo import GitError
from migrate import migrate
from routers import (audio, auth, compositions, content, dashboard, documents,
                     extraction, files, jobs, model_center as model_center_api,
                     pipeline as pipeline_api, profiles, production as production_api,
                     projects, review, seasons as seasons_api, settings, system,
                     users, workbench)
from services import ensure_workspace


def create_app() -> FastAPI:
    migrate(verbose=False)          # 启动时把库补到最新，重复执行是安全的
    ensure_admin(verbose=False)     # 一个人都没有时建超级用户
    # 内置产线模板按 (name, version) 补齐：已有的不动，只插缺的。
    # 放这里是因为 worker 进程也要有一份同样的模板表，而两边共用同一个库，
    # 谁先起来谁补齐都行——插过一次之后就是空操作。
    pipeline.ensure_builtin_templates()
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
    app.include_router(extraction.router)
    app.include_router(review.router)
    app.include_router(audio.router)
    app.include_router(dashboard.router)
    # 首页工作台：跨模块待办聚合。放在 dashboard 旁边——两个都是只读聚合，
    # 区别是 dashboard 按单个项目算，workbench 按"我看得见的项目"一起算
    app.include_router(workbench.router)
    # 项目生产工作台（0037）：跨模块只读聚合 ＋ 每人一份的当前项目×集上下文。
    # 放在 workbench 之后：两个都是聚合读，但 workbench 是首页那四格待办，
    # production 是"这一集走到哪一步"，路由前缀不冲突（/api/workbench 与 /api/context）。
    app.include_router(production_api.router)
    app.include_router(jobs.router)
    app.include_router(pipeline_api.router)
    app.include_router(compositions.router)
    app.include_router(seasons_api.router)
    app.include_router(profiles.router)
    app.include_router(projects.router)
    # 模型中心排在 settings 之前：两者都挂 system:config，但模型中心是"模型这件事的入口"，
    # settings 只剩密钥与用户，读起来先具体后通用
    app.include_router(model_center_api.router)
    app.include_router(settings.router)
    app.include_router(system.router)
    app.include_router(users.router)
    # 定妆图走带鉴权的路由（原来是 StaticFiles 挂载，不登录就能拿图）。
    # 必须排在兜底的前端挂载之前，否则 /files 会被它吞掉
    app.include_router(files.router)
    # 前端是单页应用：点侧栏不会重新拉 JS，所以静态文件一律不缓存。
    # 局域网内多传几十 KB 无所谓，让人看到旧界面才是真麻烦。
    @app.middleware("http")
    async def no_store_frontend(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    # 目录还是要建出来，但**不再静态挂载**：/files 现在是 routers/files.py
    # 那条带登录与项目归属判定的路由。原来挂 StaticFiles 等于给所有定妆图开了
    # 一个不用登录的后门，上传接口那层 ensure_asset 白做
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    # 静态前端必须最后挂，否则 /api 会被通配吃掉
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT)
