# CURRENT_PERMISSIONS.md — 真实权限面（W00 盘点）

取证方式：`sqlite3 -readonly data/platform.db` 直读 `permission`/`role`/`role_permission`/`app_user` 四表 ＋ `bootstrap.py`/`security.py`/`access.py`/`ranks.py` 源码 ＋ 43 条路由的 `Depends` 逐条解析（结果在 `CURRENT_API.md` §1）。规格 02 §5.3 要求"不能通过名称猜测权限清单"——本文全部是查出来的，没有一条是推的。

## 1. 20 个权限点（逐字，`permission` 表实读）

| id | code | name | group_name |
|---|---|---|---|
| 1 | `project:read` | 查看项目 | 项目 |
| 2 | `project:write` | 新建与修改项目 | 项目 |
| 3 | `project:delete` | 删除项目 | 项目 |
| 4 | `asset:read` | 查看资产 | 资产 |
| 5 | `asset:write` | 登记与修改资产 | 资产 |
| 6 | `doc:read` | 查看文档 | 创作 |
| 7 | `doc:write` | 编辑文档 | 创作 |
| 8 | `job:read` | 查看队列与产物 | 生成 |
| 9 | `job:submit` | 提交生成作业 | 生成 |
| 10 | `job:cancel` | 取消作业 | 生成 |
| 11 | `review:read` | 查看审核 | 审核 |
| 12 | `review:sign` | 审核签字 | 审核 |
| 13 | `audio:read` | 查看音频任务 | 音频 |
| 14 | `audio:write` | 新建音频任务 | 音频 |
| 15 | `setting:read` | 查看设定 | 设定 |
| 16 | `setting:write` | 编辑设定 | 设定 |
| 17 | `user:read` | 查看用户 | 用户 |
| 18 | `user:write` | 新建与修改用户 | 用户 |
| 19 | `user:assign` | 分配角色 | 用户 |
| 20 | `system:config` | 系统设置 | 系统 |

**数量与规格假设一致（20 个）**，但**语义有偏差**：`doc:read/doc:write` 现在守的是**集与分镜**（`content.py` 的 11 条 episodes/shots 路由），不是规格里那个"文档正文"；`job:*`、`review:*`、`audio:*`、`setting:*` 四组共 11 个权限点**没有任何端点使用**——因为对应的表、路由、页面都还不存在。规格要求"不得重新发明权限点名"，这条现状满足；但 W07/W14/W15 落地时 `doc:*` 的复用冲突要先裁（见 SPEC_DIFF `01-4.3-D1` 行）。

## 2. 三角色 → 权限（`role_permission` 实读聚合）

| 角色 | code | 权限数 | 集合 |
|---|---|---|---|
| 管理员 | `admin` | **20（全部）** | 全量 |
| 制作 | `editor` | **15** | `project:read project:write asset:read asset:write doc:read doc:write job:read job:submit job:cancel review:read review:sign audio:read audio:write setting:read setting:write` |
| 只读 | `viewer` | **8** | `project:read asset:read doc:read job:read review:read setting:read audio:read user:read` |

三条要单独记住的实测事实：

1. **editor 已经有 `review:sign`**。规格 01-4.6-A3 说"非审核角色不可签字"，而现在没有独立的 reviewer 角色，制作岗天然能签字。这是**规格与现状的真冲突**，不是缺功能——要么新增 reviewer 角色（要迁移＋改 seed，牵动 `admin` 列禁用的矩阵页），要么承认"制作即可签字"。盘点阶段不动，等用户裁。
2. **viewer 有 `user:read`**。侧栏入口判定是 `perms: ['system:config','user:read']` 任一（`modules.js:61`、`app.js:14-17`）⇒ **系统设置入口对三种角色全都可见**，进去后"模型与密钥"子页再被 `system:config` 挡回空态（`system-config.js:17-22`）。这是设计稿 v0.4x 定的，规格 01-4.8.1-6 也是这个口径，只是"人人看得见入口"这件事得让人知道。
3. `role` 表**没有 `is_builtin` 列**。矩阵页"admin 列禁止编辑"前端写死 `r.code === 'admin'`（`system-users.js:104`），后端**同样拦**：`routers/users.py:25` 定义 `PROTECTED_ROLE = "admin"`，`:183` 直接 422「内置的 admin 角色不许改权限」，注释说明它是"急救通道"。前端只是不让点，真正的门在服务端——这条与规格 01-4.8.1-5 一致，**不是** verify 项。

## 3. 超级用户与层级

- `app_user.is_superuser=1` → `access.is_manager()`（`access.py:24`）与 `ranks.user_rank()` 都判 2 级，**绕过权限点判定**，且新出现的权限点自动拥有（`0002_auth.sql` 注释）。
- 层级唯一出处 `ranks.py`：2 超级用户 > 1 有 admin 角色 > 0 其余；Python 侧 `user_rank()/row_rank()`，SQL 侧 `RANK_SQL` 同一套 CASE——注释明写"两头各写一遍迟早分叉，所以集中在这里"。
- 实读账号：`superadmin`（is_superuser=1，`must_change_password=0`，最近登录 10-31 10:18）、`admin`（普通账号，挂了角色，最近登录 10-31 17:29）。**强制改密这条已经走完了**（早前记的"admin 口令待设"不再成立）。

## 4. 会话与口令

- cookie 名 `fv_session`，`HttpOnly` + `SameSite=Lax`；库里存 `sha256(token)`，不存明文（`session.token_hash`）。
- 有效期 `FASTVIDEO_SESSION_HOURS` 默认 12 小时（`config.py:22`）。
- 口令哈希 scrypt 自描述串（`security.py:19` 起）。
- 首次登录 `must_change_password=1` → 强制改密页；改密后服务端作废旧会话，前端拿新口令重登（`app.js:264-268`）。

## 5. 盘点阶段发现的两处安全面问题

1. **`bootstrap.py:19` 有硬编码的回退口令**（`os.environ.get("FASTVIDEO_ADMIN_PASSWORD", <字面值>)`）。值本身不抄进本文档；问题是它在 git 跟踪的源文件里（`.gitignore` 没排它），规格 01-7.2-7「口令不得写入提交」直接违反。**建议改成：没给环境变量就随机生成并打印一次**，或干脆拒绝启动。这条属 W01 范围内可修，改动很小。
2. **CORS 默认不注册**（`config.py:25` 空列表 → `main.py:24` 条件挂载），且 `allow_credentials` 未声明 ⇒ 换 nginx 分源部署时带 cookie 跨源实际不可用。规格 01-7.2-2 要的是"CORS 白名单默认关"，现状**符合**；但"配了白名单也带不上会话 cookie"这一点规格没写，属未来部署坑，记在这里。
