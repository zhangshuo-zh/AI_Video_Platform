"""43 条路由的守卫回归（W01-a 的主菜）。

为什么值得写成表驱动：守卫这件事最怕的是"新加一条端点忘了挂"。
手写一条条测试会在加端点时漏掉，而这张表 + `test_路由表没有偷偷变多`
那条断言会在漏挂时立刻红。

两个视角：
- **匿名**：除 `/api/health` 与四条 auth 之外，一律 401。
- **越权**：用 outsider（editor 角色，但不是那个项目的协作人）打每一条——
  项目级的东西必须 **404**（不可见，不透露存在），账号/系统级的东西必须 **403**
  （editor 没有 `user:*` / `system:config`），能看见的列表类必须 200。
"""
import re

import pytest

from db import get_conn

# (方法, 路径, 需要的最小 body, outsider 期望状态码)
# 路径里的 {p} {e} {s} {a} {u} {r} {d} {c} 由 fixture 填真实 id
ROUTES = [
    # ---------- 项目 ----------
    ("GET", "/api/projects", None, 200),
    ("POST", "/api/projects", {"name": "越权测试项目"}, 201),
    ("PUT", "/api/projects/{p}", {"name": "改名", "description": "", "tags": []}, 404),
    # editor 没有 project:delete ⇒ 先撞权限点那一层（403），轮不到"看不见"（404）。
    # 顺序是规格 02 §5.2 定的：会话 → 权限点 → 归属。
    ("DELETE", "/api/projects/{p}?confirm=x", None, 403),
    ("POST", "/api/projects/{p}/open", None, 404),
    ("PATCH", "/api/projects/{p}/status", {"status": "archived"}, 404),
    ("PUT", "/api/projects/{p}/tags", {"tags": ["t"]}, 404),
    ("GET", "/api/projects/{p}/summary", None, 404),
    ("POST", "/api/projects/batch", {"ids": ["{p}"], "action": "archive"}, 200),
    ("POST", "/api/projects/batch-delete", {"ids": ["{p}"], "confirm": "批量删除"}, 403),
    ("GET", "/api/projects/{p}/members", None, 404),
    ("POST", "/api/projects/{p}/members", {"user_id": 1}, 404),
    ("POST", "/api/projects/{p}/transfer", {"user_id": 1}, 404),
    ("DELETE", "/api/projects/{p}/members/{u}", None, 404),
    ("GET", "/api/member-candidates", None, 200),
    ("GET", "/api/tags", None, 200),
    # 定妆图原来是 StaticFiles 挂载（不登录就能取），现在走带鉴权的路由：
    # 匿名 401、看不见那个资产的人 404。路径参数是 `:path`，整段算一个占位符
    ("GET", "/files/{af}", None, 404),
    # ---------- 创作单元 / 分镜 ----------
    ("GET", "/api/projects/{p}/episodes", None, 404),
    ("POST", "/api/projects/{p}/episodes", {"title": "第 9 集"}, 404),
    ("PATCH", "/api/episodes/{e}/status", {"status": "done"}, 404),
    ("DELETE", "/api/episodes/{e}", None, 404),
    ("GET", "/api/episodes/{e}/shots", None, 404),
    ("POST", "/api/episodes/{e}/shots", {"summary": "一镜"}, 404),
    ("PUT", "/api/shots/{s}",
     {"summary": "越权改一镜", "prompt": "", "duration": 5, "asset_ids": []}, 404),
    ("DELETE", "/api/shots/{s}", None, 404),
    # ---------- 资产 ----------
    ("GET", "/api/projects/{p}/assets", None, 404),
    ("POST", "/api/projects/{p}/assets", {"kind": "character", "name": "越权角色"}, 404),
    ("GET", "/api/assets/{a}", None, 404),
    ("PUT", "/api/assets/{a}", {"kind": "character", "name": "改名"}, 404),
    ("PUT", "/api/assets/{a}/cover", {"path": "1/x.png"}, 404),
    ("GET", "/api/assets/{a}/shots", None, 404),
    ("DELETE", "/api/assets/{a}", None, 404),
    ("POST", "/api/assets/{a}/images", "FILE", 404),
    ("DELETE", "/api/assets/{a}/images?path=x", None, 404),
    # ---------- 资产增强（0033：人物档案、命名形态、生成与产物导入） ----------
    # 全部 404：这些接口都挂在"某条资产"下面，看不见那条资产就不该知道它有没有档案。
    # 形态那两条用 {v}=0（不存在的形态）——先撞"形态不存在"，同样是 404，
    # "形态存在但父资产不可见"那一档在 test_asset_studio.py 里单独验。
    ("GET", "/api/assets/{a}/character-profile", None, 404),
    ("PUT", "/api/assets/{a}/character-profile",
     {"biography": "越权改档案"}, 404),
    ("GET", "/api/assets/{a}/variants", None, 404),
    ("POST", "/api/assets/{a}/variants", {"variant_type": "outfit", "name": "越权形态"}, 404),
    ("PUT", "/api/asset-variants/{v}",
     {"variant_type": "outfit", "name": "改名", "prompt": "", "sort_order": 0}, 404),
    ("DELETE", "/api/asset-variants/{v}", None, 404),
    # v0.99：形态的图也要有删除出口（0033 只有"删整个形态"，导错一张就得全删）。
    # 同样先撞"形态不存在"＝404，不因为多了个 query 参数就变成另一套显隐规则。
    ("DELETE", "/api/asset-variants/{v}/images?path=x", None, 404),
    ("POST", "/api/projects/{p}/asset-generations",
     {"operation": "asset_image", "image_prompt": "越权提交"}, 404),
    ("GET", "/api/projects/{p}/asset-generations", None, 404),
    ("GET", "/api/projects/{p}/asset-generations/{j}", None, 404),
    ("POST", "/api/assets/{a}/generated-images", {"job_id": "{j}", "artifact_index": 0}, 404),
    # ---------- 文档（W05：元数据在库、正文在工作区、历史在 Git） ----------
    # 全部 404：文档一定挂项目，别人看不见这个项目时连"这份文档存在"都不该知道。
    # 这里没有 403 的余地——资产才有全局池那档特例（见 access.ensure_asset）。
    ("GET", "/api/projects/{p}/documents", None, 404),
    ("POST", "/api/projects/{p}/documents", {"kind": "novel", "title": "越权稿"}, 404),
    ("GET", "/api/documents/{d}", None, 404),
    ("PUT", "/api/documents/{d}", {"title": "改名"}, 404),
    ("PATCH", "/api/documents/{d}/status", {"status": "polishing"}, 404),
    ("DELETE", "/api/documents/{d}", None, 404),
    ("GET", "/api/documents/{d}/content", None, 404),
    ("PUT", "/api/documents/{d}/content",
     {"content": "抢写", "expected_version": "sha256:不存在的那个版本"}, 404),
    ("POST", "/api/documents/{d}/lock", None, 404),
    ("DELETE", "/api/documents/{d}/lock", None, 404),
    ("GET", "/api/documents/{d}/versions", None, 404),
    ("GET", "/api/documents/{d}/versions/{c}/diff", None, 404),
    # ---------- 文档 AI 操作（W11） ----------
    # 同一口径：文档挂项目，看不见项目就连"这份文档能跑 AI"也不该知道。
    # 计价那一条用 GET：只是数一遍选中多少字，不花钱、不写库。
    ("GET", "/api/documents/{d}/ai-options?operation=polish", None, 404),
    ("POST", "/api/documents/{d}/ai-jobs",
     {"operation": "polish", "instruction": "", "scope": "document",
      "start": 0, "end": 0, "selection_text": "",
      "estimate": {"quantity": 0.01, "unit": "million_token",
                   "unit_price_micros": 8000000, "currency": "CNY",
                   "source": "越权探测"},
      "user_confirmed_cloud": True}, 404),
    ("GET", "/api/documents/{d}/ai-jobs/{j}", None, 404),
    ("POST", "/api/documents/{d}/ai-jobs/{j}/apply", None, 404),
    # ---------- 文本抽取候选（W13） ----------
    # 文档挂项目：看不见项目就连"这份稿能抽"也不该知道。运行与候选用不存在的 id。
    ("GET", "/api/projects/{p}/extract/options?doc_id={d}", None, 404),
    ("POST", "/api/projects/{p}/extract/runs",
     {"project_id": "{p}", "doc_id": "{d}",
      "estimate": {"quantity": 0.011, "unit": "million_token",
                   "unit_price_micros": 8000000, "currency": "CNY",
                   "source": "越权探测"},
      "user_confirmed_cloud": True}, 404),
    ("GET", "/api/projects/{p}/extract/runs", None, 404),
    ("GET", "/api/extract/runs/{x}", None, 404),
    ("GET", "/api/extract/runs/{x}/items", None, 404),
    ("POST", "/api/extract/items/{t}/accept", {}, 404),
    ("POST", "/api/extract/items/{t}/merge", {"asset_id": "{a}"}, 404),
    ("POST", "/api/extract/items/{t}/reject", {"reason": "x"}, 404),
    ("GET", "/api/extract/runs/{x}/shots.md", None, 404),
    # ---------- 审核中心（W14） ----------
    ("GET", "/api/projects/{p}/reviews", None, 404),
    ("GET", "/api/documents/{d}/review-gate", None, 404),
    ("POST", "/api/documents/{d}/review-gate", None, 404),
    ("POST", "/api/documents/{d}/review-sign",
     {"anchor": "document", "decision": "approved"}, 404),
    # 产物审核（0025／裁定 D1）：审的是一条已成功作业的产物，签字仍只此一个入口
    ("GET", "/api/projects/{p}/job-reviews", None, 404),
    ("GET", "/api/jobs/{j}/review", None, 404),
    ("POST", "/api/jobs/{j}/review-sign",
     {"anchor": "artifact", "decision": "approved"}, 404),
    # ---------- 音频中心（W15） ----------
    ("GET", "/api/projects/{p}/audio-clips", None, 404),
    ("POST", "/api/projects/{p}/audio-clips",
     {"episode_id": "{e}", "shot_id": "{s}", "kind": "dialogue", "text": "越权台词"}, 404),
    ("PATCH", "/api/audio-clips/{q}",
     {"kind": "dialogue", "speaker": "", "text": "改", "notes": ""}, 404),
    ("POST", "/api/audio-clips/{q}/submit", None, 404),
    ("DELETE", "/api/audio-clips/{q}", None, 404),
    # ---------- 项目看板与导出（W16） ----------
    ("GET", "/api/projects/{p}/dashboard", None, 404),
    ("GET", "/api/projects/{p}/export/shots.csv", None, 404),
    ("GET", "/api/projects/{p}/export/shots.md", None, 404),
    # 首页工作台跨项目聚合：editor 有 project:read 就能调，但里面只会出现
    # 他看得见的项目（别人的项目在服务端就筛掉了，所以是 200 而不是 404）
    ("GET", "/api/workbench", None, 200),
    # ---------- 项目生产工作台（0037） ----------
    # /api/context 存的是"这个人上次站在哪一集"：登录就能读写自己那一份，
    # 所以 outsider 也是 200——它不暴露别人的东西，也不跨项目给数字。
    ("GET", "/api/context", None, 200),
    ("PUT", "/api/context", {"project_id": None, "episode_id": None}, 200),
    # 一集的生产进度里有镜头数与作业数，必须先过项目可见性那道门
    ("GET", "/api/projects/{p}/production", None, 404),
    # ---------- 统一预检（0037） ----------
    # 三个预检都是只读，但读的是别人项目的镜头/集，所以一律 404
    ("GET", "/api/audio/capabilities", None, 200),
    ("GET", "/api/jobs/{j}/diagnosis", None, 404),
    ("GET", "/api/jobs/{j}/review-checklist", None, 404),
    ("POST", "/api/jobs/{j}/review-checklist", {"items": []}, 404),
    ("POST", "/api/jobs/review-batch", {"items": [{"job_id": 1}], "decision": "rejected"}, 404),
    ("POST", "/api/jobs/retry-batch", {"episode_id": 1}, 404),

    ("GET", "/api/precheck/shot/{s}", None, 404),
    ("GET", "/api/projects/{p}/precheck?episode_id={e}", None, 404),
    ("GET", "/api/projects/{p}/composition-precheck?episode_id={e}", None, 404),
    # 单镜头工作台读的是这一镜的作业与签字，影响分析读的是跨项目引用清单
    # ——两条都先过可见性，看不见就是 404
    ("GET", "/api/shots/{s}/workbench", None, 404),
    ("GET", "/api/assets/{a}/impact", None, 404),
    # ---------- 账号与角色（editor 一律 403） ----------
    ("GET", "/api/users", None, 403),
    ("POST", "/api/users", {"username": "zz_probe", "display_name": "", "password": "123456"}, 403),
    ("PATCH", "/api/users/{u}", {"display_name": "改"}, 403),
    ("PUT", "/api/users/{u}/roles", {"roles": ["viewer"]}, 403),
    ("POST", "/api/users/{u}/password", {"new_password": "abcdef"}, 403),
    ("DELETE", "/api/users/{u}", {"transfer_to": None, "delete_projects": False}, 403),
    ("GET", "/api/roles", None, 403),
    ("GET", "/api/permissions", None, 403),
    ("PUT", "/api/roles/{r}/permissions", {"permissions": []}, 403),
    # ---------- 生成作业（W07） ----------
    # 作业一定挂项目：看不见项目就一律 404（连"这条作业存在"都不透露）
    ("POST", "/api/jobs", {"project_id": "{p}", "capability": "video",
                           "prompt": "越权作业", "provider_code": "mock",
                           "model_code": "mock-1", "params": {}}, 404),
    ("GET", "/api/jobs", None, 200),
    ("GET", "/api/jobs/{j}", None, 404),
    ("POST", "/api/jobs/{j}/cancel", None, 404),
    ("POST", "/api/jobs/{j}/retry", None, 404),
    ("GET", "/api/jobs/{j}/logs", None, 404),
    ("GET", "/api/jobs/{j}/artifacts", None, 404),
    ("GET", "/api/jobs/{j}/artifacts/{i}", None, 404),
    # P0：重试预览只读、人工核对与待核对都是写，但都先撞 _load 的归属 ⇒ 404
    ("GET", "/api/jobs/{j}/retry-preview", None, 404),
    ("POST", "/api/jobs/{j}/reconcile", {"decision": "pending"}, 404),
    ("POST", "/api/jobs/{j}/attention", {"reason": "x"}, 404),
    # 成本账按可见项目汇总：看不见那个项目时就是空账，不是 404
    ("GET", "/api/jobs/costs", None, 200),
    ("GET", "/api/worker/status", None, 200),
    # 0047：并发设置。读是任何登录者都行（客观状态），写要 system:config ⇒ 403。
    ("GET", "/api/worker/concurrency", None, 200),
    ("PUT", "/api/worker/concurrency", {"global_max": 2}, 403),
    # 0048：worker 进程启停。三个都要 system:config ⇒ editor 一律 403。
    ("GET", "/api/worker/process", None, 403),
    ("POST", "/api/worker/process/start", {}, 403),
    ("POST", "/api/worker/process/stop", {}, 403),
    # 0049：多开、守护与通知。启停守护要 system:config（403）；
    # 通知是"我的收件箱"，任何登录者 200。
    ("POST", "/api/worker/process/start-many", {"count": 1}, 403),
    ("POST", "/api/worker/process/stop-one", {"pid": 1}, 403),
    ("GET", "/api/worker/daemon", None, 403),
    ("PUT", "/api/worker/daemon", {"enabled": False, "desired": 1}, 403),
    ("GET", "/api/notifications", None, 200),
    ("POST", "/api/notifications/read", {"ids": []}, 200),
    # ---------- 自动化漫剧产线（0026／阶段 B） ----------
    # 模板是全局的、只读，有 job:read 就能看 ⇒ editor（outsider）200。
    # 运行／节点一定挂项目：看不见那个项目就 404，连"这条运行存在"都不透露。
    # 权限点一个新都没加，所以这里不会出现 403 的那一档。
    ("GET", "/api/pipeline/templates", None, 200),
    ("GET", "/api/pipeline/templates/{x}", None, 404),
    ("POST", "/api/projects/{p}/pipeline/runs",
     {"episode_id": "{e}", "template_id": 1, "idempotency_key": "越权探测"}, 404),
    ("GET", "/api/projects/{p}/pipeline/runs", None, 404),
    # 发起前的生产前检查：只读，但只对看得见的项目开放（别人的项目 404）
    ("GET", "/api/projects/{p}/preflight?episode_id={e}", None, 404),
    ("GET", "/api/pipeline/runs/{x}", None, 404),
    ("POST", "/api/pipeline/runs/{x}/advance", None, 404),
    ("POST", "/api/pipeline/runs/{x}/pause", None, 404),
    ("POST", "/api/pipeline/runs/{x}/resume", None, 404),
    ("POST", "/api/pipeline/runs/{x}/cancel", {"reason": "越权探测"}, 404),
    ("GET", "/api/projects/{p}/compositions", None, 404),
    ("POST", "/api/projects/{p}/compositions",
     {"episode_id": 1}, 404),
    ("GET", "/api/compositions/{x}", None, 404),
    # 字幕：文件不进库，但同样要登录、要看得见项目
    ("GET", "/api/projects/{p}/composition-subtitles", None, 404),
    ("POST", "/api/projects/{p}/composition-subtitles", "FILE", 404),
    ("DELETE", "/api/projects/{p}/composition-subtitles?path=x", None, 404),
    # ---------- 整季生产（阶段 E） ----------
    ("GET", "/api/projects/{p}/seasons", None, 404),
    ("POST", "/api/projects/{p}/seasons", {"template_id": 1}, 404),
    ("GET", "/api/seasons/{s}", None, 404),
    ("POST", "/api/seasons/{s}/pause", None, 404),
    ("POST", "/api/seasons/{s}/resume", None, 404),
    ("POST", "/api/seasons/{s}/cancel", {"reason": "越权探测"}, 404),
    ("POST", "/api/seasons/{s}/retry-failed", None, 404),
    ("POST", "/api/pipeline/nodes/{x}/retry", None, 404),
    # ---------- 参数模板（W08） ----------
    ("GET", "/api/generation-profiles", None, 200),
    ("POST", "/api/generation-profiles", {"project_id": "{p}", "name": "越权模板",
                                          "capability": "video"}, 404),
    # 全局模板只有管理员能建：editor 的 job:submit 过了，也在这里被挡
    ("POST", "/api/generation-profiles", {"name": "全局越权", "capability": "video"}, 403),
    ("PUT", "/api/generation-profiles/{g}", {"name": "改名", "params": {},
                                             "is_default": False}, 404),
    ("DELETE", "/api/generation-profiles/{g}", None, 404),
    # ---------- Provider（editor 没有 system:config） ----------
    ("GET", "/api/providers", None, 403),
    ("GET", "/api/providers/manifest", None, 403),
    # 当前默认是给普通模块看的只读摘要：有会话就能读，不带密钥/备注
    ("GET", "/api/providers/current", None, 200),
    # 提交表单要用：只回可用的槽位与参数白名单，不带密钥变量名/备注
    ("GET", "/api/providers/available", None, 200),
    ("PUT", "/api/providers", [{"id": 1, "provider": "x", "model": "",
                                "enabled": False, "is_default": True}], 403),
    # P0：验证历史与两条验证写入都要 system:config，editor 一律 403。
    # validate 只收 env_check；validate-job 反过来不收 env_check（要带 job_id）。
    ("GET", "/api/providers/validations", None, 403),
    ("POST", "/api/providers/validate",
     {"capability": "text", "backend": "local", "kind": "env_check"}, 403),
    ("POST", "/api/providers/validate-job/{j}",
     {"capability": "text", "backend": "local", "kind": "real_call", "job_id": "{j}"}, 403),
    # ---------- 模型中心（0035；editor 没有 system:config ⇒ 403） ----------
    # resolve 与 /providers/available 同一档：有会话就能读候选，所以是 200。
    # {m}=0 是不存在的登记行——外人先撞权限点那一层（会话→权限点→归属），
    # 轮不到"这条不存在"，所以期望仍是 403。
    ("GET", "/api/models", None, 403),
    ("GET", "/api/models/stats", None, 403),
    # 现查那一条同样要 system:config：它会真的去连本机服务，
    # 不是"打开页面顺带读一下"那种只读接口。
    ("POST", "/api/models/probe", None, 403),
    ("POST", "/api/models",
     {"capability": "video", "backend": "local", "provider": "fastmetal",
      "model_code": "fastmetal-5b-qad"}, 403),
    ("PATCH", "/api/models/{m}", {"display_name": "越权改名"}, 403),
    ("DELETE", "/api/models/{m}?confirm=1", None, 403),
    ("POST", "/api/models/{m}/check", None, 403),
    ("GET", "/api/model-policies", None, 403),
    ("PUT", "/api/model-policies",
     [{"scene_key": "character_bio", "capability": "text"}], 403),
    ("GET", "/api/model-policies/resolve/{sk}", None, 200),
    # 场景骨架与 resolve 同一档：普通编辑者要能在提交框里列出场景。
    # 这一条下发的是"有哪些场景、各要什么能力"，不含建议轨／备用轨——那些仍要 system:config。
    ("GET", "/api/model-policies/scenes", None, 200),
    ("GET", "/api/config-changes", None, 403),
    # ---------- 引擎实例（0040／W-B；同样全部 system:config） ----------
    # 这五条是"这一档引擎在哪个端点"的管理面。探测与撤除都不动应用与历史作业，
    # 但都能改配置，所以外人一律 403；{m}=0 那条不存在的实例也是先撞权限点。
    ("GET", "/api/engines", None, 403),
    ("POST", "/api/engines",
     {"engine_code": "draw_things", "endpoint": "http://127.0.0.1:7860"}, 403),
    ("PATCH", "/api/engines/{m}", {"note": "越权改"}, 403),
    ("DELETE", "/api/engines/{m}?confirm=1", None, 403),
    # ---------- 本地运行时路径（0051） ----------
    # 两条都 system:config：路径决定 worker 去哪里找权重，改错了整条生成腿就停摆。
    # 普通人（含制作）看都不该看到这台机器的目录结构。
    ("GET", "/api/runtime-paths", None, 403),
    ("PUT", "/api/runtime-paths/{m}", {"path": "/tmp/evil"}, 403),
    # ---------- 云端密钥的指纹与连通性（2026-11-11） ----------
    # 两条都 system:config。指纹推不回密钥值，但它能让拿到泄漏 key 的人确认
    # "是不是这一把"，且露出的家族标记也算信息；测试连接是**真出网**的调用。
    # 两条都不该让普通人碰到。
    ("GET", "/api/secrets", None, 403),
    ("POST", "/api/secrets/{m}/test", None, 403),
    # ---------- 密钥的密文存储（0052，2026-11-11） ----------
    # 这三条能**改真实密钥**，是整份权限表里最敏感的三条。普通人 403 是底线；
    # 管理员能看到指纹但看不到明文（明文只在一个 PUT 入参里存在一次）。
    ("GET", "/api/secret-values", None, 403),
    ("PUT", "/api/secret-values/{m}", {"value": "sk-evil"}, 403),
    ("DELETE", "/api/secret-values/{m}", None, 403),
    ("POST", "/api/engines/{m}/probe", None, 403),
    # 启动/关闭本机引擎：越权者同样只该看到 403，轮不到"这条实例存不存在"。
    # 真正会在回环来源下执行命令的那一路，由 tests/test_engine_launch.py 覆盖。
    ("POST", "/api/engines/{m}/launch", None, 403),
    ("POST", "/api/engines/{m}/stop", None, 403),
    # 探测历史（0042）：只读，但仍要 system:config——detail 里有端点和内网地址。
    ("GET", "/api/engines/{m}/probes", None, 403),
    # ---------- 智能体中心（0041） ----------
    # 三条配置面（概览／配置卡／工具表）都是"平台怎么装的"，沿用 system:config ⇒ 403。
    # 其余按业务点走：发起＝job:submit、暂停恢复取消＝job:cancel、
    # 审批队列＝review:read、签字＝review:sign——**一个新权限点都没加**。
    # outsider 有 job:submit／review:read，所以会一路走到归属判定，拿 404。
    # ChatGPT 连接属于当前登录用户，草案还必须通过项目可见性检查。
    ("GET", "/api/agent/chatgpt/usage", None, 200),
    ("GET", "/api/agent/chatgpt/connection", None, 200),
    ("POST", "/api/agent/chatgpt/authorize", {"new_account": False}, 200),
    ("DELETE", "/api/agent/chatgpt/connection", None, 200),
    ("GET", "/api/agent/chatgpt/models", None, 422),
    ("POST", "/api/agent/chatgpt/drafts", {"request_id": "{cg}", "project_id": "{p}",
      "goal": "越权的制作目标", "model": "test", "confirmed": True}, 404),
    ("GET", "/api/agent/chatgpt/drafts/{cg}", None, 404),
    ("POST", "/api/agent/chatgpt/drafts/{cg}/accept", {}, 404),
    ("GET", "/api/agent/overview", None, 403),
    ("GET", "/api/agent/configs", None, 403),
    ("GET", "/api/agent/tools", None, 403),
    ("GET", "/api/agent/plan-options?project_id={p}", None, 404),
    ("POST", "/api/agent/runs",
     {"project_id": "{p}", "goal": "把这一集缺的资产补齐", "scene_key": ""}, 404),
    ("GET", "/api/agent/runs?project_id={p}", None, 404),
    ("GET", "/api/agent/runs/{x}", None, 404),
    ("POST", "/api/agent/runs/{x}/pause", None, 404),
    ("POST", "/api/agent/runs/{x}/resume", None, 404),
    ("POST", "/api/agent/runs/{x}/cancel", None, 404),
    ("GET", "/api/agent/pending", None, 200),
    ("GET", "/api/projects/{p}/agent-action-reviews", None, 404),
    ("GET", "/api/agent-steps/{x}/review", None, 404),
    ("POST", "/api/agent-steps/{x}/review-sign",
     {"anchor": "execute", "decision": "approved", "note": "越权签"}, 404),
]

# auth 四条与 health 不参与"匿名 401"这张表，单独测
EXCLUDED = {"/api/health", "/api/auth/login", "/api/auth/logout",
            "/api/auth/me", "/api/auth/password"}


def _fill(path: str, ids: dict) -> str:
    out = path
    for k, v in ids.items():
        out = out.replace("{" + k + "}", str(v))
    return out


@pytest.fixture
def ids(seeded, people):
    """表里那些占位符对应的真实 id。角色 id 现查，不写死 1/2/3。"""
    with get_conn() as conn:
        role_id = conn.execute("SELECT id FROM role WHERE code = 'viewer'").fetchone()["id"]
    return {"cg": "00000000-0000-0000-0000-000000000001", "p": seeded["project_id"], "e": seeded["episode_id"], "s": seeded["shot_id"],
            "a": seeded["asset_id"], "u": people["viewer"]["id"], "r": role_id,
            "d": seeded["document_id"], "c": seeded["commit"],
            "j": seeded["job_id"], "g": seeded["profile_id"], "i": 0,
            "v": 0,               # 不存在的形态 id：先撞"形态不存在"，也是 404
            "x": 0, "t": 0, "q": 0,
            "m": 0,               # 不存在的模型登记行：外人先撞权限点，仍是 403
            "sk": "character_bio",  # 真实场景键：resolve 只要登录就能读，200
            # /files 的整段相对路径（资产目录下一张不存在的图）：
            # 归属判定在文件存在性之前，所以外人拿到的还是 404
            "af": f"{seeded['asset_id']}/not-there.png"}


def _sub(v, ids):
    """递归替换 body 里的 "{p}" 这类占位符——ids 是数组的接口（batch）也要能填。"""
    if isinstance(v, str) and v.startswith("{") and v.endswith("}"):
        return ids[v[1:-1]]
    if isinstance(v, list):
        return [_sub(x, ids) for x in v]
    if isinstance(v, dict):
        return {k: _sub(x, ids) for k, x in v.items()}
    return v


def _send(client, method, path, body, ids):
    url = _fill(path, ids)
    if body == "FILE":                      # 上传接口要 multipart，给一张最小合法 PNG
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                            "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
        return client.request(method, url, files={"file": ("x.png", png, "image/png")})
    return client.request(method, url, json=_sub(body, ids) if body is not None else None)


@pytest.mark.parametrize("method,path,body,_expect", ROUTES,
                         ids=[f"{m} {p}" for m, p, _, _ in ROUTES])
def test_匿名一律_401(client, ids, method, path, body, _expect):
    """没登录时，所有业务端点都必须 401——不例外于"看起来无害"的 GET。"""
    r = _send(client, method, path, body, ids)
    assert r.status_code == 401, f"{method} {path} → {r.status_code}（应为 401）"


@pytest.mark.parametrize("method,path,body,expect", ROUTES,
                         ids=[f"{m} {p}" for m, p, _, _ in ROUTES])
def test_越权一律被拒(as_role, ids, method, path, body, expect):
    """editor 打别人的项目要 404（不透露存在），打账号/系统要 403。"""
    c = as_role("outsider")
    r = _send(c, method, path, body, ids)
    assert r.status_code == expect, (
        f"{method} {path} → {r.status_code}（期望 {expect}）｜{r.text[:120]}")


def test_批量接口把看不见的收进_skipped_而不是整批失败(as_role, seeded):
    """规格 01-2.2-R2：批量不得因一项失败而放宽权限，也不必要地全批回滚。"""
    c = as_role("editor")          # editor 看得见自己的项目
    r = c.post("/api/projects/batch", json={"ids": [seeded["project_id"], 999999],
                                            "action": "archive"})
    assert r.status_code == 200
    body = r.json()
    assert seeded["project_id"] in body["done"]
    assert any(s["id"] == 999999 and s["reason"] for s in body["skipped"]), body


def test_路由表与本表条数对齐(app):
    """表驱动测试的守门人：加端点必须同时进这张表，否则这里先红。

    不写死"43 条"这种数字——写死的人总会忘记改，忘了改就等于新端点
    可以悄悄不进守卫回归。真正起作用的是下面那条集合相等。
    """
    paths = app.openapi()["paths"]
    real = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    business = {(m, p) for m, p in real if p not in EXCLUDED}
    tabled = {(m, p.split("?")[0]) for m, p, _, _ in ROUTES}
    # 占位符名字两边不一样（表里写 {p}，openapi 里是 {project_id}），归一后再比
    norm = lambda s: re.sub(r"\{[^}]+\}", "{}", s)
    a, b = {(m, norm(p)) for m, p in business}, {(m, norm(p)) for m, p in tabled}
    assert a == b, f"表里缺：{sorted(a - b)}；表里多：{sorted(b - a)}"


# ---------------------------------------------------------------- 交叉组合
# 上面那张表只覆盖「路径里**所有** id 都是别人的」这一种形态：`ids` 全部指向
# editor 名下的那个项目，outsider 一进来先撞 `ensure(project_id)` 就 404 了，
# 后面的 `{e}` `{j}` 到底有没有单独校验**永远不会被执行到**。
#
# 2026-11-10 的审计正是从这条缝里挖出两个真越权（都实测复现过）：
#   · `POST /api/assets/{a}/generated-images` 只判"资产归你"，不判 `job_id`
#     归谁——拿别人项目里一条成功图像作业，产物字节会被复制进自己的资产再下载
#     （两份 cmp 完全相同）；
#   · `GET /api/projects/{p}/precheck` 与 `/composition-precheck` 只判项目可见，
#     `episode_id` 原样下传，「自己的 p + 别人的 e」回 200 并按别人的集把各项
#     算完——"这一集不属于该项目"只是最后一条 blocking 文案，数据早就出去了。
#
# 所以这里补**交叉**用例：p / a 用 editor 自己看得见的，e / j 用别人的。

@pytest.fixture(scope="module")
def foreign(people):
    """outsider 名下的项目 + 一集 + 一条成功图像作业（editor 全都看不见）。"""
    import uuid

    from repositories.content import assets as asset_repo
    from repositories.content import episodes as episode_repo
    from repositories.jobs import jobs as job_repo
    from repositories.projects import projects as project_repo

    oid = people["outsider"]["id"]
    pid = project_repo.create(f"别人的项目_{uuid.uuid4().hex[:6]}", "", [], oid)["id"]
    eid = episode_repo.create(pid, "别人的第 1 集")["id"]
    job = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": None,
        "capability": "image", "kind": "image", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "交叉组合用", "params": {}, "input_refs": [],
        "created_by": oid,
    })
    aid = asset_repo.create(pid, "character", f"别人的角色_{uuid.uuid4().hex[:6]}",
                            "", "", [])["id"]
    return {"project_id": pid, "episode_id": eid, "job_id": job["id"], "asset_id": aid}


def test_交叉组合_自己的项目配别人的集必须404(as_role, seeded, foreign):
    """/precheck 与 /composition-precheck：p 是自己的，episode_id 是别人的。

    修之前两条都回 200，并且是按**别人的集**把镜头数、音画缺口那些项算完的。
    """
    c = as_role("editor")                     # editor 看得见 seeded，看不见 foreign
    mine, theirs = seeded["project_id"], foreign["episode_id"]
    assert c.get(f"/api/projects/{mine}/summary").status_code == 200, "前提：自己的项目要看得见"
    assert c.get(f"/api/projects/{theirs}").status_code == 404, "前提：别人的项目要看不见"

    r = c.get(f"/api/projects/{mine}/precheck?episode_id={theirs}")
    assert r.status_code == 404, f"整集预检把别人项目的结构算出来了：{r.text[:120]}"

    r = c.get(f"/api/projects/{mine}/composition-precheck?episode_id={theirs}")
    assert r.status_code == 404, f"合成预检把别人项目的结构算出来了：{r.text[:120]}"


def test_交叉组合_自己的资产配别人的作业必须404(as_role, seeded, foreign):
    """`POST /assets/{a}/generated-images`：a 是自己的，job_id 是别人的。

    修之前回 200：别人项目作业的产物字节被复制进自己的资产，
    再用 `/files/...` 就能下载出来——跨项目读取他人产物。
    """
    c = as_role("editor")
    r = c.post(f"/api/assets/{seeded['asset_id']}/generated-images",
               json={"job_id": foreign["job_id"], "artifact_index": 0})
    assert r.status_code == 404, (
        f"能把别人作业的产物导进自己的资产：{r.status_code} {r.text[:160]}")


def test_交叉组合_自己的资产配别人的资产也得404(as_role, seeded, foreign):
    """反方向：资产是别人的，job 是自己的——同一道门不能只装一半。"""
    c = as_role("editor")
    r = c.post(f"/api/assets/{foreign['asset_id']}/generated-images",
               json={"job_id": seeded["job_id"], "artifact_index": 0})
    assert r.status_code == 404, f"别人的资产挡住了吗：{r.status_code} {r.text[:120]}"
