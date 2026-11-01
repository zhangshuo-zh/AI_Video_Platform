# MODEL_CAPABILITIES.md — 本机模型能力实测（W00 盘点）

> **2026-11-01 W09 更新**：FastMetal 1.3B T2V 已从浏览器分镜跑到真 mp4（640×352/81 帧/3 步/`--fast-spatial`，28.2 秒），见 `W09_BASELINE.md`。本页其余数字仍是 W00 当天快照（“零子进程调用”“未跑过”都不再是今天的事实）。

纪律：**没有证据的一律写 UNKNOWN**，不根据模型名推断参数、许可、价格、耗时。取证方式：`$HOME` 下目录扫描（跳过 Library / node_modules / site-packages / .venv 内部）、`git log -1`、`sys.version`/`importlib` 探测、`stat` 权重文件（**只 stat 不打开不移动**）、后端源码 grep。本轮**没有执行任何生成命令**，所以"可运行"指的是原料齐备，不等于跑通过。

## 0. 一句话结论

**八条能力里只有 T2V 一条原料齐备**；I2V／图像／TTS／音乐／口型五条连仓库都没有；文本 LLM 与云视频是"有 key 名不匹配"和"完全空白"。而平台后端**零子进程调用**——所以现在离"第一条 mp4"差的不是 UI，是仓库里那三个还不存在的目录（`adapters/`、`workers/`、`data/jobs/`）。

## 1. 仓库与安装现状

| 工具 | 本机是否存在 | 证据 |
|---|---|---|
| FastVideo / FastMetal | **存在**，git 仓库，remote `github.com/hao-ai-lab/FastVideo`，HEAD `6ded84ee`（2026-10-04），顶层有 `LICENSE`（Apache-2.0 抬头）/`README.md`/`pyproject.toml`；已 editable 装进 `~/FastVideo/.venv`（fastvideo 0.2.1、mlx 0.32.3、torch 2.12.0、mlx_metal） | `/Users/zhangshuo/FastVideo/` |
| mlx-video | **未找到**（无目录、PATH 无、`importlib` 探测 False） | 只有许可证文本一份 |
| mflux | **未找到**（系统 python 与 FastVideo venv 均无） | 同上 |
| VoxCPM2 | **未找到**。`voicestudio 1.0.1` 只在 uv **下载缓存**里，未装进任何 venv，且 VoiceStudio ≠ VoxCPM | `~/.cache/uv/...` |
| ACE-Step 1.5 | **未找到** | 只有许可证文本 |
| Rhubarb | **未找到**（PATH 无、`brew list` 无） | 同上 |
| ComfyUI | **无可运行实例**；`~/pinokio` 框架在但 `api/`、`drive/` 为空 | — |

## 2. 参数核实（只核到 FastVideo 一家，其余无从查起）

- MLX 路线真实存在：`~/FastVideo/README.md:15`（"supports MLX on Apple Silicon with FastMetal-QAD"）、`:69`（`uv pip install -e '.[mlx]'`）。
- 入口：`examples/inference/basic/mlx_wan_prompt_to_video.py`，模型自带 Quickstart 给的调用形态是 `python .../mlx_wan_prompt_to_video.py --model-root … --mlx-checkpoint … --prompt …`（`FastMetal-1.3B-QAD/README.md`）。
- **`--taehv-parallel` 确实存在**：`mlx_wan_prompt_to_video.py:697`（`store_true`，help 写"更快但更吃内存"）——设计稿里那条"`--taehv-parallel` 本机判负"的说法，参数本身是真的。
- 同文件还核实到：`--decode-backend {wan-vae,taehv,taehv-torch}`（`:691`，`:692` 原文提示默认 taehv "has temporal bug"）、`--taehv-source-path`/`--taehv-checkpoint-path`（`:695-696`）、`--height/--width/--num-frames/--num-inference-steps/--seed/--fps/--metrics-json`（`:689`）。
- `fastvideo` CLI 有 `generate/bench/serve` 子命令，但 **cli 目录内 grep `mlx|mps|apple` 零命中** ⇒ CLI 走不走 MLX：**UNKNOWN**。
- mlx-video 的 `--model-type`／`--lightning`：**UNKNOWN（工具不在本机）**。mflux 量化档位：**UNKNOWN（同上）**。规格 01-4.4-J10「可用档必须以机上基准为准」这一条现在**无法回答**。

## 3. 权重盘点（只 stat）

| 路径 | 大小 | 判断 |
|---|---|---|
| `~/FastVideo/FastMetal-1.3B-QAD/`（整目录） | 12 GB，**文件齐全**：`mlx_dit.safetensors` 1.51 GB、fp16 UMT5 五分片 2.45–2.5 GB×4＋1.44 GB、`vae` 507 MB、tokenizer、scheduler | T2V 原料齐 |
| `~/.cache/fastvideo/taehv/taew2_1.pth` | 22.7 MB | VAE 解码备选在 |
| `~/.cache/fastvideo/prompt_embeds/` | 4 组 `.npy` | 提示词缓存，说明**历史上真跑过** |
| `~/FastVideo/output_fastmetal.mp4` | 429,882 B（10-07 生成） | 有过真实出片，但**无耗时/峰值记录** |
| `~/资料/模型存放位置/noobaiXLNAIXL_epsilonPred11Version.safetensors` | 7.1 GB | 图像权重在，**但本机没有 mflux 可承接** |
| `~/AI动漫工作流设计文档/Minimax H3 Draw Things/` | 4 个 H3 相关权重（1.96 GB×2、2.48 GB、0.78 GB）＋ LoRA 182 MB | **无 ComfyUI 实例承接**；H3 许可条款（含地域限制）本轮未复核，维持 UNKNOWN |
| `~/AI-Models/checkpoints｜video-models｜lora` | 全为 **0 B 空目录** | 看着像有、其实没有 |
| `~/AI-Models/tts/piper/zh-CN-huayan-medium.onnx` | 63 MB（配套 `~/AI-Tools/piper-source/`、`piper-native`、espeak-ng/phonemize） | **本机唯一现成的本地 TTS 路线是 piper，不是 VoxCPM2** |
| `~/.ollama/models` | 4.9 GB，manifest `qwen3/8b` | 本地文本模型在，**后端零调用代码** |

## 4. 平台侧的接入面

- **后端零子进程**：`app/backend` 全量 grep `subprocess|Popen|shell=True|os.system|shutil.which|ffmpeg` → 0 命中。机器上有 brew 版 ffmpeg，但平台没引用。
- **provider 表就是十行**：`0003_providers.sql` 的 `CHECK(capability IN ('text','image','video','tts','music'))` × `CHECK(backend IN ('local','cloud'))`，`UNIQUE(capability,backend)`。实读 10 行全部 `provider=''`、`model=''`、`enabled=0` ⇒ **一个都没配**。
- **没有口型槽位**：capability 枚举里没有 `lip_sync`/viseme 这一类。规格 01-4.5 要挂 Rhubarb/viseme，落点要么扩枚举（迁移＋前端表头），要么把口型并入 `tts`——**这是需要用户裁的一条**（见 SPEC_DIFF `01-4.5-F5`）。
- **密钥只从环境变量读**：`repositories/providers.py:13` 拼 `FV_KEY_<PROVIDER>`，`:20` 只回"有没有配"，`models.py:137` 的 `has_key` 永不回传值。现状符合规格 01-4.8.2-5。
- **命名对不上**：本机 shell 里唯一的密钥变量名是 `DEEPSEEK_API_KEY`（定义在 `~/.zshrc`，值未读取），而后端约定要读的是 `FV_KEY_DEEPSEEK`。**即使现在填 deepseek 也读不到 key**。（另注：`~/.zshrc` 只有交互式 shell 读得到，这决定了适配器子进程必须显式继承或改读位置——W09 的坑，先记。）
- **`data/fastvideo.db` 是一个 0 字节空文件**，真库叫 `platform.db`。清理属 W18。

## 5. 基准与耗时

设计稿里那些数字（250s、1.3B@352×640、457s·19.5GiB 等）**在本机找不到任何可复现产物**：`~` 下没有机器生成的 benchmark log/json，只有上游的基准脚本（`fastvideo/benchmarks/mlx_fastwan_bench.py`、`examples/inference/basic/mlx_wan_quant_benchmark.py`）和几份笔记。规格 02-14.4 要求"待实测档不可虚构"——**当前所有耗时/峰值一律按 UNKNOWN 处理**，UI 上那栏就该空着（设计稿 v0.6 已留空，符合）。

## 6. 能力结论表

| 能力 | 规格默认路线 | 本机可运行性 | 依据 |
|---|---|---|---|
| 视频 T2V | FastMetal QAD | **原料齐，可开工**（本轮未实跑，16GB 上未实测） | §1 §2 §3 |
| 视频 I2V | mlx-video | **缺仓库**（连带缺权重） | §1 |
| 图像 | mflux + FLUX 系 | **缺仓库**；权重在但无 runtime | §1 §3 |
| 配音 TTS | VoxCPM2 | **缺仓库**；本机现成的是 piper（口径不同：无字级时间戳那套能力） | §1 §3 |
| 音乐 | ACE-Step 1.5 | **缺仓库、缺权重** | §1 §3 |
| 口型 viseme | Rhubarb | **缺仓库、缺二进制**，且 provider 表**无此槽位** | §1 §4 |
| 文本 LLM | 国内云（必须配） | **未接入**：10 槽全空＋key 变量名不匹配；本地 ollama qwen3:8b 在但无调用代码 | §3 §4 |
| 云视频 | 逐镜头显式勾选 | **UNKNOWN**：无仓库、无密钥、无调用证据 | §4 |

**目标机差异**：本机是 MacBook Air M4 16GB（`sysctl` 确认），规划中的 Mac Studio M5 Ultra 96GB **尚未到手**。所以规格 02-14.4 那批基准（尤其 5B 档、14B 档、峰值内存）现在**测不了也不该测**——W17 整体处于 BLOCKED，不是 laziness。
