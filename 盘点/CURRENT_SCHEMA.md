# CURRENT_SCHEMA.md — 真实库结构（W00 盘点）

取证方式：`sqlite3 -readonly data/platform.db .schema`（**只读打开，未写入**）＋ `migrations/*.sql` 全文 ＋ `schema_version` 表实读。两份来源一致，11 条迁移全部落库。

## 1. 库里现在有什么

15 张表（含 `schema_version` 与 `sqlite_sequence`）：

`app_user`、`role`、`permission`、`user_role`、`role_permission`、`session`、`provider_config`、`project`、`project_tag`、`project_member`、`episode`、`shot`、`asset`、`schema_version`、`sqlite_sequence`

索引 6 条：`idx_session_user`、`idx_project_tag(tag)`、`idx_project_member_user(user_id)`、`idx_shot_episode(episode_id,seq)`、`idx_asset_project(project_id,kind,name)`、`uq_asset_name(project_id,kind,name)` **唯一**。

实读行数（2026-11-01）：`project=2`、`episode=0`、`shot=0`、`asset=0`、`provider_config=10`、`session=6`、`user_role=4`、`permission=20`、`role=3`、`app_user=2`。**除项目与账号外全是空表**——内容层（集/镜/资产）一条真实数据都没有，这决定了后面所有回归测试都可以放心建在临时库上。

## 2. 迁移履历（`schema_version` 实读）

| 版本 | 文件 | 落库时间 | 干了什么 |
|---|---|---|---|
| 1 | `0001_init.sql` | 10-30 15:16 | 建 `project`（从"schema.sql 时代"搬进迁移体系，老库靠认领跳过） |
| 2 | `0002_auth.sql` | 10-30 15:23 | 建 `app_user`/`role`/`permission`/`user_role`/`role_permission`/`session`；表名用 `app_user` 避开 Postgres 保留字 |
| 3 | `0003_providers.sql` | 10-30 15:45 | 建 `provider_config`，五能力 × 本地/云端 = 固定十行，`UNIQUE(capability,backend)` |
| 4 | `0004_content.sql` | 10-30 15:56 | 建 `episode`/`shot`/`asset` |
| 5 | `0005_project_no_kind.sql` | 10-30 17:38 | 项目去类型化：**重建** `project`（重建期要关外键，见 `migrate.py`） |
| 6 | `0006_project_tag.sql` | 10-30 17:55 | 建 `project_tag`（单开表而不是 JSON 列，为了按标签反查） |
| 7 | `0007_project_updated_at.sql` | 10-30 18:19 | 加 `updated_at`：因 SQLite 不许 `ADD COLUMN` 带非常量默认值 → **重建**整表 |
| 8 | `0008_project_description.sql` | 10-31 09:45 | 加 `description`（默认值是常量，所以这次能直接 `ADD COLUMN`） |
| 9 | `0009_superadmin.sql` | 10-31 10:09 | 超管改名 `superadmin` 并挂全角色（写成迁移而非一次性脚本，新库也要成立） |
| 10 | `0010_project_owner.sql` | 10-31 10:59 | 加 `owner_id`（NOT NULL 且每行默认值不同 → 又是**重建**）；建 `project_member` |
| 11 | `0011_project_last_opened.sql` | 10-31 15:30 | 加 `last_opened_at`（可空，NULL＝从没打开过） |

**规律**：三次重建 `project`（0005/0007/0010）都不是为了改语义，而是 SQLite 的两条限制——不能加非常量默认值的列、不能加无默认值的 NOT NULL 列。后续给 `project` 加列前先看这条，能 `ADD COLUMN` 就别重建。

## 3. 关键列逐字（与规格对表要用）

- **`project`**：`id`、`name`、`status TEXT NOT NULL DEFAULT 'active'`（**表上没有 CHECK**，白名单在 `models.py:32` 的 `Literal["active","archived"]`）、`description`、`owner_id NOT NULL REFERENCES app_user(id) ON DELETE RESTRICT`、`created_at`、`updated_at`、`last_opened_at TEXT`（可空）。计数列 `episode_count/shot_count/asset_count/episode_done` **不落库**，由 `repositories/projects.py:45` 的 `_SELECT` 聚合现算 —— 与规格 03 §4.1「派生数值不建议作为权威列」一致。
- **`episode`**：`project_id`、`seq`（项目内序号，注释写明"不从 max(id) 推"）、`title`、`status DEFAULT 'draft'`（`models.py:33` `Literal["draft","active","done"]`）、`created_at`、`UNIQUE(project_id,seq)`。**没有 `updated_at`**（规格 03 §4.4 标"待核实"，答案是没有）。
- **`shot`**：`episode_id`、`seq`、`summary`、`prompt`（单列！）、`duration REAL DEFAULT 5`、`status DEFAULT 'todo'`（注释 `todo|queued|done|failed`，**没有代码写它**——见 §4）、`asset_ids TEXT DEFAULT '[]'`、`created_at`、`UNIQUE(episode_id,seq)`。**没有 `doc_id`、没有 `shot_size`、`camera_movement`、`dialogue`、`image_prompt`、`video_prompt`、`updated_at`**。
- **`asset`**：`project_id NOT NULL`（**所以全局资产池现在做不到**）、`kind CHECK IN ('character','scene','prop')`、`name`、`summary`、`consistency_prompt`、`ref_images TEXT DEFAULT '[]'`、`status DEFAULT 'draft'`、两个时间列。**没有 `knowledge_id`**，设定引用这条链在库里不存在。
- **`provider_config`**：`capability CHECK IN ('text','image','video','tts','music')`、`backend CHECK IN ('local','cloud')`、`provider`、`model`、`enabled`、`is_default`、`updated_at`、`UNIQUE(capability,backend)`。**表上没有任何密钥列**，实读十行 `provider=''`、`model=''`、`enabled=0` —— 十个槽位一个都没配。密钥走环境变量：`repositories/providers.py:13` 拼 `FV_KEY_<PROVIDER>`（`-` 转 `_`、大写），`:20` 只回"有没有配"。与规格 01-4.8.2-5「只显已配置/未配置」一致。
- **`app_user`**：`username UNIQUE`、`display_name`、`password_hash`（scrypt 自描述串）、`is_superuser`、`is_active`、`must_change_password`、`created_at`、`last_login_at`。**没有 `created_by`/审计列**（规格 D-06 说除 Review 外不给业务实体加普遍 created_by —— 现状即符合）。
- **`session`**：`token_hash PRIMARY KEY`（sha256，库里不存明文）、`user_id CASCADE`、`created_at`、`expires_at`、`last_seen_at`。

## 4. 规格里有、库里没有的（这是"missing"的主清单）

| 规格实体 | 数据字典节 | 现状 |
|---|---|---|
| `job`（生成作业＋成本真相源） | 03 §5.3 | **不存在**。`job:read/submit/cancel` 三个权限点在 seed 里，但没有任何表或路由承接 |
| `generation_profile` 参数模板 | 03 §5.4 | 不存在 |
| `prompt_template` 提示词模板 | 03 §5.5 | 不存在，且**也没有文件型模板**（全仓搜不到模板存储） |
| `document` / Doc 元数据 | 03 §6.1 | 不存在。`doc:read/doc:write` 两个权限点**已被集/镜接口借用**（`content.py` 用 `doc:read` 守卫 `episodes`），语义与"文档"不是一回事 |
| `review` 校验与签字 | 03 §6.2 | 不存在 |
| `knowledge` 设定条目 | 03 §6.3 | 不存在（`setting:read/write` 权限点在 seed 里，无表无路由） |
| `extraction_run` / `extraction_item` | 03 §7 | 表不存在。**但 `平台设计/接口草案-抽取.md`（274 行）已在仓库里**，它自己定义了三张新表＋六个接口＋幂等规则，路径命名与规格 02 §11.2 建议的那套不同 |
| `AudioItem` | 03 §8.2 | 不存在（`audio:read/write` 同上，只有权限点） |
| `AssetImage` 独立表 | 03 §8.1 | 不存在，图片路径以 JSON 数组塞在 `asset.ref_images`（现状即规格说的"沿用已有方式"，**不要**再建第二套登记） |
| `shot.status` 的写入方 | — | 列存在但**没有任何代码写过它**：全仓 `shot.status` 只在 `_SELECT` 读取与默认值里出现，`todo|queued|done|failed` 四值目前是死字段（与 `project.status` 在 v0.53 之前的处境一模一样） |

## 5. 规格 03 §11「必须从现有仓库核对的内容」逐条回答

1. 权限点真实清单 → 20 个，逐字见 `CURRENT_PERMISSIONS.md`。
2. 表/列物理名 → 本文 §3（`seq` 而非 `number`；`title` 存在；`kind` 而非 `type`；`summary` 而非 `description`；`ref_images` 而非 `reference_images`）。
3. `shot` 归属 → 现在**只有** `episode_id`，没有 `doc_id`，也没有关联表。规格 D-02 的"保持 episode 兼容＋可选 doc 关联"落点就在这里。
4. `doc` 表是否存在 → 不存在（§4）。
5. 分集状态白名单 → `models.py:33` `Literal["draft","active","done"]`，`PATCH /api/episodes/{id}/status` 走 Pydantic 校验，表外值 422；库里有 0 行数据，**改枚举不需要回填**。
6. 资产图片路径存储 → `asset.ref_images` JSON 数组，元素形如 `"<asset_id>/<uuid>.<ext>"`，相对 `FILES_DIR`（`content.py:186`）。
7. `episode.updated_at` → 没有这一列。
8. 迁移前备份与保留 5 份 → **已实现**：`migrate.py` 的 `_backup()` 用 SQLite `backup()` API（v0.50 落的），保留最近 5 份，只在"有待应用迁移且库里已有表"时触发。规格 01-7.3-1 / 02-4.4-4 标"需核实"，答案是**有，做回归测试即可，别再造一套**。
9. `PRAGMA foreign_keys` → `db.py` 每连接开启（02-4.4-3 问的这条，现状满足）。
