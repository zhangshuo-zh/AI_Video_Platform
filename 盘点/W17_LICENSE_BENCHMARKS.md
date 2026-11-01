# W17 许可与真机基准记录

日期：2026-11-01　适用设计稿：v0.72

这份表只回答三件事：**这个档位现在能不能跑、许可是什么、实测是多少**。
没核到的一律写 `UNKNOWN`，不拿推算值冒充实测；价格只记接口清单里已有的口径。

## 1. 视频

| 档位 | 可运行性（本机） | 许可 | 价格口径 | 实测 |
|---|---|---|---|---|
| FastMetal `fastmetal-1.3b-qad` | 需要 `FASTVIDEO_FASTMETAL_HOME` 指向 FastVideo 仓库与权重；本机有仓库 | **UNKNOWN**（FastVideo 仓库的内置档位，权重许可未在本地逐字核到） | 本地不计费 | 有：MacBook Air M4 16GB，640×352×81 帧、3 步 DMD、`--fast-spatial` 开，28.2 秒、峰值 2.39GB（`盘点/W09_BASELINE.md`）。**目标机 M5 Ultra 未实测** |
| FastMetal `fastmetal-5b-qad` | 需要 5B 权重；本机未装 | **UNKNOWN** | 本地不计费 | 无（`UNKNOWN`） |
| mlx-video `wan2.2-i2v-a14b` | 需要 `FASTVIDEO_MLX_VIDEO_HOME` 与权重；本机未装 | **Apache-2.0**（Wan2.2 权重；本仓库未附权重） | 本地不计费 | 无（`UNKNOWN`） |
| mlx-video `wan2.2-t2v-a14b` | 同上 | **Apache-2.0** | 本地不计费 | 无（`UNKNOWN`） |
| 阿里云百炼 `wan2.7-t2v` | 需要 `DASHSCOPE_API_KEY` 与 Workspace；未发生真实付费调用 | 商业 API，无本地权重；以阿里云服务条款为准 | 720P 0.6 元/秒、1080P 1 元/秒（清单，2026-11-01 官方文档） | 无（`UNKNOWN`；只有本地 mock HTTP 全链） |
| 可灵 / Seedance | 未核验，清单只留名字 | **UNKNOWN** | **UNKNOWN** | 无（`UNKNOWN`） |

## 2. 图像

| 档位 | 可运行性（本机） | 许可 | 价格口径 | 实测 |
|---|---|---|---|---|
| mflux `flux1-schnell` | 需要 `FASTVIDEO_MFLUX_HOME`；本机未安装 | **Apache-2.0**（FLUX.1 schnell） | 本地不计费 | 无（`UNKNOWN`） |
| 即梦 / 通义万相（图像） | 未核验 | **UNKNOWN** | **UNKNOWN** | 无（`UNKNOWN`） |

## 3. 文本 / 配音 / 音乐

| 档位 | 可运行性（本机） | 许可 | 价格口径 | 实测 |
|---|---|---|---|---|
| DeepSeek `deepseek-flash` | 已真实调用成功（W11） | 商业 API，以 DeepSeek 服务条款为准 | 输出忙时上限 8 元/百万 Token（清单） | 有两笔真调用：150 token、620/632 ms（`盘点/W11_DOC_AI.md`） |
| 火山方舟 / MiniMax 文本 | 未核验 | **UNKNOWN** | **UNKNOWN** | 无（`UNKNOWN`） |
| VoxCPM2 `voxcpm2` | 需要 `FASTVIDEO_VOXCPM_HOME` 与运行时；本机未装 | **Apache-2.0**（项目双证口径） | 本地不计费 | 无（`UNKNOWN`） |
| ACE-Step `ace-step-1.5` | 需要 `FASTVIDEO_ACE_STEP_HOME`；本机未装 | **MIT** | 本地不计费 | 无（`UNKNOWN`） |
| 火山方舟 / MiniMax 配音 | 未核验 | **UNKNOWN** | **UNKNOWN** | 无（`UNKNOWN`） |

## 4. 停用流程

1. 在 `backend/adapters.py` 把该档位从 `ADAPTERS` 移除，或在系统设置里把槽位设为未启用；
2. 已提交的作业不受影响（Job 行冻的是 provider/model 与参数快照），但重试会按新清单校验并失败；
3. 在 `盘点/SPEC_DIFF.md` 记一条"停用原因 + 日期"，不要只在聊天里说；
4. 界面上的受限模型标注（若启用 H3 一类）必须与许可原文同步，删档时一并删标注。

## 5. 本表没有做的事

- **没有在 M5 Ultra 上跑任何基准**：目标机尚未到位，表里所有 M4 Air 数字都标了机器；
- **没有替未核验的价格与许可补值**：`UNKNOWN` 就是 `UNKNOWN`；
- **没有对 H3 下结论**：H3 不在当前 adapter 清单里，启用前必须重新核一遍许可原文与地域条款。

## 6. 验收口径

- 每个**在清单里可见**的档位都有：可运行性、许可、价格口径、实测或 `UNKNOWN`——本表已覆盖；
- 没有猜测的耗时或峰值；
- 停用流程可执行（改清单 + 关槽位 + 记录）；
- 目标机基准作为剩余开口，等机器到位后补实测，不提前写数字。
