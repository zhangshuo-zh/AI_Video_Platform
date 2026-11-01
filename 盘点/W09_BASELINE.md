# W09 本地 adapter 基准（2026-11-01）

这一页只记**实测**：命令、环境、失败形状、成功那一条的字节与耗时。
没有跑过的档位写"未安装"，不拿推算值冒充实测。

## 1. 环境

| 项 | 值 |
|---|---|
| 机器 | MacBook Air M4 · 16GB 统一内存（无风扇） |
| 系统 | macOS 26（Apple Silicon） |
| FastVideo | HEAD `6ded84ee`，虚拟环境 `$FASTVIDEO_FASTMETAL_HOME/.venv` |
| Python / MLX | 3.12.8 / 0.32.3 |
| 权重 | `FastMetal-1.3B-QAD`（12GB，tokenizer + UMT5 + VAE + INT8 DiT 齐全） |
| TAEHV | `taehv/taew2_1.pth`（22.7MB，解码默认档） |
| 平台 | `app/` 静态前端 + FastAPI + 独立 worker（同一台机器） |

mlx-video 与 mflux 在这台机器上**没有安装**（仓库、虚拟环境、转换后的权重都没有）。
这两个 adapter 的命令构造已接好，健康检查会明确回「缺条件」，不会假装能跑；
它们的第一条真实产物要等装好运行时/权重或目标机到手。

## 2. 失败形状（先记，免得后来的人重踩）

### 2.1 832×480 / 81 帧 / 不开 fast-spatial

作业 #33：去噪三步跑完，TAEHV 解码段 OOM：

```
RuntimeError: [METAL] Command buffer execution failed:
Insufficient Memory (00000008:kIOGPUCommandBufferCallbackErrorOutOfMemory)
```

### 2.2 640×352 / 81 帧 / 不开 fast-spatial

作业 #34：同样在 TAEHV 解码段 OOM。说明这台 16GB 机器的瓶颈不是去噪，
是 81 帧一次性解码的峰值；只降分辨率不够。

## 3. 成功基准

**作业 #35**，从浏览器分镜点「生成」提交，走默认本地轨：

| 项 | 值 |
|---|---|
| provider / model | `fastmetal` / `fastmetal-1.3b-qad` |
| 分辨率 / 帧率 / 帧数 | 640×352 / 16fps / 81 帧（5.06 秒） |
| 去噪 | DMD 固定 3 步，`dmd_denoising_steps=1000,757,522` |
| 加速开关 | `--fast-spatial`（320×176 去噪+解码，再 2× 上采样） |
| seed | 1024 |
| 墙钟 | **28.2 秒**（提示词嵌入已缓存；文本编码器首次加载另加约 20 秒，完全冷启动曾到 151 秒） |
| 模型内部 | 提示词编码 0.01s · 去噪 19.09s · 解码出片 2.97s · 总 22.69s |
| 峰值 | 去噪 2.39GB（MLX 报告）· 子进程 RSS 2,087,232KB |
| 子进程 CPU | 8.6 秒 |
| 产物 | 265KB H.264 MP4 |
| SHA-256 | `b73eb88929d57499a3a48a2902e17dfa1ae08fe7190daefeac9c15b3376468b0` |

`ffprobe` 读数：`h264, 640x352, 16/1, 5.0625s, 81 frames`。

证据文件：

- 视频：`app/截图/v69-baseline.mp4`
- 分镜入口：`app/截图/v69-shot.png`
- 预填提交框：`app/截图/v69-prefill.png`
- 成功详情与机时：`app/截图/v69-artifact.png`
- 抽屉里可播放的产物：`app/截图/v69-artifact-video.png`
- 系统设置里的健康状态：`app/截图/v69-provider-health.png`

## 4. 怎么复现

平台里：项目 → 创建集 → 加一镜 → 行内「生成」→ 提交。
等价的 CLI 形状（与 worker 实际用的 argv 同一个顺序）：

```bash
export FASTVIDEO_FASTMETAL_HOME="$HOME/FastVideo"
export FASTVIDEO_FASTMETAL_MODEL="$FASTVIDEO_FASTMETAL_HOME/FastMetal-1.3B-QAD"

"$FASTVIDEO_FASTMETAL_HOME/.venv/bin/python" \
  "$FASTVIDEO_FASTMETAL_HOME/examples/inference/basic/mlx_wan_prompt_to_video.py" \
  --model-root "$FASTVIDEO_FASTMETAL_MODEL" \
  --mlx-checkpoint "$FASTVIDEO_FASTMETAL_MODEL" \
  --prompt "anime style, a lone small boat drifting on a misty mountain lake at dawn, distant pine ridges, soft golden light, cinematic wide shot, wind moving the water" \
  --height 352 --width 640 --num-frames 81 --seed 1024 \
  --fast-spatial \
  --output-path ./35-fastmetal.mp4 \
  --metrics-json ./35-metrics.json
```

平台侧复现要看三件事：生成中心详情里的「实际命令」是 argv 数组；
`resources_json` 里同时有 `cpu_ms`（worker）与 `child_cpu_ms`（模型）；
产物按钮打开后是 5 秒、640×352 的 MP4。

## 5. 这组数字能说明什么、不能说明什么

**能说明**：一条真实 Job 从浏览器分镜入口进入队列、由独立 worker 起子进程、
产物落盘并通过魔数校验、相对路径登记、详情里能播放、机时与模型内部耗时可查；
16GB M4 Air 上 1.3B/81 帧的可用形状是 640×352 + `--fast-spatial`。

**不能说明**：M5 Ultra 96GB 的产能（目标机未到手）；5B/14B 的速度；
mlx-video 与 mflux 的真实产物（本机没有运行时/权重）；
`--fast`（RIFE 插帧）是否更快（未测）；视频画质是否达到交付标准
（fast-spatial 是 320×176 上采样，只能当预览档，成片档要等更大显存或全分辨率解码）。
