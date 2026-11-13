"""FastAPI 入口：只做装配，业务都在 routers/ 里。

前后端分离的边界：
- 后端只管 /api 返回 JSON，不拼一行 HTML；
- 前端是纯静态文件（../frontend），由同一个进程兜底托管，
  以后要换 nginx 直接指过去即可，后端不用动。
"""
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import errors
import pipeline
from bootstrap import ensure_admin
from config import (CORS_ORIGINS, DOCS_ENABLED, FILES_DIR, FRONTEND_DIR,
                    HOST, PORT)
from gitrepo import GitError
from migrate import migrate
from routers import (agents as agents_api, chatgpt_plan as chatgpt_plan_api, audio, auth, compositions, content,
                     dashboard, documents, extraction, files, jobs,
                     model_center as model_center_api,
                     pipeline as pipeline_api, profiles, production as production_api,
                     projects, review, seasons as seasons_api, settings, system,
                     users, workbench)
from services import ensure_workspace


def create_app() -> FastAPI:
    migrate(verbose=False)          # 启动时把库补到最新，重复执行是安全的
    # 网页上设过的运行时路径并进本进程环境（0051）。
    # **必须在 migrate 之后**：`runtime_path` 表是 0051 建的，读早了会撞
    # "no such table"（`runtime_paths.apply` 会吞掉异常，于是静默不生效——
    # 那比报错更难查）。
    # 环境变量优先：这里只补"环境里没有、网页上设过"的那几项，不覆盖显式配置。
    try:
        import runtime_paths
        written = runtime_paths.apply()
        if written:
            print(f"[启动] 已应用网页上设的运行时路径 {len(written)} 项："
                  + "、".join(written))
    except Exception as exc:
        # 一条路径没应用不该让整个平台起不来：探测会如实回 unknown/missing，
        # 人点开设置页看得见，比启动失败好排查。
        print(f"[启动] 运行时路径没应用上：{type(exc).__name__}: {exc}")
    ensure_admin(verbose=False)     # 一个人都没有时建超级用户
    # 只恢复有持久化意图的导入，不扫描删除历史文件；与其他进程的导入共用写锁。
    try:
        import asset_import_recovery
        recovered = asset_import_recovery.recover()
        if any(recovered.values()):
            print(f"[启动] 图片导入恢复：{recovered}")
    except Exception as exc:
        print(f"[启动] 图片导入恢复暂未完成：{type(exc).__name__}，请运行恢复工具检查")
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
    # 接口文档默认**不开**（2026-11-08，P1）：`/api/docs` 与 `/api/openapi.json`
    # 不用登录就能拿到全部接口清单、参数模型与权限点名称——对"局域网里谁都能
    # 打开"的部署，这是白送的侦察材料。本机调试时设 FASTVIDEO_DOCS=1 打开。
    app = FastAPI(title="AI 视频生产平台 API",
                  docs_url="/api/docs" if DOCS_ENABLED else None,
                  redoc_url="/api/redoc" if DOCS_ENABLED else None,
                  openapi_url="/api/openapi.json" if DOCS_ENABLED else None)
    # ---------- 统一错误出口（2026-11-08） ----------
    #
    # 两套错误形状并存是这一版之前的实况：
    #   业务拒绝  {"detail": "作业不存在"}                    ← detail 是字符串
    #   校验拒绝  {"detail": [{"type":..., "loc":...}]}       ← detail 是数组
    # 前端 `api.js` 的写法是 `new Error(JSON.parse(text).detail)`，数组进去之后
    # 会被 JS 当成 Array.toString()，用户看到的是 **`[object Object]`** ——
    # 缺一个必填项时弹窗里就是这五个字。这不是理论问题，是实际发生的。
    #
    # 所以这里把出口收成一条：detail 恒为**一句人话**，另给稳定的 `code`。
    # 三条纪律写在 `errors.py` 的头注释里，核心是"只加字段、不改既有内容"——
    # 浏览器验收与说明书引用过那些句子，换掉就是另一轮返工。
    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422,
                            content=errors.payload(422, exc.errors()))

    @app.exception_handler(HTTPException)
    async def _on_http_error(request: Request, exc: HTTPException):
        # `exc.detail` 可能是字符串（绝大多数）、dict（少数自己拼结构化信息的），
        # 统一压成字符串；业务侧如果自己要给 code，可以放在 dict 的 `code` 里。
        code = None
        if isinstance(exc.detail, dict):
            code = exc.detail.get("code")
        return JSONResponse(
            status_code=exc.status_code,
            content=errors.payload(exc.status_code, exc.detail, code),
            headers=getattr(exc, "headers", None) or None,
        )

    # 同源部署（本进程托管静态文件）时不需要 CORS；前端搬去 nginx 再开
    if CORS_ORIGINS:
        # `*` 与"按 cookie 认证"不能共存：一旦将来有人加 allow_credentials=True，
        # 浏览器会拒绝这个组合，而更糟的情况是有人把 `*` 当真了，以为已经限好来源。
        # 这里在启动期就把它拒掉，让配置错误当场可见，而不是等浏览器里出现怪现象。
        if "*" in CORS_ORIGINS:
            raise SystemExit(
                "FASTVIDEO_CORS 不能是 *：这个平台用 cookie 认证，通配来源等于"
                "允许任意站点带着登录态访问。请写明确的来源（如 http://192.168.1.9:8000）")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            # 显式关掉：登录态走 cookie，若允许跨源携带凭据，来源白名单就是唯一
            # 的防线，而它很容易被配宽。同源部署用不到这一条。
            allow_credentials=False,
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
    # 智能体中心排在模型中心之后：它读模型中心那三张表（配置、实例、策略），
    # 自己一行配置都不存。签字入口仍然在审核中心（`routers/review.py`），
    # 这里只有"看"和"发起／暂停／取消"
    app.include_router(agents_api.router)
    app.include_router(chatgpt_plan_api.router)
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

    # worker 守护线程（0049）：**默认不守**，只有 `data/run/daemon.json` 里
    # `enabled` 为真时才真去补进程。这里无条件起线程是因为"能开关"本身要生效：
    # 线程空转一轮只读一个小 JSON，代价远小于"改了开关要重启 API"。
    # 守护的边界见 `worker_daemon` 模块头：**API 挂了它也就停了**。
    try:
        import worker_daemon
        worker_daemon.start_thread()
    except Exception:
        # 一句守护起不来不该让整个 API 起不来——它是增强项，不是必需项。
        pass
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=HOST, port=PORT)
