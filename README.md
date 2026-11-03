# AI 视频生产平台

2–5 人局域网自部署的 AI 动漫／视频生产平台。后端 FastAPI + SQLite（WAL），前端是原生 ES Module 静态页面（无构建链、不依赖 node），生成作业由独立的 `worker.py` 进程执行（可多开，按资源类并发）。

当前进度：**W00—W18、评审阶段 0＋1 与自动化漫剧产线阶段 A＋B＋C＋D＋E 已走完（设计稿 v0.93）**，`pytest` **619 项全过**；业务端点 132 条，另加 health 与四条 auth 共 137 条。队列、本地与云端 adapter、文档 AI、分镜编辑、文本抽取、审核 gate 与签字（**文档＋产物两档**）、音频台账、项目看板与导出、备份恢复、模型验证五档出处与故障演练都已接线；产线编排（模板／运行／节点／事件四张表、16 种节点、调度节拍住在 worker）与**产线中心页面**（运行／模板／整季）、**成片中心**（FFmpeg 合成、时间线冻结、版本留档、ffprobe 验收、**可选硬字幕**）、**资源类并发与多 worker 心跳**、**首页待办条**（跨模块聚合 `GET /api/workbench`：待签字／失败作业／待核对账单／进行中的运行，只算你参与的在产项目）均已交付。可复算数字以 [文档/发布基线.md](文档/发布基线.md) 为准。

两处仍是**部分完成**，不假装封版：W15 音频只到台账、权限与同队列提交（本机没有 VoxCPM2／ACE-Step／Rhubarb 运行时，不会真的出声）；W17 只有 M4 Air 16GB 上 FastMetal 1.3B 的实测，目标机 M5 Ultra 的基准与部分模型许可／计价仍是 `UNKNOWN`。设定中心只有入口与文案，功能未实现。

产线与成片也要说清证据边界：生成节点**从未跑过真实模型**，全部验证走 mock 适配器与临时库，按实施文档口径不作为真实模型生产验证证据；模板里不能放云端节点（云端要逐镜头确认计费，建 run 时直接 422）。成片链路真跑了 FFmpeg 与 ffprobe，但验收素材是本机临时构造的测试视频，不是真实模型生成的分镜。并发是本机多进程、按 `text／image／video／audio／compose` 分类限流，不是跨机集群。页面不会自动轮询，worker 推进后切回列表重开或刷新才看到最新。十条产线故障演练、阶段 C 页面、阶段 D 成片与阶段 E 整季验收全部实测，见 [文档/黄金样例与故障演练.md](文档/黄金样例与故障演练.md) §2.6—§2.9。

## 目录

```
app/
  run.sh                 启 API / worker / 停止；首次运行自动建 .venv
  backend/               FastAPI 应用、仓储、迁移、独立 worker
    migrations/          SQLite 迁移，启动时自动补到最新
    tests/               pytest 全量测试（不跑模型、无需密钥）
    data/                运行数据（已被 .gitignore 排除，见下）
  frontend/              index.html + js/ + css/ + img/
文档/                   使用手册与三份 as-built 说明书：系统使用、需求概要、系统设计、数据字典（截图在 assets/，导出 HTML 在 导出/）
设计稿.md               产品与架构设计稿（含逐版修订记录）
盘点/                   SPEC_DIFF 回填与各包验收留档（W09—W18）
```

## 快速开始

要求：Python 3.12、Git（文档正文的版本历史依赖它）。macOS 与 Linux 均可；模型运行时（FastMetal／mlx-video／mflux）不是跑平台的前提。

```bash
cd app
./run.sh            # 首次运行会创建 .venv 并装依赖
```

浏览器打开 <http://127.0.0.1:8000>。生成队列要另开一个终端：

```bash
cd app
./run.sh worker     # 独立进程；Ctrl+C 停
```

默认全局并发 1、每类 1（仍是「一次只跑一条」）。要并行可再开一个终端，并显式声明上限：

```bash
./run.sh worker --max-global 2 --max-video 1 --max-compose 1
```

上限是**每个进程**的；多个 worker 通过 SQLite 原子领取，同一条作业只会被一个进程拿走。整季页顶部的 worker 面板会列出每个进程的心跳与当前作业。

停止两者：`./run.sh stop`。

首次启动时库里没有用户，会建超级用户 `superadmin`：

- 设了 `FASTVIDEO_ADMIN_PASSWORD`：初始口令就是它；
- 没设：随机生成 16 位，**只在启动那一次打印到 stderr**。

两种情况第一次登录都会强制改密。口令值不要写进仓库、文档或提交记录。

对局域网开放：`FASTVIDEO_HOST=0.0.0.0 ./run.sh`（默认只监听 `127.0.0.1`）。

## 测试

```bash
cd app/backend
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试用 ASGI 进程内调用，不起端口；数据库、文件、稿件工作区、作业目录全部落在临时目录（`tests/conftest.py` 在 import 前设置环境变量），不会污染 `data/`。假适配器只在测试里开启（`FASTVIDEO_ALLOW_MOCK=1`）。CI 跑的是同一条命令，见 `.github/workflows/tests.yml`。

## 配置

全部走环境变量；代码里没有写死的路径、端口或密钥。

| 变量 | 默认 | 说明 |
|---|---|---|
| `FASTVIDEO_DB` | `app/backend/data/platform.db` | SQLite 库 |
| `FASTVIDEO_FILES` | `app/backend/data/files` | 上传的定妆图等 |
| `FASTVIDEO_WORKSPACE` | `app/backend/data/workspace` | 文档正文的 Git 工作区，可指向已有稿目录 |
| `FASTVIDEO_JOBS` | `app/backend/data/jobs` | 作业日志与产物 |
| `FASTVIDEO_HOST` / `FASTVIDEO_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `FASTVIDEO_ADMIN_PASSWORD` | 空（随机生成） | 首次建超级用户时的初始口令 |
| `FASTVIDEO_LOCK_MINUTES` | `15` | 文档编辑锁时长 |
| `FASTVIDEO_DOC_MAX_BYTES` | `1048576` | 单份正文上限 |
| `FASTVIDEO_SESSION_HOURS` | `12` | 登录会话有效期 |
| `FASTVIDEO_CORS` | 空 | 前端换 nginx 托管时填来源，逗号分隔 |

模型 provider 的密钥**只从进程环境读**，库里只存“用哪个环境变量名”（如 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`），不存值，也不回显到接口、日志或错误消息。示例一律用占位符：

```bash
export DEEPSEEK_API_KEY=YOUR_API_KEY
export ALIYUN_WAN_BASE_URL=https://your_workspace_id.cn-beijing.maas.aliyuncs.com
```

能力与 provider 在「系统设置 · 模型与密钥」里选，库里只存环境变量名。这台机器当前的配置是：文本默认轨 `deepseek/deepseek-flash`（文本没有本地合格路线），视频默认本地 `fastmetal`，云端 Wan 2.7 只在镜头里显式选择并确认计费。新部署要自己选一次 provider，并在进程环境里提供对应密钥；Wan 真实付费调用还需要带 Workspace 的 `ALIYUN_WAN_BASE_URL`。平台不做“本地失败自动转云端”。

## 数据与 Git

`app/backend/data/` 整个目录被忽略，不会进仓库：里面有 `platform.db`（含用户口令哈希与会话）、稿件工作区、作业产物与日志。

- 小说／剧本正文的事实来源是工作区里的 Markdown 文件 + Git 提交；库里只有元数据。
- 每保存一次、或应用一次 AI 改写，就生成一次提交，提交身份是操作的人。
- 备份用脚本，不要只手拷数据库：

```bash
cd app/backend
.venv/bin/python backup.py create
.venv/bin/python backup.py verify <备份目录>
.venv/bin/python backup.py restore <备份目录> --yes
```

`create` 用 SQLite 自己的 `backup()`（WAL 下直接拷文件会漏已提交数据），把库、上传文件、文档工作区、作业产物打成一个目录；manifest 记 sha256、`integrity_check`、`foreign_key_check` 与逐表行数。恢复必须先验证，且会自动留一份 `restore-safety-<stamp>`。备份是整目录快照，不是增量。

提交信息沿用「工作包 + 版本」的风格（例如 `v0.71：W11 文档 AI 操作——…`）。推送前先确认 `pytest` 全绿；需要代理的网络给 git 命令临时加环境变量即可，不要把代理或凭据写进仓库配置。

## 文档入口

- [文档/系统使用说明书.md](文档/系统使用说明书.md)：面向制作、审核与管理员的逐步操作手册
- [文档/需求概要说明书.md](文档/需求概要说明书.md)：产品定位、角色权限、逐页功能需求与截图
- [文档/系统设计说明书.md](文档/系统设计说明书.md)：架构、分层、状态机、132 条端点目录、运维与测试
- [文档/数据字典.md](文档/数据字典.md)：34 张表的字段、外键、索引、枚举与删除级联
- [文档/黄金样例与故障演练.md](文档/黄金样例与故障演练.md)：黄金样例包与八步手册、七条 Job 演练、备份恢复演练、十条产线演练与阶段 C 页面验收（逐条记观测）
- [文档/发布基线.md](文档/发布基线.md)：可复算数字的唯一出处，由 `tools/make_release_baseline.py` 生成，`--check` 是本地与 CI 的提交门禁
- [文档/开发/自动化漫剧产线/01-现状审计报告.md](文档/开发/自动化漫剧产线/01-现状审计报告.md)：产线阶段 A 的现状取证与裁定建议（阶段 B／C 的结论附在文末）
- [文档/导出/](文档/导出/)：以上四份说明书的单文件 HTML，截图已 base64 内嵌，适合直接发送；重新生成用 `python3 tools/build_doc_html.py`
- [设计稿.md](设计稿.md)：产品边界、模块职责、关键决策与逐版修订记录
- [盘点/SPEC_DIFF.md](盘点/SPEC_DIFF.md)：规格与实际实现的逐包差异回填
- [盘点/W17_LICENSE_BENCHMARKS.md](盘点/W17_LICENSE_BENCHMARKS.md)：许可、价格与真机基准
- [盘点/W18_RELEASE.md](盘点/W18_RELEASE.md)：发布清单、备份恢复与故障恢复
- 外部规格包原文（`AI视频生产平台_Qwen开发规格包/`）已随评审收口移出仓库；它是历史输入，当前口径以 `文档/` 与 `盘点/SPEC_DIFF.md` 为准。要看原文从 git 历史取：`git show beebee6:"AI视频生产平台_Qwen开发规格包/README_交付说明.md"`

## 不做的事

当前明确不做：节点画布、自建常驻推理服务、失败自动转云端、把密钥或口令写进提交。产线这一版另有三条：不做 FFmpeg 成片（阶段 D，`export_prepare` 恒为 `skipped`）、不做并发与整季批量（阶段 E，worker 的全局互斥与单行 `worker_state` 一个字段都没动）、不允许模板里出现云端节点。完整清单见设计稿 §6。
