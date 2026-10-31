# CURRENT_ARCHITECTURE.md — 目录与职责（W00 盘点）

取证：`find` 全量列文件＋逐文件读。**当前工作区有未提交改动**（`app/frontend/css/app.css`、`js/store.js`、`js/views/projects.js`、`设计稿.md` 四个文件，git 基线是 `7074bb7 基线：视频生产平台设计稿 v0.54`），本文按**磁盘现状**描述，不区分已提交/未提交。

## 1. 规模（`wc -l` 实数）

后端 18 个 `.py` **2545 行** ＋ 11 个迁移 **306 行**；前端 17 个 `.js` **2788 行** ＋ `app.css` 872 行 ＋ `index.html` 195 行。总计约 6.7k 行。依赖只有 3 行：`fastapi>=0.115`、`uvicorn[standard]>=0.30`、`python-multipart>=0.0.9`——**没有** ORM、没有 gitpython、没有任务队列库，规格 §14 要建的测试体系目前也还没有（见下）。

## 2. 目录树（去掉 `.venv`/`__pycache__`/`data`）

```
平台设计/
├── 设计稿.md                    决策与修订记录（v0.58，58 条修订行）
├── 接口草案-抽取.md              文本抽取的三表六接口草案（尚未实现）
├── AI视频生产平台_Qwen开发规格包/  本次盘点的输入规格（9 份）
├── 盘点/                        本次产出（本目录）
└── app/
    ├── run.sh                   启停脚本：stop 用 pkill -f "uvicorn main:app"；首次跑自动建 venv 装依赖
    ├── backend/
    │   ├── main.py       61     装配：migrate() → ensure_admin() → FastAPI() → 条件 CORS → 挂 6 个 router → 静态托管
    │   ├── config.py     25     全部环境变量出口（见下）
    │   ├── db.py         27     连接工厂：WAL、foreign_keys=ON、timeout=5.0
    │   ├── migrate.py   181     迁移执行器：发现/认领/单事务/重建期关外键/迁移前备份保留 5 份
    │   ├── bootstrap.py  41     首次启动建超管（口令来自环境变量，**有硬编码回退值**，见 CURRENT_PERMISSIONS §5）
    │   ├── security.py   49     scrypt 口令哈希、会话 token 生成
    │   ├── deps.py       55     FastAPI 依赖：current_user / require_permission
    │   ├── access.py     63     项目级判定 ensure()/ensure_by()，不可见 404、级别不足 403
    │   ├── ranks.py      31     账号层级 2/1/0 的唯一出处（Python + SQL 两份同规则）
    │   ├── services.py   33     跨对象事务：delete_project_cascade（删行＋清登记图＋收空目录）
    │   ├── models.py    344     全部 Pydantic 入出参；枚举白名单在这里（Literal 挡 422）
    │   ├── reset_password.py 44 本机急救：忘口令时重置 superadmin
    │   ├── routers/            auth 93 / projects 271 / content 208 / users 186 / settings 47 / system 12
    │   └── repositories/       projects 331 / users 224 / content 171 / providers 48   ← 唯一写 SQL 的地方
    └── frontend/
        ├── index.html   195    壳：前置主题脚本、侧栏/顶栏骨架、登录闸门
        ├── css/app.css  872    :root 与 [data-theme=night] 两块令牌（10-108 / 112-189）＋全部组件
        └── js/
            ├── app.js   494    boot、导航、顶栏自绘项目菜单、主题、命令面板、闸门、账户菜单
            ├── router.js 53     hash 路由：root.innerHTML = await view.render(); view.mount?.(root)
            ├── modules.js 65    九个模块与二级标签声明（含唯一一条 perms 判定）
            ├── navstate.js 14   viewId → 当前二级下标
            ├── store.js   74    唯一状态源（projects/currentId/user/permissions/roles）
            ├── api.js    145    41 个接口函数
            ├── util.js    23    escapeHtml ＋ run()（全站唯一 alert）
            └── views/          projects 777 / system-users 395 / writing 254 / assets 167 /
                                system-config 114 / overview 104 / generate 36 / audio 24 /
                                knowledge 18 / review 19 / index 12
```

## 3. 分层是否守住规格的六条不变量（02 §1）

| 不变量 | 现状 | 证据 |
|---|---|---|
| Router 不写 SQL | **守住了**：`grep -n "SELECT\|INSERT\|UPDATE\|DELETE FROM" routers/*.py` 零命中 | SQL 全在 `repositories/` |
| Router 不互 import | **守住了**（v0.50 把跨对象事务挪进 `services.py` 之后） | `services.py:1` 起 |
| 一个写所有者 | **守住了**：项目/集/镜/资产各只有一个写入口 | 见 CURRENT_API §1 |
| 一个 Job 表/队列/成本口径 | **不存在这条链**（无 job 表、无 worker、无成本列） | CURRENT_SCHEMA §4 |
| 一个 Review 写入口 | 不存在 | 同上 |
| 正文不进 SQLite | **暂时真空**：还没有正文，也没有文件系统读写与 Git 代码 | `grep subprocess\|git` 后端零命中 |

## 4. 进程模型

**只有一个进程**：`run.sh` 起 uvicorn，同一个进程既发 `/api` JSON 又托管静态前端。没有 worker、没有子进程、没有任务表。规格 D-01 定的"独立 worker 子进程"目前**不是冲突而是空白**——因为连同进程后台 worker 都还没有，W07 落地时可以直接按目标形态建，不必先记差异再迁移。

## 5. 环境变量出口（`config.py` 逐字）

| 变量 | 默认 | 语义 |
|---|---|---|
| `FASTVIDEO_DB` | `backend/data/platform.db` | SQLite 主库 |
| `FASTVIDEO_FILES` | `backend/data/files` | 上传图根目录，实际落 `files/<asset_id>/<uuid>.<ext>` |
| `FASTVIDEO_HOST` | `127.0.0.1` | 注释明确警告：一期没做登录前别开 `0.0.0.0`（现在已有登录，注释过期，属 W18 文档项） |
| `FASTVIDEO_PORT` | `8000` | |
| `FASTVIDEO_SESSION_HOURS` | `12` | 会话小时数 |
| `FASTVIDEO_CORS` | 空 | 逗号分隔白名单，空＝不注册中间件 |
| `FASTVIDEO_ADMIN_USER` / `_DISPLAY` / `_PASSWORD` | superadmin / 超级管理员 / **硬编码回退值** | 仅首次启动用 |
| `FV_KEY_<PROVIDER>` | — | 密钥唯一来源，`providers.py:13` 按 provider 名拼，只回"有没有配" |

规格 02 §13.1 列的六个变量名与现状**完全一致**（`FASTVIDEO_*` 前缀沿用，不要另建同义配置）。

## 6. 与规格 02 §2.2 目标目录的对照

| 规格目标 | 现状 | 处理 |
|---|---|---|
| `backend/adapters/` | 无 | W06/W09 新建 |
| `backend/workers/` | 无 | W07 新建 |
| `backend/services.py` | 有（33 行，只有一个函数） | 直接扩，不另建 |
| `backend/access.py` / `ranks.py` / `migrate.py` / `bootstrap.py` / `reset_password.py` | **五个全有，名字一致** | 不动 |
| `backend/repositories/`、`routers/` | 有，结构一致 | 不动 |
| `data/workspace/`（正文与 Git 根） | 无 | W05 新建，需要一个新的 `FASTVIDEO_WORKSPACE` 配置项 |
| `data/jobs/<id>/outputs｜logs` | 无 | W07/W08 新建 |
| 前端 `views/` 文件划分 | 一致（`knowledge.js`/`system-config.js` 命名也符合 01-3.1-R） | 不动 |

**结论**：目标结构与现状**不冲突**，缺的都是新目录，不需要 `CURRENT_TO_TARGET_FILE_MAP.md`（规格 02 §2.2 只在文件名不一致时才要那份）。

## 7. 三条"完全没有"的基础设施（W01 的靶子）

1. **零自动化测试**：`find app -name "test_*.py" -o -name "*_test.py" -o -type d -name tests` 无命中，`requirements.txt` 里也没有 pytest。规格 02 §14 的四层测试（单元/API 集成/真浏览器/真机基准）**一层都没有**，历次验证全靠一次性脚本＋CDP 手工量（本轮盘点用的临时脚本已全部删除，未入库）。
2. **零适配器**：`app/backend` 里 `subprocess|Popen|shell=True|os.system|shutil.which|ffmpeg` 全部零命中——平台目前**不启动任何外部进程**。
3. **`app/截图/` 目录**：里面是历次 UI 验证截图，不在版本控制的源码结构里，规格目标结构中也没有这一项（W18 归档时决定去留）。
