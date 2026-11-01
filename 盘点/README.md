# 盘点与验收留档

这个目录只放**证据与历史**，不是当前口径。当前口径看 `../文档/` 下三份 as-built 说明书与 `../设计稿.md`。

## 还留在这里的文件

| 文件 | 用途 |
|---|---|
| `SPEC_DIFF.md` | W00 规格差异表＋W01—W18 逐包回填；写清哪些 done／partial／missing，以及每轮真踩到的问题 |
| `W09_BASELINE.md` | FastMetal 1.3B 在 M4 Air 16GB 上的原始基准、OOM 报错与复现命令 |
| `W10_CLOUD.md` | DeepSeek 与 Wan 2.7 的官方取数、计价口径与边界 |
| `W11_DOC_AI.md` | 文档 AI 六操作的落点、选区规则与真调用记录 |
| `W12_SHOTS.md` | 分镜表字段映射、快照不变与验收记录 |
| `W17_LICENSE_BENCHMARKS.md` | 模型许可、价格、真机基准矩阵；未核到的一律 `UNKNOWN` |
| `W18_RELEASE.md` | W00—W18 状态、备份恢复命令、迁移与故障恢复、发布清单 |

## 已删除的过时快照

2026-11-01 W18 收尾时删掉了 W00 当天的五份 `CURRENT_*` 快照、`MODEL_CAPABILITIES.md`、`资产中心-现状与差异.md` 与 `接口草案-抽取.md`：

- `CURRENT_ARCHITECTURE.md`／`CURRENT_SCHEMA.md`／`CURRENT_API.md`／`CURRENT_PERMISSIONS.md`／`CURRENT_UI.md`：W00 时点的事实，之后 0013—0019 迁移、20 多个端点与十几个界面都已变化，继续留着会与代码冲突。历史仍在 Git 里。
- `MODEL_CAPABILITIES.md`：W00 的八工具盘点，已被 `W17_LICENSE_BENCHMARKS.md` 与 adapter 清单取代。
- `资产中心-现状与差异.md`：W04 改造前的差异报告，已被 `文档/需求概要说明书.md` 的资产中心一节取代。
- `接口草案-抽取.md`：W13 实现时把入参从“路径／文本”改成“按文档 id”，证据复核与候选处理也已经落地；草案口径与代码不一致，留档价值低于误读风险。

**编号诚实声明**：规格 01／02 没有 FR／AC 编号体系；`SPEC_DIFF.md` 里形如 `01-4.1-B3` 的引用键是 W00 盘点为可核对新造的“节号-类别-序号”，不是原文编号。
