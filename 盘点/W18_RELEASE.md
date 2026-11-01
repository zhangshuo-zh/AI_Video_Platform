# W18 稳定性、备份与发布状态

日期：2026-11-01　设计稿：v0.73（W00—W18）

## 1. 功能状态总表

| 工作包 | 状态 | 说明 |
|---|---|---|
| W00 规格与盘点 | DONE | 当时的 `CURRENT_*` 快照已在 W18 收尾时删除（历史在 Git），由 `文档/` 三份 as-built 说明书取代 |
| W01 基础设施与守卫 | DONE | 路由守卫表、权限双门、错误出口 |
| W02 通用 UI | DONE | 403/404 分流、危险确认、窄屏、双主题 |
| W03 项目中心 | DONE | 一页到底、批量、归档、成员与转移 |
| W04 资产中心 | DONE | 列表＋详情、全局池、定妆图、引用保护 |
| W05 文档文件/Git/锁 | DONE | 元数据在库、正文在工作区、版本在 Git |
| W06 Provider 配置 | DONE | 清单驱动、健康检查、密钥只存变量名 |
| W07 Job 队列 | DONE | 八值状态机、租约、取消、重试、崩溃恢复 |
| W08 生成中心与成本 | DONE | 队列、详情抽屉、成本账、参数模板 |
| W09 本地 adapter | DONE | FastMetal 实测；mlx-video/mflux 命令已接、缺运行时 |
| W10 云端 adapter | PARTIAL | DeepSeek 真调用；Wan 只有本地 mock HTTP，未付费实测 |
| W11 文档 AI | DONE | 六操作、diff 预览、应用落 Git；真 DeepSeek 闭环 |
| W12 分镜表 | DONE | 整体编辑、只读引用、每镜作业、快照不变 |
| W13 文本抽取 | DONE（代码） | 候选绑定文档版本与原文证据；真模型只走了本地假 HTTP，未付费跑真实抽取 |
| W14 审核中心 | DONE | 三条确定性规则、版本绑定、按锚点签字；未接外部校验器 CLI |
| W15 音频中心 | PARTIAL | 素材台账、关联集/镜、同队列提交与产物回抄已完成；VoxCPM2/ACE-Step 的 CLI 运行时与 Rhubarb 口型未接 |
| W16 看板与导出 | DONE | 只读聚合、CSV/Markdown 导出 |
| W17 许可与基准 | PARTIAL | 清单内档位逐条记录；目标机 M5 Ultra 未到位，所有目标机速度均为 `UNKNOWN` |
| W18 稳定性与发布 | DONE（本文件） | 备份/校验/恢复脚本、迁移与外键检查、发布说明 |

**标 PARTIAL 的三项不是"忘了做"，而是缺少本机外部条件**：Wan 付费视频、VoxCPM2/ACE-Step/Rhubarb 运行时、M5 Ultra 实机。平台在这些条件下仍会诚实拒绝或标 `UNKNOWN`，不伪造成功。

## 2. 备份与恢复

```bash
cd app/backend
.venv/bin/python backup.py create                 # 默认 data/backups/<时间>/
.venv/bin/python backup.py verify <备份目录>
.venv/bin/python backup.py restore <备份目录> --yes
```

备份内容：

- `platform.db`：用 SQLite `backup()` 取一致快照（不是 cp，WAL 下 cp 会漏数据）；
- `files/`：资产定妆图；
- `workspace/`：Markdown 正文与 Git 历史；
- `jobs/`：日志与产物。

恢复前三件事：先 `verify`（sha256、integrity_check、foreign_key_check、文件计数），
必须显式 `--yes`，并自动把当前数据另存到 `data/restore-safety-<时间>/`。

## 3. 升级与迁移

- 启动时 `migrate()` 自动补到最新版；一个迁移一个事务，失败不落半张表；
- 迁移前自动备份整库，最多保留 5 份；
- 当前迁移到 `0019_audio.sql`；
- 升级后自检：`PRAGMA foreign_key_check` 为空、`schema_version` 等于代码里的最大迁移号。

## 4. 故障恢复

| 现象 | 先看哪里 | 处理 |
|---|---|---|
| 队列不动 | 顶栏 worker 在线状态、`data/jobs/<id>/logs/run.log` | `./run.sh worker` 重启；崩溃的 running 会在下一轮恢复成 failed(worker_lost) |
| 文档保存 409 | 编辑器冲突条 | 对照两份；「用我这份覆盖」要再点一次；不要手工删库行 |
| 文档保存 423 | 锁持有人与到期时间 | 等对方保存或过期；不要改系统时间 |
| 磁盘满 | `df -h`、`data/jobs` | 先备份再清旧 jobs；保存会回滚并另存未应用正文 |
| 备份恢复 | `backup.py verify` | 校验不过绝不恢复；恢复会自动留 safety 目录 |

## 5. 发布前检查

1. `pytest -q` 全绿；
2. 真浏览器打开六个模块与登录页，无 JS 报错；
3. `./run.sh` 与 `./run.sh worker` 都能起；`./run.sh stop` 两样都停；
4. 备份 → 校验 → 恢复演练一次（在副本上做，不碰生产数据）；
5. 密钥只在进程环境，仓库里没有任何真实值；
6. README、三份说明书、`盘点/W17_LICENSE_BENCHMARKS.md` 与当前版本一致。
