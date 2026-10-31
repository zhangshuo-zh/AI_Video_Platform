# CURRENT_API.md — 真实路由表（W00 盘点）

取证时间 2026-11-01。来源三件交叉：**运行中的 `GET /api/openapi.json`**（43 条，与下表一一对上）、`app/backend/routers/*.py` 的装饰器与 `Depends(require_permission(...))`、`app/frontend/js/api.js` 的 41 个导出。响应码列直接取 openapi 的 `responses` 键，未做人工美化。

**读法**：`声明的权限点` 是全局那一层；项目级/账号级那一层在 §2。`/api/health` 是唯一不挂守卫的业务外端点，`/api/auth/*` 四条自判会话。

## 1. 全量 43 条
| # | 方法 | 路径 | 处理函数 | 声明的权限点 | 响应码 |
|---|---|---|---|---|---|
| 1 | DELETE | `/api/assets/{asset_id}` | `delete_asset` | asset:write | 204,422 |
| 2 | DELETE | `/api/assets/{asset_id}/images` | `delete_asset_image` | asset:write | 200,422 |
| 3 | POST | `/api/assets/{asset_id}/images` | `upload_asset_image` | asset:write | 200,422 |
| 4 | POST | `/api/auth/login` | `login` | (内部自判) | 200,422 |
| 5 | POST | `/api/auth/logout` | `logout` | (内部自判) | 200 |
| 6 | GET | `/api/auth/me` | `me` | (内部自判) | 200 |
| 7 | POST | `/api/auth/password` | `change_password` | (内部自判) | 200,422 |
| 8 | DELETE | `/api/episodes/{episode_id}` | `delete_episode` | doc:write | 204,422 |
| 9 | GET | `/api/episodes/{episode_id}/shots` | `list_shots` | doc:read | 200,422 |
| 10 | POST | `/api/episodes/{episode_id}/shots` | `create_shot` | doc:write | 201,422 |
| 11 | PATCH | `/api/episodes/{episode_id}/status` | `set_episode_status` | doc:write | 200,422 |
| 12 | GET | `/api/health` | `health` | (无声明) | 200 |
| 13 | GET | `/api/member-candidates` | `member_candidates` | project:write | 200 |
| 14 | GET | `/api/permissions` | `list_permissions` | user:read | 200 |
| 15 | GET | `/api/projects` | `list_projects` | project:read | 200,422 |
| 16 | POST | `/api/projects` | `create_project` | project:write | 201,422 |
| 17 | POST | `/api/projects/batch` | `batch_projects` | project:write | 200,422 |
| 18 | POST | `/api/projects/batch-delete` | `batch_delete_projects` | project:delete | 200,422 |
| 19 | DELETE | `/api/projects/{project_id}` | `delete_project` | project:delete | 204,422 |
| 20 | PUT | `/api/projects/{project_id}` | `update_project` | project:write | 200,422 |
| 21 | GET | `/api/projects/{project_id}/assets` | `list_assets` | asset:read | 200,422 |
| 22 | POST | `/api/projects/{project_id}/assets` | `create_asset` | asset:write | 201,422 |
| 23 | GET | `/api/projects/{project_id}/episodes` | `list_episodes` | doc:read | 200,422 |
| 24 | POST | `/api/projects/{project_id}/episodes` | `create_episode` | doc:write | 201,422 |
| 25 | GET | `/api/projects/{project_id}/members` | `list_members` | project:write | 200,422 |
| 26 | POST | `/api/projects/{project_id}/members` | `add_member` | project:write | 200,422 |
| 27 | DELETE | `/api/projects/{project_id}/members/{member_id}` | `remove_member` | project:write | 200,422 |
| 28 | POST | `/api/projects/{project_id}/open` | `mark_project_opened` | project:read | 204,422 |
| 29 | PATCH | `/api/projects/{project_id}/status` | `set_project_status` | project:write | 200,422 |
| 30 | GET | `/api/projects/{project_id}/summary` | `project_summary` | project:read | 200,422 |
| 31 | PUT | `/api/projects/{project_id}/tags` | `set_project_tags` | project:write | 200,422 |
| 32 | GET | `/api/providers` | `list_providers` | system:config | 200 |
| 33 | PUT | `/api/providers` | `save_providers` | system:config | 200,422 |
| 34 | GET | `/api/roles` | `list_roles` | user:read | 200 |
| 35 | PUT | `/api/roles/{role_id}/permissions` | `set_role_permissions` | user:write | 200,422 |
| 36 | DELETE | `/api/shots/{shot_id}` | `delete_shot` | doc:write | 204,422 |
| 37 | GET | `/api/tags` | `list_tags` | project:read | 200 |
| 38 | GET | `/api/users` | `list_users` | user:read | 200 |
| 39 | POST | `/api/users` | `create_user` | user:write | 201,422 |
| 40 | DELETE | `/api/users/{user_id}` | `delete_user` | user:write | 204,422 |
| 41 | PATCH | `/api/users/{user_id}` | `update_user` | user:write | 200,422 |
| 42 | POST | `/api/users/{user_id}/password` | `reset_password` | user:write | 200,422 |
| 43 | PUT | `/api/users/{user_id}/roles` | `set_user_roles` | user:assign | 200,422 |

## 2. 第二层守卫的实际落点（权限点之外）

| 组 | 文件 | 第二层判定 | 证据 |
|---|---|---|---|
| 项目 | `routers/projects.py` | `access.ensure(user, project_id, need)` 共 **11** 处；删项目/管成员另判「所有人或管理员」 | `grep -c 'ensure(' routers/projects.py` = 11；`access.py:35`、`access.py:44` |
| 内容（集/镜/资产/图） | `routers/content.py` | `ensure` / `ensure_by(kind,id,user,need)` **11 条路由全覆盖**（逐条核过：`content.py:29/37/51/61/71/79/91/101/109/123/167/197`） | `grep -n ensure routers/content.py` |
| 账号 | `routers/users.py` | 不走 `access`，走 `ranks.py` 的 2/1/0 层级；越级写回 **404** 不透露存在 | `ranks.py` 全文、`access.py:24` |
| Provider | `routers/settings.py` | 仅 `system:config`，无第二层 | `settings.py` |
| 健康 | `routers/system.py` | 无守卫（规格 02-4.1 明确列为例外） | `system.py` |

判定顺序与 02 §5.2 一致：会话 → 权限点 403 → 层级/归属（不可见 404）→ 执行。`access.py:35` 的 `ensure()` 同时承担「不可见＝404」和「级别不足＝403」两件事。

## 3. 与规格 02 §11.2「建议补齐端点簇」的差距

规格列了 14 簇新端点，现状：

| 规格端点簇 | 现状 |
|---|---|
| `POST/GET /api/jobs`、`/api/jobs/{id}`、`/cancel`、`/retry`、`/logs`、`/artifacts` | **全部不存在**（`job` 只作为权限点 `job:read/submit/cancel` 存在于 seed，无表无路由） |
| `GET/PUT /api/documents`、`/api/documents/{id}/content`、`/lock` | **全部不存在**（无 `doc` 表、无正文读写） |
| `GET/POST/PUT/DELETE /api/generation-profiles` | 不存在 |
| `POST /api/extractions`、`GET /api/extractions/{id}`、`POST /api/extraction-items/{id}/accept` | **不存在**（表也没有）。但仓库里**确实有** `平台设计/接口草案-抽取.md`（274 行，含三张新表与六个接口），它用的是另一套路径：`POST /api/extract/runs`、`GET /api/extract/runs/{id}/items`、`POST /api/extract/items/{id}/accept｜merge｜reject`、导出镜头候选 —— **与规格 02 §11.2 建议的路径不同名**，落地前要先选一套（见 SPEC_DIFF `01-4.3-E1` 行） |
| `GET /api/reviews`、`/api/reviews/{id}`、`/decision`、`/gates` | 不存在 |
| `GET/POST /api/audio-items` | 不存在 |
| `GET/POST/PUT /api/knowledge` | 不存在 |
| `GET /api/projects/{id}/dashboard` | 不存在 |

## 4. API 层已确认的四个缺陷（盘点阶段只记录，不改）

1. **三个 DELETE 不过 `toJson`**：`api.js:118 deleteEpisode`、`:129 deleteShot`、`:137 deleteAsset` 返回原始 `Response`，非 2xx 不抛 → `writing.js:197/242`、`assets.js:162` **后端拒绝也照样重渲染并当作删除成功**。规格 02 §16 禁止项「用模拟数据或假成功声称功能已完成」正中这条。
2. **`deleteProject` 丢后端文案**：`api.js:95` 把 `detail` 换成 `res.status + ' ' + res.statusText`，用户看到 "409 Conflict" 而不是服务端给人看的那句话，与 `api.js:9` 自己定的约定相反。
3. **运行中 401 没有出口**：`app.js:478` 只在 boot 期处理 401；会话 12 小时过期后任何一次写操作只会 `alert("…失败：…")`，没有重登入口。
4. **错误伪装成空态**：`assets.js:31`、`writing.js:47/50/80` 都是 `catch { x = [] }` → 403/500 时页面显示「还没有资产/还没有集」，读起来像数据丢了。