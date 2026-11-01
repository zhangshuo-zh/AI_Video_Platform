# SPEC_DIFF.md — 规格 vs 现状（W00 盘点）

**编号说明（诚实声明）**：`01_需求概要说明书.md` 与 `02_系统设计说明书.md` **没有 FR/US/AC 编号体系**，全文唯一的字母编号是 01 §9 的 `D-01`~`D-08`（逐字照抄）与 §8 的 `P0/P1/P2` 期次。其余条目的引用键（形如 `01-4.1-B3`、`02-7.3-5`）是**本次盘点为可核对新造的"节号-类别-序号"**，不是原文编号。原文无独立编号的验收 bullet 尤其要注意这一点。

**状态定义**：`done`＝代码里有且已核到证据；`partial`＝做了一半或缺关键一环；`missing`＝规格要、代码完全没有；`conflict`＝代码现状与规格规定**互相矛盾**（需要人裁，不能自动改）；`verify`＝规格自己标"待核实"，本次已核出答案（答案写在依据列）。

---

## 1. 鉴权与账号（01 §2、§4.8.1；02 §5）

| 键 | 要求 | 状态 | 依据与处理 |
|---|---|---|---|
| 01-1.2-9 | 口令登录、无自助注册、会话 12h、首登强制改密 | **done** | `config.py:22`、`app_user.must_change_password`、`app.js:231-236` |
| 01-2.2-1/2 | 权限点＋项目归属双层 | **done** | 43 条路由逐条核过，见 CURRENT_API §2 |
| 01-2.2-R1 | 不可见 404、级别不足 403 | **done** | `access.py:35-44` |
| 01-2.2-R2 | 批量逐项、done/skipped | **done** | `routers/projects.py:114-126`，前端逐项展示 `projects.js:380-388` |
| 01-2.2-R3 | 不能停用自己／不能改自己角色／超管只读／越级 404／删用户先转移 | **done** | `ranks.py` 全文、`users.py` 层级判定、删用户带转移参数 |
| 01-4.8.1-1~6 | 用户表五列、禁显哈希、五种操作、强制改密、删用户二选一、矩阵 20×3 | **done** | `system-users.js` 全文；admin 列前端禁改＋**后端也拦 422**（`users.py:183`，verify 有答案） |
| 01-7.2-1 | 端点显式声明权限，不靠前端隐藏 | **done** | 除 `/api/health` 与 4 条 auth 外全部 `require_permission` |
| 01-7.2-7 | 口令不进文档与提交 | **conflict** | `bootstrap.py:19` 的回退口令写在 git 跟踪的源文件里。**建议改成"无环境变量则随机生成并打印一次"**，W01 内可修 |
| 02-5.1-5 | 会话过期与口令过期分开，不加未确认的口令周期 | **partial** | 过期机制有；但**运行中 401 没有出口**（只在 boot 处理一次，`app.js:478`）→ 归 W02 |
| 01-4.6-A3 | 非审核角色不可签字 | **conflict** | `editor` 已拥有 `review:sign`（实读 15 权限含它），而现在没有 reviewer 角色。要么加角色（牵动 seed＋矩阵页），要么承认"制作可签字"。**等用户裁，不自动改** |

## 2. 项目中心（01 §4.1）——**W03 已于 v0.62 封版**

| 键 | 要求 | 状态 | 依据与处理 |
|---|---|---|---|
| 01-4.1-B1/B2 | 默认只显 active；最后打开排序 | **done** | `projects.js:13`、`repositories/projects.py:_ORDER`（0011 迁移） |
| 01-4.1-B3 | 打开不推 `updated_at` | **done** | `POST /api/projects/{id}/open` 只写 `last_opened_at`（v0.53 设计点） |
| 01-4.1-B4 | 名称必填、介绍 ≤500、标签去重 ≤10 且 ≤20 字 | **done** | `models.py:23-24`、`routers/projects.py:37-44` |
| 01-4.1-B5 | 一次 PUT 整体修改 | **done** | `PUT /api/projects/{id}` |
| 01-4.1-B6 | 删除输项目名、批量删固定词 | **done** | `projects.js` 两个弹窗；固定词 `BATCH_DELETE_WORD` |
| 01-4.1-B8 | 筛选后同步清除隐藏行勾选 | **done** | v0.57 未提交改动：`applyFilter()` 里按可见集合剪 `picked` 并同步按掉复选框 |
| 01-4.1-B9 | 当前项目回退不选归档 | **done** | v0.57 未提交改动：`store.load()` 退到 `activeProjects[0]`，只剩归档则置空 |
| 01-4.1-B10 | 删用户不留悬空 `owner_id` | **done** | `owner_id ON DELETE RESTRICT` ＋删用户必选转移/连删 |
| 01-4.1-A1 | 筛选叠加不丢关键词 | **done** | v0.57 未提交改动，CDP 实测 `#view-root` 写入计数 0→0 |
| 01-4.1-L7 / W03 要求 | **所有权转移 UX/API** | **done（v0.62）** | `POST /api/projects/{id}/transfer` ＋「成员」弹窗里的转移行（只有所有人与管理员看得见）。规则：只能交给当前协作人、交完自己降为协作人、接手人从名单里删掉、不推 `updated_at`、三步一个事务（`repositories/projects.transfer_owner`）。`set_owner` 保持原样给删账号流程用 |
| 01-4.1-L4 | 表格列含"最后打开" | **done（v0.62）** | 表头补这一列（从没打开过是破折号）；并修掉一个被该列暴露的真问题：`store.select()` 只写库不回读列表 ⇒ 点完一行这一列停在旧值。现在先立刻重绘、`openProject` 成功后静默回读再重绘 |

## 3. 资产中心（01 §4.2）——**W04 已于 v0.60 完成**

| 键 | 要求 | 状态 | 依据与处理 |
|---|---|---|---|
| 01-4.2-F1 | 角色/场景/道具增删改查 | **done（v0.60 补齐"改"）** | 原来只有 create／delete；新增 `GET／PUT /api/assets/{id}` 整体提交 |
| 01-4.2-F2 | 同归属同类型唯一、冲突 409 | **done＋加强** | `uq_asset_name` 拆成两条**部分索引**（项目内一条、全局池一条）——SQLite 认为 NULL 互不相等，原来那条对全局资产等于不设防 |
| 01-4.2-F3 | PNG/JPEG/WebP ≤20MB、校验实际签名、HEIC 提示 | **done** | `content.py:143-178`，四条断言在 `test_assets.py`／`test_infra.py` |
| 01-4.2-F4 | 按资产 ID＋随机名落盘，前端只用安全相对路径 | **done** | `content.py:180-186` |
| 01-4.2-F5 | 删图校验属该资产、只删登记文件、不碰未知文件 | **done** | `content.py:194-208`；`test_删资产只清它自己登记过的图` 直接造一个同目录陌生文件验证不被带走 |
| 01-4.2-L1/F6 | 全局资产池（跨项目复用、归属可为全局） | **done（v0.60，C4 已裁）** | 迁移 0012 把 `project_id` 改可空；**不需要中间表**——镜头 `asset_ids` 直接存全局 id，引用不复制；写与删只放管理员（`access.ensure_asset` 403），全局不收回成项目资产（409） |
| 01-4.2-F6 | 可引用 `Knowledge` 设定 | **missing（仍阻塞）** | 设定中心整体不存在（W 序列里没有它的前置），资产侧不预留列以免猜形状 |
| 01-4.2-F7 | 镜头页只显示 chips 跳转、不可编辑资产 | **done＋** | `writing.js:100-103`；v0.60 起资产详情反向列出引用镜头并可跳转定位那一集 |
| 01-4.2-A2 | 路径穿越拒绝 | **done** | 有自动化用例（`path=../../etc/passwd` → 404 且系统文件未动） |
| 01-4.3-R3 | 删被引用资产先拦截并列清单 | **done（v0.60）** | 409 带"哪一条镜头"，**不提供 force 旁路** |

## 4. 创作中心 / 文档 / Git（01 §4.3；02 §6.1）

| 键 | 要求 | 状态 | 依据与处理 |
|---|---|---|---|
| 01-4.3-L1 | 左树 项目→集→文档 | **done（v0.63）** | 集树一直在；文档层 W05 补上：小说／剧本两页各是"左文档列表＋右编辑器＋版本历史"。分镜不给做成文档——`shot` 表才是它的真相（`storyboard` 留在 `kind` 枚举里但界面不开放） |
| 01-4.3-L4 | 分镜表列（景别/运镜/对白/图像提示词/视频提示词/生成状态/审核状态） | **missing** | `shot` 表只有 `summary/prompt/duration/status/asset_ids`；规格点名的 6 列**一列都没有**。规格 01-4.3-L4 自己要求"以当前库已有列兼容"→ 现状就是兼容基线，扩列走迁移（W12） |
| 01-4.3-D1~D7 | 文档落文件＋状态流＋Git＋锁＋diff 预览 | **done（v0.63）** | `0013_document.sql`（`document`＋`doc_lock`）／`workspace.py`（路径规范化＋原子写）／`gitrepo.py`（参数列表调 git、每次现给署名、路径级提交）／`services.save_content()`（令牌比对→锁→写→commit→更新元数据，失败回滚并另存未应用结果）／`routers/documents.py` 12 条端点／`writing.js` 编辑器与版本面板。状态流 `draft→polishing→finalized` 落 CHECK；diff 预览是 `git show <sha> -- <path>` 的 unified diff |
| 01-4.3-D7 | 改分集状态枚举前先核白名单与已有数据 | **verify→已核** | `models.py:33` `Literal["draft","active","done"]`；库里 `episode=0` 行 ⇒ 改枚举无需回填 |
| 01-4.3-R1~R3 | 引用不拷贝、只在创作中心改 `asset_ids`、删被引用资产先拦截 | **partial** | 前两条成立；**第三条没有**：`delete_asset` 不检查镜头引用，会留下悬空 ID |
| 01-4.3-AI1~AI10 | 12 种 AI 文档操作 | **missing** | 无任何 LLM 调用、无 Job、无 diff 预览 |
| 01-4.3-E1~E3 | 文本抽取候选 | **missing** | 表与端点都没有；但 `平台设计/接口草案-抽取.md`（274 行）**已在仓库**，它定的路径是 `/api/extract/runs`、`/api/extract/items/{id}/accept｜merge｜reject`，**与规格 02 §11.2 建议的 `/api/extractions` 不同名** → 冲突，见 §8 |
| 01-4.3-V1 | 建删集只在创作中心，项目中心只读计数 | **done** | 项目中心无建集入口（v0.4x 搬走过） |

## 5. 生成中心 / 队列 / 成本（01 §4.4；02 §7、§9）

整块 **missing**，且是唯一"从零建"的一大块：

| 键 | 要求 | 状态 |
|---|---|---|
| 01-4.4-J1 | 一键提交六类作业 | missing |
| 01-4.4-J2 | 统一状态机 8 值 | missing（`shot.status` 有 `todo\|queued\|done\|failed` 四值注释，但**没有任何代码写它**，是死字段） |
| 01-4.4-J3 | Job 存 14 类快照字段 | missing（无 `job` 表） |
| 01-4.4-J4 | 本地串行一个、云端按限流 | missing（无 worker） |
| 01-4.4-J5 | 独立 worker 子进程、API 只入队 | missing（**注意**：规格 D-01 说"现有同进程 worker 先记差异"——现状是**连同进程 worker 都没有**，所以不是冲突而是空白，可直接按目标形态建） |
| 01-4.4-J6~J13 | 适配器薄包装、默认路线、云显式勾选、幂等、重试新 Job、成本快照、多币种、参数不假支持 | missing |
| 01-4.4-C1~C3 | `cost_amount = unit_price × quantity`、失败不归零、项目侧只读聚合 | missing（**库里没有任何金额列**） |
| 01-4.4-T1~T3 | 参数模板（预设而非任意命令） | missing（无表、无文件模板） |
| 01-4.4-L1~L5 | 队列概览/筛选/主表/详情抽屉/成本页 | missing（`generate.js` 36 行占位，含一整块假成本卡） |
| 02-8.1 | adapter 六方法契约 | missing（无 `adapters/` 目录） |
| 01-D-05 | 每能力只有一个有效默认 | **done**：`settings.py:31-43` 双向校验（重复默认 422、缺默认也 422）；实读 10 行是**五个 cloud 行 `is_default=1`、五个 local 行 `is_default=0`** ⇒ 每能力恰好一个默认，语义成立。但注意**默认现在全指向云端，而十个 `provider`/`model` 字段都是空**——"默认后端"指向一条没配齐的空槽，等 W06 真配 provider 时要一并复核 |

## 6. 音频 / 审核 / 设定（01 §4.5、§4.6、§4.7）

| 键 | 要求 | 状态 | 依据 |
|---|---|---|---|
| 01-4.5-F1~F8 | 三个二级页、对白列表、字级时间戳、音乐条目、viseme | **missing** | `audio.js` 24 行占位；**且 `provider_config.capability` 枚举里没有口型槽位**（`0003_providers.sql:8` 只有 text/image/video/tts/music）→ 挂 Rhubarb 前要先决定：扩枚举 or 并入 tts。**需用户裁** |
| 01-4.5-A2 | 音频生成只创建生成中心 Job、不开第二条队列 | **missing**（结构性要求，W15 落地时必须守） |
| 01-4.6-F1~F6 | 校验器 gate、结构化报告、三种结果、唯一写入口、逐句锚点 | **missing** | `review.js` 19 行占位；无 `review` 表。规格说的"包装现有校验器"指的是**另一条内容线的脚本** `存档/三皇五帝写实传/校验一致性.py`（十八项校验），它在本仓库之外、且输出格式不是结构化报告 → W14 落地时要先定"报告 schema"（规格 01-4.6-F5 自己留了这个口子） |
| 01-D-06 | 签字身份用 `reviewer_user_id`，不用自由文本 | **missing**（且 `app_user` 无普遍 created_by，符合"只给 Review 开例外"的方向） |
| 01-4.7-F1~F5 | 设定条目＋证据快照＋全局/项目范围 | **missing** | `knowledge.js` 18 行占位；无表 |

## 7. 系统设置与 Provider（01 §4.8.2）

| 键 | 要求 | 状态 | 依据 |
|---|---|---|---|
| 01-4.8.2-1 | 五能力 × 本地/云端 = 10 槽 | **done** | `provider_config` 实读 10 行 |
| 01-4.8.2-2 | 配置项含 provider/模型/默认/启用/密钥名/健康/备注 | **done（W06）** | 加 `key_env`／`notes` 两列（0014）；"允许参数"与"健康"不落库——前者由 `adapters.py` 清单按档位现给，后者每次打开现查（本地看运行时目录、云端看密钥变量），存下来只会变成"上次检查时是好的" |
| 01-4.8.2-3 | 密钥只经 `FV_KEY_<PROVIDER>`、页面只显已配置 | **done（W06 扩了命名口径）** | 优先级：这一行填的 `key_env` > 清单建议名（deepseek→`DEEPSEEK_API_KEY`）> `FV_KEY_<PROVIDER>`。只存变量名；值只在进程环境里，测试用真串反证过响应/库/日志 |
| 01-4.8.2-6 | 能力支持由 adapter manifest 驱动 | **done（W06；W10 补两家云端）** | `backend/adapters.py` 登记 15 个 adapter：本地 5 个带模型档与参数出处；云端 W06 只登记名字，W10 补入 DeepSeek `deepseek-flash` 与 Wan `wan2.7-t2v` 的模型/参数/价格，其余仍空；未登记的 provider 明说"不猜" |
| 01-4.8.2-4 | 普通模块只读"当前默认模型/状态" | **done（W06）** | `GET /api/providers/current`（`require_user`，五类固定五行，不带密钥名与备注） |
| 01-4.8.2-5 | 云端视频必须逐镜头显式选择、不自动兜底 | **done（W06）** | 保存时云端视频不许设默认（422）；`services.resolve_provider()` 默认轨没配好直接 `NoRoute`，视频走云端还要"显式＋已确认计费" |
| 01-D-08 | 所有价格视为待复核 | **partial（W10）** | DeepSeek `deepseek-flash` 与阿里云 Wan 2.7 的价格、模型、参数已按 2026-11-01 官方文档核过并进清单（见 §18）；火山方舟／MiniMax／可灵／Seedance／图像与配音云端档**仍然没有价格数据**，继续按 verify 处理，W17 再复核 |
| 现场坑 | — | **已解（W06）** | 名字对不上：`key_env` 可填 `DEEPSEEK_API_KEY`，清单对 deepseek 直接给这个建议名。**值仍只在交互式 shell 里**（`~/.zshrc`）⇒ 服务要从带变量的终端启动，否则健康显示"环境变量没有值"；这是运行时事实，不是代码能绕的 |

## 8. 冲突清单（需要人裁，盘点阶段一律不动代码）

| # | 冲突 | 两个选项 | 盘点的建议 |
|---|---|---|---|
| C1 | `editor` 已能签字 vs 规格"非审核角色不可签字" | 加 reviewer 角色 / 承认制作可签字 | **先不签**：审核中心整体还不存在（W14），等做 W14 时一起裁，现在改 seed 是空转 |
| C2 | 抽取接口路径：仓库草案 `/api/extract/*` vs 规格 `/api/extractions` | 选一套 | **用仓库草案那套**（它已定义三表＋幂等＋证据偏移，比规格的簇更具体；规格 04-W13 自己就说"依据已存在的草案核对"） |
| C3 | `doc:read/doc:write` 现被集/镜接口占用 vs 规格里 Doc 是正文实体 | 复用 / 新增 `document:*` | **复用**：集与文档同属创作域，规格 01-2.1 明令"不得重新发明权限点"；把"文档正文"的读写继续挂在这两个点上，W05 不需要新权限点 |
| C4 | 全局资产池要 `project_id NULL` vs 现状 NOT NULL | ~~加迁移放宽 / 不做全局~~ | **已裁（2026-11-01，v0.60）**：做。授权策略按本次指令 §5.3 落成"全局资产人人可读，写与删只放管理员；项目引用全局资产不复制（镜头 `asset_ids` 直接存全局 id），全局资产不可收回成项目资产"。落点见 §3 与 `盘点/资产中心-现状与差异.md` |
| C5 | 口型没有 provider 槽位 | 扩 capability 枚举 / 并入 tts | **并入 tts**（少一次迁移、少一格 UI；Rhubarb 是 tts 的后处理，不是独立能力） |
| C6 | `shot.status` 死字段四值 vs Job 状态机八值 | 保留做派生 / 删掉 | **保留但改为派生**：镜头的"最新生成状态"由 Job 聚合出来，`shot.status` 降级为缓存列或直接删列（W12 一并处理） |
| C7 | 回退口令硬编码在源码 | 随机生成 / 拒绝启动 | **随机生成并打印一次**，W01 顺手修 |

## 9. 规格要求的"先记差异不许重写"清单（本盘点即那份记录）

`02 §16` 的 7 条禁止项对照现状，全部**没有被违反过**：没有第二套队列/成本/签字入口（因为它们都还不存在）、没有把正文搬进 SQLite、没有引入构建链、没有 Node/React、没有假 API（**除 §4 那三个 DELETE 的假成功与四个占位页的死按钮，这两处正是 02 §16 禁止的"用静态按钮或假百分比声称功能已完成"，必须在 W02 里消掉**）。

---

## 10. 优先级实施计划（≤15 条，W00 之后的路）

排序原则：**先让"假成功"变成"真失败"（否则后面所有测试都不可信）→ 再补没有地基的表 → 再碰需要真模型的**。目标机未到手、五个工具仓库不在本机，所以 W09/W10/W15/W17 现在**必然 BLOCKED**，不排进本轮。

1. **W01-a 建测试地基**：`pytest` ＋ 临时库 fixture（`FASTVIDEO_DB` 指临时文件）＋ `httpx.ASGITransport` 进程内客户端；先给 43 条路由做"守卫存在性"回归（每条断言未登录 401、越权 403/404）。
2. **W01-b 修三个假成功**：`api.js:118/129/137` 补 `toJson`；`deleteProject` 改回透传 `detail`。
3. **W01-c 修 `bootstrap.py` 回退口令**（C7）＋ 清 `data/fastvideo.db` 空文件 ＋ 修备份 sidecar 泄漏（`migrate.py:47` 的 glob 把 `.bak-*-shm/-wal` 当独立备份计数，导致只留 2 份真备份且旧 sidecar 永不清理）。
4. **W02-a 401 出口**：`api.js` 层统一拦截 401 → 弹闸门重登，而不是 alert。
5. **W02-b 错误不再伪装空态**：`assets.js:31`、`writing.js:47/50/80` 四处 `catch{[]}` 改成"读不到 X ＋ 重试"。
6. **W02-c 交互补齐**：4 个缺 Escape 的弹窗、2 个缺 Enter 的表单、`assets.js:33`/`writing.js:101` 的 span chip 改 `<button>`、radio 圆形化、原生 `confirm()` 五处换成自绘危险弹窗。
7. **W02-d 窄屏两条硬伤**：表格加 `.table-wrap{overflow-x:auto}`（并把 `.card:has(table)` 的 hidden 改成不裁列）、`.nav-sub` 在 860px 以下改为可展开而不是 `display:none`。
8. **W03 项目中心封版**：所有权转移（API＋成员弹窗按钮）、补"最后打开"列、修 `modules.js`/`assets.js` 的 subs 名称互换（这条其实是资产侧，一起收）。
9. **W04 资产中心封版**：全局资产池迁移（C4，先要用户裁授权策略）、删资产前检查镜头引用并列清单（01-4.3-R3）。
10. **W05 文档层**：**已于 v0.63 完成**——`document`＋`doc_lock` 迁移、`FASTVIDEO_WORKSPACE` 配置、路径规范化与根目录约束、Git 初始化与提交、`expected_version` 冲突 409、编辑锁 holder/超时回收、创作中心两页做成真编辑器。当时写的"这块纯软件可验，是'第一条 mp4'之前最值钱的可交付"成立：188 项自动化＋21 项真浏览器全过，不依赖任何模型与密钥。
11. **W06 Provider 封版**：`adapter manifest`（先只做能力清单，不接真模型）、补"允许参数/健康状态/备注"三列、密钥命名冲突（C7 之外的 `FV_KEY_*` vs `DEEPSEEK_API_KEY`）给出迁移说明。**已于 v0.66 完成**：清单 15 个 adapter、`key_env` 可填既有名、健康五档、云端视频不许默认、`resolve_provider()` 不兜底；209 项自动化＋16 项真浏览器。
12. **W07 Job 表＋持久队列＋独立 worker**：按 01-4.4-J3 的 14 类字段建表、`idempotency_key` 唯一、状态机 8 值、`POST /api/jobs` 202 立即返回、worker 独立进程＋重启恢复＋原子领取。**用假 adapter（sleep 脚本）就能把整条队列语义测穿**，不需要真模型。**已于 v0.67 完成**：`job`＋`worker_state`（0015）、独立 `worker.py`、8 条接口；243 项自动化＋13 项真进程＋6 项真浏览器；真适配器仍留 W09。
13. **W08 生成中心 UI＋成本账**：队列表/筛选/详情抽屉/日志/重试取消、成本字段用整数最小货币单位、多币种分组、失败不归零。
14. **W12 分镜表扩列**（依赖 W05/W11 的字段确定）＋ W14 审核中心（依赖 W05/W07）。
15. **BLOCKED 待外部条件**：W09/W10/W15/W17（要真模型与目标机）、W11/W13（要 W05＋真 LLM key）、W16/W18（要前面全部）。

**下一步建议从 1—3（W01）开始**：它们决定后面每一次"测过了"是不是真的。

---

## 11. W01 完成后回填（2026-11-01）

计划 1—3 已落，`pytest tests -q` → **94 passed / 0.62s**（`app/backend/tests/`）。三条与盘点预期不同的实测结果，按规格 §7.4「发现差异先记」补在这里：

1. **`01-2.2-R3` 的"越级写返回 404"在超管身上不成立**：`_guard_target`（`routers/users.py`）实际是两条独立规则——目标是超管一律 **422「超级用户账号不可修改」**，只有"层级比调用者高且非超管"才 404。而层级 2 的唯一来源就是 `is_superuser`，所以**404 那条分支今天是走不到的防御代码**。测试按实测行为写死并注明原因，没有为了符合文档而改代码。
2. **`01-4.2-F5` 的删除精准性已可回归**：伪造 MIME／超 20MB／HEIC／白名单外类型四条回码分别是 415/413/415/415，正常 PNG 落盘 `FILES_DIR/<asset_id>/<uuid>.png`；`path=../../etc/passwd` 走未登记分支回 404 且不动系统文件。这条以前只靠代码审查，现在是断言。
3. **备份"保留 5 份"以前是假的**：`migrate.py` 的 glob 把 `.bak-*-wal/-shm` 当独立备份计数 ⇒ 实得 2 份真备份，且旧 sidecar 成孤儿（`data/` 里现存两个）。修后新增一条用例直接造 8 份带 sidecar 的备份来数份数与查孤儿。

另外两件盘点时没预料到的：`test_guards.py` 里 `POST /api/projects/batch` 对"看不见的项目"返回的是 **200 + skipped**，而 `DELETE /api/projects/{id}` 对 editor 返回 **403**（editor 的 15 个权限点里没有 `project:delete`）——同一张表里两类"越权"的不同落点，说明"批量逐项"与"单点守卫"确实是两套语义，测试表按实测分别写死。

**未动的三件**（属于 W02，不在 W01 范围）：运行中 401 无重登出口、四处 `catch { [] }` 把错误伪装成空态、原生 `confirm()` 与自绘危险弹窗并存。

---

## 12. W02 完成后回填（2026-11-01）

计划第 4—7 条已落，`pytest tests -q` → **120 passed**，CDP 真浏览器 **16 项全过**。逐条对账：

| 盘点条目 | 现在 |
|---|---|
| CURRENT_UI §4「401 只在 boot 处理一次」 | 已修：`api.js` 认到 401 派 `app:unauthorized` → 闸门带说明；登录/改密接口的 401 排除 |
| CURRENT_UI §4「错误伪装成空态」四处 | 已修：`writing.js` 三处 + `assets.js` 一处（v0.60 已改），统一 `errorBox/errorLine` |
| CURRENT_UI §4「原生 confirm 五处」 | 已修：剩三处（另两处随 v0.60 资产页重写消失），统一 `confirmDanger` |
| CURRENT_UI §5「四个弹窗缺 Escape」 | 已修，且**修的过程暴露一个更根本的错**：Escape 绑在弹窗元素上依赖焦点，成员弹窗打开时没有焦点 ⇒ 不生效。改成 document 级 + 关最上层可见弹窗 |
| CURRENT_UI §5「两处缺 Enter」 | 已修：新建用户三个输入框、重置口令输入框 |
| CURRENT_UI §9「表格无横向滚动」 | 已修：五张表统一 `.table-wrap`。**但实测修正了盘点的判断**：640px 时表格是靠换行压缩、并不溢出，要 380px 才逼出滚动条——所以这条修的是"溢出时得能滚"，不是"平时一直在裁" |
| CURRENT_UI §9「860px 以下二级导航彻底消失」 | 已修：`writing`／`system-config`（含无权限那一屏）抬头补页内切换器，与侧栏同一个 `navstate` 下标；`assets` 页 v0.60 已有 |
| CURRENT_UI §5「侧栏不可 Tab」 | **未做**，属可访问性专项，与 W02 的"错误一致性"不同轴，留到 W18 一起处理 |
| CURRENT_UI §5「全站无焦点陷阱」 | **未做**，同上。`confirmDanger` 因为按钮自持焦点不受影响，其余弹窗 Tab 仍会跑到遮罩背后 |

新增 5 条静态守门人测试（无原生 `confirm()`／每张 `<table>` 都有 `.table-wrap`／401 出口两端齐／有二级页的模块页内有切换器／`alert` 只在 `util.js`），并把原来写死 `util.js:20` 行号的那条改成只比文件名——行号会飘，那种断言只会制造无谓改动。

## 13. W05 完成后回填（2026-11-01）

计划第 10 条已落，`pytest tests -q` → **188 passed**（新增 `test_documents.py` 36 项，路由守卫表 48 → 60 条），CDP 真浏览器 **21 项全过**。逐条对账：

| 盘点条目 | 现在 |
|---|---|
| CURRENT_ARCHITECTURE §「没有文档层」 | 已补：`workspace.py`／`gitrepo.py`／`services` 里那条"读—写—提交"链／`routers/documents.py` 12 条端点 |
| CURRENT_SCHEMA「九实体里只缺 Doc」 | 已补 `document`＋`doc_lock`（0013）。**正文不进库**这条守住了：库里没有一列装全文 |
| CURRENT_API「创作中心只有 episodes/shots」 | 60 条业务端点，文档一组齐：列表／新建／详情／改元数据／改状态／删／读正文／写正文／拿锁／放锁／版本／差异 |
| CURRENT_PERMISSIONS C3（`doc:*` 被集镜占用） | 已结：**复用不新增**，文档读写继续挂 `doc:read`／`doc:write` |
| CURRENT_UI §「小说／剧本两页是占位文案」 | 已换成真界面：文档列表＋编辑器＋版本历史＋冲突对照 |
| 02 §6.1 五条（路径规范化／原子写／commit 失败可恢复／冲突 409／锁有 holder 与 expiry） | 五条都有实现与测试，其中"commit 失败"走的是**回滚＋另存未应用结果**两条一起（规格写的是"或"） |
| 02 §14.1「文档路径规范化、Git 写回补偿、锁超时与版本冲突」四项单测 | 四项齐（越界路径九种拒收＋库里躺脏路径的读写拒绝、Git 失败回滚与留底、过期回收与续期、409/423/不带版本 422） |

**还没做的两条，说清楚免得后一轮误以为已经成立**：① 01-4.3-L4 分镜表那 6 列（景别／运镜／对白／图像提示词／视频提示词／审核状态）仍然没有，那是 W12；② AI 文档操作（01-4.3-AI1~AI10）与文本抽取（E1~E3）要 Job 与 LLM provider，排在 W07/W11，`doc_lock` 与 `git_commit` 已经是它们需要的锚（候选入库要绑"证据出自哪一版正文"）。

**这一轮踩到的三件事，记下来**：(a) 版本历史行用 `float:right` 排版会行行叠在一起——float 不参与兄弟行的高度计算，改 flex；(b) git 默认把非 ASCII 路径转成八进制串，diff 头完全没法读，`-c core.quotePath=false`；(c) 正文不补结尾换行，diff 末尾永远挂一句 `\ No newline at end of file`，而且下次在末尾追加会把上一行也标成改动——**但规范化必须同时参与令牌计算**，否则客户端拿着"没换行那份"的哈希去存，服务端按"有换行那份"算，每次保存都自己和自己冲突。

**v0.64 补记（走真流程才发现的两处）**：① 编辑器只有一行高——`textarea.ctl{min-height:66px}` 是「元素＋类」的选择器，单一类 `.doc-area` 压不过它，正文框停在 44px；改成 `textarea.ctl.doc-area`（0,2,1）并排到 `textarea.ctl` 之后。② 删掉一篇再建同名的会占回同一个文件、版本面板混着上一篇的提交；"路径被占"改成"库里还有这行 **或** Git 历史里有过这个路径"（`gitrepo.path_has_history`），同名重建排号，新稿子只有 1 条提交。两条都补了断言。③ 另修一处：正文规范化会把空正文补成一个换行，刚建好、一个字没写的章节在编辑器上显示「1 字」，像平台替人写了一个字；空串直接返回空串，建好的空章节是 0 字节。`pytest` 190 passed。

**v0.65 补记（在编辑器里真写一段才发现的三处）**：① 保存回执只有提交号没有时间，存完「改动」那行还停在上一版——`DocSaveOut` 补 `updated_at`，界面按回执只改两个标签，不整块重绘。② 列表行把**自己**持有的锁也显示成「×× 在改」（只判了名字非空），改成还要比对持有人是否本人。③ 字数把规范化补上的结尾换行一起数了（68 字显示 69），先剥尾再数。另钉死空保存口径：内容没变不写库、不推 `updated_at`（那列语义是"内容动过"）。`pytest` 192 passed；真浏览器两段正文分别提交 `eb602202`／`3f439d1b`，页面标签、文件字节、库里时间四处对齐。**与内置浏览器面板有关的一条工具事实**：面板隐藏时它的 CDP surface 会 detach，截图与派发鼠标事件都报 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`／点击不回执；这一轮的落点是改用无头 Chrome＋stdlib WebSocket 真鼠标，或直接用页面里的 `.click()` 验事件处理器本身——两者要分清楚，"点了没反应"未必是界面坏了。

## 14. W06 完成后回填（2026-11-01）

`pytest tests -q` → **209 passed**（新增 `test_providers.py` 17 项），路由守卫表 **57 条业务端点 / 62 条含 health 与四条 auth**（v0.63 写的"60 条业务端点"实际是总路由数，这一轮把口径改准：62 条总路由，去掉 `/api/health` 与四条 auth 才是 57 条业务端点），CDP 真浏览器 **16 项全过**。

| 盘点条目 | 现在 |
|---|---|
| CURRENT_SCHEMA 的 provider_config 只有四列 | 加 `key_env`／`notes`（0014）。**允许参数与健康不落库**：前者由清单按档位现给，后者现查 |
| 01-4.8.2 默认策略（图像/视频/TTS/音乐本地、文本云端） | 0014 把四类默认轨从云端改回本地；文本保持云端 |
| 01-4.8.2 云端视频逐镜头显式选择 | 两处挡：保存时云端视频不许设默认（422）；`resolve_provider()` 视频走云端要"显式＋confirmed_cloud" |
| 01-4.8.2 adapter manifest | `backend/adapters.py`：15 个 adapter，本地五档带参数与出处；云端 W06 当时只登记名字，W10 另补 DeepSeek 与 Wan 2.7 两档 |
| 现场坑 DEEPSEEK_API_KEY vs FV_KEY_DEEPSEEK | `key_env` 可填既有名；deepseek 在清单里直接建议 `DEEPSEEK_API_KEY`。值只在进程环境，服务须从带变量的终端启动 |
| 01-4.8.2 普通模块只读默认 | `GET /api/providers/current`（require_user），不带密钥名与备注 |

**W06 的边界（别误读成"模型能跑了"）**：`adapters.py` 只是能力清单与健康检查，不执行任何模型；云端 provider 的模型档、参数、价格一律空着，等 W10（接入与授权）与 W17（许可与计价复核）填。`resolve_provider()` 是 W07 队列的入口约定，现在只有测试在调。

**本轮改到的文件**：`migrations/0014_provider_manifest.sql`、`backend/adapters.py`（新）、`backend/repositories/providers.py`、`backend/services.py`、`backend/models.py`、`backend/routers/settings.py`、`frontend/js/api.js`、`frontend/js/views/system-config.js`、`frontend/css/app.css`、`tests/test_providers.py`（新）、`tests/test_guards.py`。

**验收时抓到三处界面在说谎（已修，记下来）**：① 未登记的 provider 与空 provider 共用一个分支，参数列都显示"选 provider 后按清单显示"——现在分开说"未登记这个 provider；参数不猜"；② provider 还没填的行，备注带却写"没有登记这个 provider"——现在空行说"选 provider 后显示适配器与出处"；③ 保存成功后健康列还停在页面加载时的结论——现在编辑即标"待保存"，保存成功后按服务端回读只更新那一列（不重绘整页，免得丢焦点）。

## 15. W07 完成后回填（2026-11-01）

`pytest tests -q` → **243 passed**（新增 `test_jobs.py` 18 项），路由守卫表 **65 条业务端点 / 70 条含 health 与四条 auth**。真进程：API＋独立 worker 起在同一份真库上，HTTP 13 项、崩溃/恢复两条路径、重试一条；真浏览器 6 项（`app/截图/v67-worker.png`）。

| 盘点条目 | 现在 |
|---|---|
| CURRENT_SCHEMA「`job` 不存在，只有三个权限点」 | 已补 `job` 表（0015）＋ `worker_state` 心跳表；`job:*` 三个权限点全部有端点承接 |
| CURRENT_API「`/api/jobs` 全部不存在」 | 8 条：创建／列表／详情／取消／重试／日志／产物／worker 状态 |
| CURRENT_ARCHITECTURE「无 job 表、无 worker、无成本列」 | 队列与独立 worker 已建；成本列先建好（micros）等 W08 填 |
| 01-4.4「Job 存 14 类快照字段」 | 14 类齐：能力/执行方式/provider/模型/adapter 版本/参数/提示词/输入引用/产物/日志/错误/时间/重试/成本（成本 W08 写入） |
| 01-4.4「8 值状态机」 | 八值齐，CHECK 约束写死，不并存第二套 |
| 01-4.4「单本地 worker 串行」 | 领取前检查租约未过期的 starting/running，第二条领不到 |
| 01-4.4「API 快速返回，不停在请求里」 | `POST /api/jobs` 只落库，202；执行在 `worker.py` |
| 01-4.4「worker 消失后不永挂」 | 启动恢复＋运行中周期恢复；kill -9 实测落 `failed(worker_lost)` |
| 01-4.4「重试新建 Job，原记录保留」 | `retry_of_job_id` 新行复制快照，原行日志/耗时/错误不动 |
| 01-4.4「幂等键不重复执行」 | 全局 UNIQUE；重复提交 200 返回原行，不重跑 |
| 02 §7.2「Task 与 Job 分开」 | **Task 暂不建**：队列只由 Job 驱动，W14 从 Job/Review 派生；这是有意延后，不是漏做 |
| 02 §7.3「参数白名单 / 云端二次确认」 | 提交时按 W06 清单校验；云端视频须 `user_confirmed_cloud=true`；真适配器执行仍 `adapter_not_ready` |

**W07 的边界**：不跑任何模型。`worker.py` 对真适配器只做白名单校验然后失败，第一条真 mp4 还差 W08（提交/队列界面）与 W09（adapter 接线）。成本列已建但 W07 不写；`billable_status` 默认 `not_billable`，等 W08/W10 才有意义。

**这一轮真被踩到的两个实现问题（都修了）**：
1. **"崩溃后立刻重启"会漏收**：只在启动时 `recover_stale()`，重启时租约还没过期，job 会永远停在 running。改成主循环每轮再收一次；正常作业每 0.2 秒续租，所以不会被误杀。真进程用 `kill -9` 复现并验证。
2. **仓储层在未提交事务里用另一个连接读回**：`create/claim/retry/request_cancel` 原先调 `self.get()`，第二个连接看不到未提交的 INSERT/UPDATE，会拿到 None 或旧状态。全部改成在同一个连接里 `SELECT` 读回。

## 16. W08 完成后回填（2026-11-01）

`pytest tests -q` → **272 passed**（新增 `test_cost.py` 7 项、`test_profiles.py` 5 项、管理员空筛选 1 项；守卫表 +8 条入口＝参数化 +16）。路由 **72 条业务端点 / 77 条含 health 与四条 auth**。真浏览器 **33 项全过**，图在 `app/截图/v68-generate.png`、`v68-submit.png`、`v68-drawer.png`、`v68-profiles.png`、`v68-project-cost.png`。

| 盘点条目 | 现在 |
|---|---|
| CURRENT_SCHEMA「job 成本列建好但没人写」 | W08 开始写：`quantity/unit/unit_price_micros/cost_micros/currency/pricing_snapshot_json/billable_status`。另加 `resources_json`（本地机时）与 `generation_profile`（0016） |
| 03 §5.3「Job 存单位、数量、单价、金额、币种、定价快照、账单核对状态」 | 七类齐；金额是整数 micros，数量是 REAL。定价快照含来源、确认人、确认时间、公式 |
| 03 §5.4「generation_profile 参数模板」 | 表已建；项目模板按项目归属读写，全局模板（project_id NULL）只有管理员能写；改模板不影响已提交 Job 的参数快照 |
| 03 §5.3「禁止失败自动清零 / 重试覆盖原 Job」 | 失败与取消都不动金额；重试新建一行继承计价快照、原行成本/日志/错误不动 |
| W08 验收「云端预计成本需用户确认」 | 云端提交必须带 `estimate` 并 `user_confirmed_cloud=true`；金额服务端 Decimal 半入，不采信客户端总价 |
| W08 验收「失败不默认归零」 | `fail()` 不写成本列；真浏览器里云端作业失败后仍 ¥6.00，账单状态待核对 |
| W08 验收「多币种分别汇总」 | `GET /api/jobs/costs` 按币种分桶，`by_capability` 只在该币种内汇总；项目中心同样按币种分行，不合计 |
| W08 验收「重试保留原成本」 | 新行继承数量/单价/币种/快照，金额重算为同一值并回到待核对；测试断言原行仍 4,000,000 micros |
| W08 验收「本地任务记录耗时和资源」 | `resources_json` 记 wall_ms / cpu_ms / max_rss_kb；macOS 的字节值统一折算为 KB |
| W08「队列表/过滤/详情/日志/产物/失败/重试/取消/参数模板」 | 生成中心一整页：四组筛选＋搜索、详情抽屉、日志尾部、产物下载、取消/重试、模板建改删应用、多币种成本账 |
| W08「项目中心只读摘要」 | 项目列表带 `costs[]`（币种、金额、笔数、待核对笔数）；进入项目中心时重拉一次列表，避免看过的是旧账 |
| CURRENT_API「作业列表只有状态/能力筛选」 | 加执行方式、逗号分隔状态（“进行中”＝四态）、作业号/提示词搜索；管理员无筛选的空条件 500 已修 |
| CURRENT_ARCHITECTURE「普通模块没有可用的槽位选择接口」 | 新增 `GET /api/providers/available`：只回已启用槽位、健康与参数白名单，不带密钥变量名与备注 |

**W08 的边界（别误读成“云端能计费了”）**：
- 仍然**不跑任何模型**：真适配器照旧以 `adapter_not_ready` 失败；第一条真 mp4 在 W09。
- 云端单价是**提交人填的预计**，带来源，不是价目表自动取价；实际用量与供应商回执（`provider_request_id`、known 账单）在 W10。
- 产物只做“列表＋按序号下载”，文本/mock 可直接看；真视频的播放器、缩略图与落盘登记在 W09。
- 业务 Task 表仍不建；参数模板不是提示词模板（§5.5 的 `prompt_template` 仍未动）。

**这一轮真被踩到的五个问题（都修了）**：
1. **管理员无筛选列作业 500**：`project_ids=None`、状态/能力也空时条件数组为空，SQL 拼成 `WHERE  ORDER BY`，回 500；空条件补 `1`，并加了一条守卫测试。
2. **macOS 的 `ru_maxrss` 是字节**：直接按 KB 入库，成本卡片显示“21344 MB”；按平台折算成 KB，测试加“< 10,000,000 KB”上限。
3. **抽屉只在打开时取一次**：作业从排队变失败后，正文仍停在“排队中”，错误与日志永远不出现；改为随 3 秒轮询刷新，并保留滚动位置。
4. **模板列表重拉错项目**：新建模板后用“顶栏当前项目”重拉，刚建的模板从列表里消失；改为记住模板弹窗打开时的项目（保存到别的项目就跟着切换）。
5. **刷新失败弹 alert 会堵死页面**：提交后一次 500 触发 `alert`，主线程阻塞，CDP 与“重试”都点不到；改成页内错误条。

**本轮改到的文件**：`migrations/0016_cost_profile.sql`、`backend/models.py`、`backend/services.py`、`backend/repositories/jobs.py`、`backend/repositories/profiles.py`（新）、`backend/routers/jobs.py`、`backend/routers/profiles.py`（新）、`backend/routers/settings.py`、`backend/repositories/projects.py`、`backend/jobfiles.py`、`backend/worker.py`、`backend/main.py`、`frontend/js/api.js`、`frontend/js/util.js`、`frontend/js/modules.js`、`frontend/js/views/generate.js`、`frontend/js/views/projects.js`、`frontend/js/views/system-config.js`、`frontend/css/app.css`、`tests/conftest.py`、`tests/test_guards.py`、`tests/test_jobs.py`、`tests/test_cost.py`（新）、`tests/test_profiles.py`（新）。

## 17. W09 完成后回填（2026-11-01）

`pytest tests -q` → **281 passed**（新增 `test_runners.py` 9 项：argv 数组、提示词原样、缺权重、运行时探测与健康同口径、选了 provider 没填模型、mflux CLI、I2V 条件图边界、产物魔数、metrics 摘要）。路由不变：**72 条业务端点 / 77 条含 health 与四条 auth**。真浏览器 **22 项全过**（分镜 → 预填 → 入队 → 成功 → 产物可读 → 机时/内部耗时）＋补证 **5 项**（播放器首帧、系统设置健康、项目中心），图与视频在 `app/截图/v69-*.png`、`v69-baseline.mp4`；基准细节在 `盘点/W09_BASELINE.md`。

| 盘点条目 | 现在 |
|---|---|
| W09「按真实仓库与模型权重完成 FastMetal T2V」 | **已跑通**：`fastmetal-1.3b-qad`，640×352/81 帧/3 步 DMD/`--fast-spatial`/seed 1024，墙钟 28.2s、峰值 2.39GB、265KB H.264 |
| W09「mlx-video I2V（符合实际支持能力的模型档）」 | 命令已按 README 接好（`python -m mlx_video.wan_2.generate --model-dir ... --image ...`），**本机没有仓库/转换权重**，健康回“缺条件”；不假装出图 |
| W09「mflux image」 | 命令已按 CLI 接好（`mflux-generate --model schnell ... --output`），**本机没有 mflux-generate**，健康回“缺条件”；不假装出图 |
| W09「适配器只起 CLI 子进程」 | `worker.py` 用 `subprocess.Popen(argv, shell=False)`；提示词是独立 argv 元素，不做字符串拼接 |
| W09「返回规范化 Job 结果」 | 成功只登记经过魔数/非空校验的产物相对路径；失败归一为 `adapter_unavailable`／`adapter_start_failed`／`adapter_failed`／`empty_output` |
| W09「浏览器分镜选择 → Job 入队」 | 分镜行「生成」→ navstate 一次性草稿 → 生成中心预填项目/集/镜/提示词 → 提交带 `episode_id`/`shot_id` |
| W09「产物登记/可预览」 | 详情抽屉里 mp4 内联播放器（实测元数据 640×352、5.06s、无解码错误）、图片/音频各自预览 |
| W09「成本/耗时记录」 | 本地 0 元/不计费；`resources_json` 记 worker CPU 307ms、子进程 CPU 8.6s、子进程峰值 2038MB、墙钟 28.2s；模型内部 提示词编码 0.01s / 去噪 19.09s / 解码 2.97s |
| W09「至少一个可复现成功基准」 | ✅ 作业 #35；SHA-256 `b73eb88929d57499a3a48a2902e17dfa1ae08fe7190daefeac9c15b3376468b0` |
| W06 健康检查「本地只看目录在不在」 | 升级为逐项探测：解释器、入口脚本、模型四件套；mlx-video 还查包与 `config.json`，mflux 查可执行文件。仍不加载权重 |
| W06 清单“1.3B 入口只有 --fast” | **旧说法错误**：1.3B 入口同样有 `--fast-spatial`/`--fast`/`--refine`；v0.69 按 `--help` 复核并实测 `--fast-spatial` |
| W06 清单 1.3B 默认 448×832/77 | 改为 640×352/81；832×480/81 在 16GB M4 Air 上解码 OOM（作业 #33） |
| W08 边界「真适配器照旧 adapter_not_ready」 | 已结束：真适配器缺运行时回 `adapter_unavailable` 并说清缺哪件；有运行时则真跑 |

**W09 的边界（别误读成“三档都能出片了”）**：
- 只有 FastMetal 1.3B 在这台机器上有权重并跑通；5B 权重不在本机。
- mlx-video 与 mflux 的命令构造、健康检查、失败归一都做了，但**本机没有运行时/权重**，所以没有它们的真实产物；装好后不需要改 worker。
- `--fast-spatial` 是 320×176 去噪+解码再上采样，**属于预览档，不是交付画质**；成片档要等 M5 Ultra 96GB 或全分辨率解码。
- `--fast`（RIFE）与 `--refine` 未实测；视频画质没有做主观评分。
- 云端仍不接、不发起任何调用；W10 才做云端 adapter 与显式授权。
- 分镜 `shot.status` 仍未由 Job 自动改写：W12 要把“多 Job 对一个镜头”的状态展示做对，不能现在先写一个单值状态机。

**这一轮真被踩到的五个问题（都修了/记下了）**：
1. **832×480/81 帧解码 OOM**：去噪三步都跑完，TAEHV 解码 `Insufficient Memory`；降到 640×352/81 仍 OOM。改成默认 `--fast-spatial`（半分辨率解码后上采样）后 28.2 秒完成。默认值与提示写进清单，避免下一个人再撞。
2. **清单把 1.3B 的 `--fast-spatial` 写没了**：旧文案说“三个开关只在 5B 入口齐备”，1.3B 的 argparse 里明明有。按 `--help` 复核后纠正；这也是第一条成功基准能跑起来的前提。
3. **`steps` 是假旋钮**：FastMetal 1.3B 走 DMD 固定 3 步，传 `--num-inference-steps 1` 仍打印 3 步；清单删掉 `steps`，不摆一个不生效的参数。
4. **I2V 条件图的路径边界**：相对路径只允许落在本作业目录，`../` 与符号链接 resolve 后再比对；没有真实资产选择器之前，界面提交 I2V 会明确回“需要条件图”。
5. **无头 Chrome 的媒体时钟不推进**：`play()`/seek 都停在 0，但 `<video>` 元数据可读、首帧已画、`error=0`。不把“无头浏览器不播”当产品缺陷；可预览的凭据改为元数据+首帧截图，真机播放器仍用原生控件。

**本轮改到的文件**：`backend/runtime.py`（新）、`backend/runners.py`（新）、`backend/adapters.py`、`backend/worker.py`、`backend/config.py`（未改常量，沿用 JOBS_DIR）、`run.sh`、`frontend/js/navstate.js`、`frontend/js/views/writing.js`、`frontend/js/views/generate.js`、`frontend/css/app.css`、`tests/test_jobs.py`、`tests/test_runners.py`（新）、`盘点/W09_BASELINE.md`（新）、`设计稿.md`、`app/截图/v69-*`。

## 18. W10 完成后回填（2026-11-01）

`pytest tests -q` → **295 passed**（新增 `test_cloud.py` 14 项：清单价与枚举、DeepSeek 成功/401/429/缺密钥、Wan 创建轮询下载/缺 base URL、请求文件无密钥值、worker 端到端写请求 ID 与用量、401 失败仍保留预计成本、默认本地不选云端、本地失败重试仍本地、云端重试继承方式与计价）。路由不变：**72 条业务端点 / 77 条含 health 与四条 auth**。真浏览器 **15 项全过**＋补证 **1 项**（Wan 的 `ratio` 五档与 720P 0.6 元/秒）；真 DeepSeek 调用 2 次（作业 #36/#37），Wan 只跑本地 mock HTTP。图在 `app/截图/v70-cloud-form.png`、`v70-cloud-result.png`、`v70-local-default.png`、`v70-wan-form.png`；官方取数与调用记录在 `盘点/W10_CLOUD.md`。

| 盘点条目 | 现在 |
|---|---|
| W10「接入经许可证、国内可用性、官方 API、价格和密钥方案核验的文本/视频 provider」 | 文本：DeepSeek 官方 API，模型 `deepseek-flash`，OpenAI 兼容 `/chat/completions`；视频：阿里云百炼 Wan 2.7 文生视频，模型 `wan2.7-t2v`，异步任务。两家的模型/参数/价格都按 2026-11-01 官方文档核过才登记 |
| W10「使用配置的国内服务，不新增境外聚合」 | 清单里只有国内 provider；DeepSeek 直连官方域名，Wan 走百炼 Workspace 域名，没有 OpenRouter 等聚合层 |
| W10「单镜头选择 cloud 后显示 provider/model/计价单位/预计成本 + 明确确认」 | 选中云端槽位自动预填：`deepseek-flash` → 百万 Token / 8 元 / 0.002 百万（¥0.02）；Wan 720P → 秒 / 0.6 元 / 5 秒（¥3.00），`ratio` 五档（16:9/9:16/1:1/4:3/3:4）由清单渲染成下拉。确认框默认不勾，服务端仍要求 `user_confirmed_cloud=true` 与一张完整 `estimate` |
| W10「重试继承 original execution mode」 | `jobs.retry()` 复制 `execution_mode`、provider/模型、数量/单价/币种/定价快照；云端新行回到待核对，本地新行仍不计费；原行字段一个不动 |
| W10「默认本地任务不会触发云端调用」 | 视频本地默认 fastmetal；不传执行方式时 `resolve_provider()` 只认 `is_default`。真浏览器里默认视频轨显示「本地 · fastmetal」且计价区隐藏；测试断言作业行 `execution_mode=local` |
| W10「模拟本地失败也不会自动云端兜底」 | `runners.build()` 只按 Job 行冻结的 `execution_mode` 分派；本地失败只落本地失败，重试仍复制本地方式；测试 `test_本地失败重试仍是本地` |
| W10「云端错误/请求 ID/实际用量有日志并脱敏」 | 回执 JSON 的 `request_id` 落 `job.provider_request_id`；实际用量与 HTTP 状态进 `resources_json.provider`；日志写一行「provider 回执」，无密钥值。401/429 错误消息先抹密钥再落库，测试用真串反证 |
| W10「真实调用」 | 作业 #36：请求 ID `a38c360a-034f-4939-96cd-2406c84ad153`，HTTP 200，42 token，延迟 1051ms；作业 #37：`0c578bbc-b7d0-40b3-9972-d992ceac7963`，45 token，延迟 860ms。两次产物是中文正文，日志与结果文件里没有密钥串 |
| W08 边界「实际用量与供应商回执在 W10」 | 已补：`provider_request_id` 列现在有值；实际用量在 `resources_json`。账单状态仍是 `pending_reconciliation`——**实际用量不等于自动结算**，known 仍要人工核对 |
| 01-D-08 价格待复核 | DeepSeek 与 Wan 2.7 已填官方价；其余云端 provider 仍空着。详见 §18 边界 |

**W10 的边界（别误读成“云端全部接通了”）**：
- **Wan 没有真实付费调用**：本机没有 `DASHSCOPE_API_KEY`，也没有带 Workspace 的 base URL，健康状态如实回「缺东西」。Wan 的创建→轮询→下载是用本地 mock HTTP 全链测的，真实网关的字段差异要等有 key 时再做一次。
- **DeepSeek 只接了文本正文**：没有接流式输出、工具调用、JSON mode、v4-pro；`thinking` 只映射到 `{"type": "enabled|disabled"}`。
- **价格是预填上限，不是结算价**：DeepSeek 预填输出忙时 8 元/百万 token，真实输入/输出与缓存命中价不同；Wan 按秒计费但回执里的实际时长可能和提交值不同。两者都只做“预计成本确认单”，账单状态仍是待核对。
- **取消云端作业不等于供应商停止计费**：worker 会 SIGTERM 子进程，但已经创建的任务可能继续跑；Wan 创建成功后会先落一份含 `task_id` 的部分回执，供人工去后台追。
- **Wan 只接 T2V**：I2V、首尾帧、音频没接；T2V 的 `resolution`／`ratio`（16:9/9:16/1:1/4:3/3:4）／`duration`／`seed` 已按官方文档进清单。默认 720P 是成本取舍（官方默认 1080P）。
- **没有自动对账**：实际用量只写进 `resources_json`，不会自动改 `cost_micros` 或把账单状态变 known；那属于后续对账/结算包。

**这一轮真被踩到的问题（都修了/记下了）**：
1. **旧模型名已下架**：DeepSeek 官方现在只有 `deepseek-flash`／`deepseek-v4-pro`，旧 `deepseek-chat`／`reasoner` 不再登记；v4-pro 价格没核到同口径，宁可不填。
2. **云端视频旧测试写死了 `wan-1080p`**：清单换成 `wan2.7-t2v` 后旧模型档不再合法；测试同步改到新档位，并加枚举校验。
3. **请求文件不能带密钥值**：`runners._cloud()` 只写 `key_env` 变量名；测试读请求 JSON 用真串反证。401 响应体里若回显密钥，`cloud_run._redact()` 也会抹掉。
4. **失败也要带请求 ID**：`JobFailure` 加了 `provider_request_id` 字段；Wan 创建成功后立刻写一份部分回执，取消/崩溃时至少还能拿到 `task_id`。
5. **`million_token` 不是自由文本**：单位白名单与前端 `UNITS` 同步加这一档；否则 8 元/百万 token 会写成 0.000008 元的单 token 单价，对账时容易看错。

**本轮改到的文件**：`backend/adapters.py`、`backend/cloud_run.py`（新）、`backend/runners.py`、`backend/worker.py`、`backend/models.py`、`backend/repositories/jobs.py`、`frontend/js/views/generate.js`、`tests/test_cloud.py`（新）、`tests/test_providers.py`、`tests/test_jobs.py`、`tests/test_cost.py`、`tests/test_runners.py`、`盘点/W10_CLOUD.md`（新）、`设计稿.md`、`app/截图/v70-*`。
