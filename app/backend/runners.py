"""本地 adapter 的命令构造与产物校验（W09）。

这一层只回答两个问题：**用什么 argv 起进程**、**跑完后什么算成功**。
它不写数据库、不写 Job 状态——那些仍由 worker.py 统一收尾。
一处例外要写明（W-B 起）：本机服务型引擎需要读 `engine_instance` 里那一行
**配置**（端点），走的是 `engines.probe()`，只读不写。为什么不干脆把端点
塞进 job 参数：那等于让客户端能指定"这条作业往哪台机器发"，而端点是管理员
配的设施，不是作业输入。

四条硬约束：

1. **argv 是数组**，绝不拼 shell 字符串。提示词里带引号、分号、`$(...)`
   都只是普通字符；`shell=False` 是这里的默认，也是唯一写法。
2. 每条命令必须能解释清楚"为什么这么填"：参数来自 adapters.py 的白名单，
   路径来自 runtime.py 的探测，产物落在 `data/jobs/<id>/outputs/`。
3. 输入图两档：相对路径只认 Job 自己的目录；绝对路径只认平台自己产的作业产物
   （`data/jobs/<作业号>/…`，见 `_is_platform_artifact`）。产线会把上游关键帧
   注入进来，那一档必须放行，否则 video 节点自己人被自己的闸门挡住；
   但依然不接受"界面随手填一个 /etc/passwd"——I2V 的真实资产选择器在 W12。
4. 产物要验真：mp4 看 ftyp、png 看魔数、文件非空。进程返回 0 不代表
   它真的写了东西（磁盘满、脚本提前 return 都会留下 0 字节文件）。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import adapters
import engines
import runtime
from config import JOBS_DIR
from paths import resolve_within, within

CLOUD_RUNNER = Path(__file__).resolve().parent / "cloud_run.py"
ENGINE_HTTP_RUNNER = Path(__file__).resolve().parent / "engine_http_run.py"
LLM_RUNNER = Path(__file__).resolve().parent / "llm_http_run.py"

class RunnerUnavailable(Exception):
    """运行时缺东西：解释器／入口／权重／模型目录。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class RunSpec:
    runner: str
    argv: list[str]
    cwd: Path
    expected: list[Path]
    media: str                    # video / image / audio / other
    meta: dict = field(default_factory=dict)
    # 有些执行器自己决定文件名（mlx-audio 的 TTS 会按分段数命名 000/001…），
    # 这时"预期产物"只能事后按模式找，不能假装我们预先知道文件名。
    # 空字符串＝仍按 `expected` 逐条要求存在。
    expected_glob: str = ""

def _need_runtime(probe) -> None:
    if probe.state != "ok":
        raise RunnerUnavailable(probe.detail)


def _spec_default(job, model_code: str, name: str, fallback):
    """作业参数里没写这一项时，回落到清单里的默认值。

    为什么要回落：产线节点提交的作业 `params` 是空的（模板不写死参数，
    `pipeline._submit_job_node` 只带模板 input 里真有的那几项）。而清单给
    FastMetal 1.3B 标的 `fast_spatial` 默认是 True——那是 W09 在 16GB 上的实测结论
    （640×352×81 不开 fast-spatial 解码就 OOM）。不回落等于把这条实测丢掉：
    作业会以"模型跑崩了"的样貌失败，人看不出是自己没填参数还是平台漏了默认。
    规格仍以 `adapters.py` 为唯一来源，这里只照读一份数字，不另写第二份。
    """
    adapter = adapters.find(job["capability"], job["execution_mode"],
                           job["provider_code"])
    if adapter is None:
        return fallback
    for model in adapter.models or ():
        if model.code != model_code:
            continue
        for param in model.params or ():
            if param.name == name and param.default is not None:
                return param.default
    return fallback


def _flag(params: dict, name: str, job, model_code: str) -> bool:
    """布尔开关：显式给了就照给，没给就照清单默认。"""
    value = params.get(name)
    if value is None:
        value = _spec_default(job, model_code, name, False)
    return bool(value)


def _mp4_path(outputs: Path, job_id: int, label: str) -> Path:
    return outputs / f"{job_id}-{label}.mp4"


def _metrics_path(outputs: Path, job_id: int) -> Path:
    return outputs / f"{job_id}-metrics.json"


def _fastmetal(job, params: dict, outputs: Path, model_code: str) -> RunSpec:
    probe = runtime.probe_fastmetal(model_code)
    _need_runtime(probe)
    v = probe.values
    is_5b = model_code == "fastmetal-5b-qad"
    out = _mp4_path(outputs, job["id"], "fastmetal")
    metrics = _metrics_path(outputs, job["id"])
    seed = int(params.get("seed", 1024 if not is_5b else 1234))
    frames = int(params.get("frames", 81 if not is_5b else 121))
    width = int(params.get("width", 640 if not is_5b else 832))
    height = int(params.get("height", 352 if not is_5b else 448))

    if is_5b:
        argv = [v["python"], v["script"],
                "--mlx-checkpoint", v["model"],
                "--text-encoder-root", v["model"],
                "--vae-root", str(Path(v["model"]) / "vae"),
                "--prompt", job["prompt_snapshot"],
                "--height", str(height), "--width", str(width),
                "--num-frames", str(frames), "--seed", str(seed),
                "--output-path", str(out), "--metrics-json", str(metrics)]
        if _flag(params, "fast", job, model_code):
            argv.append("--fast")
        if _flag(params, "fast_spatial", job, model_code):
            argv.append("--fast-spatial")
        if _flag(params, "refine", job, model_code):
            argv.append("--refine")
    else:
        argv = [v["python"], v["script"],
                "--model-root", v["model"], "--mlx-checkpoint", v["model"],
                "--prompt", job["prompt_snapshot"],
                "--height", str(height), "--width", str(width),
                "--num-frames", str(frames), "--seed", str(seed),
                "--output-path", str(out), "--metrics-json", str(metrics)]
        if _flag(params, "fast", job, model_code):
            argv.append("--fast")
        if _flag(params, "fast_spatial", job, model_code):
            argv.append("--fast-spatial")

    # 这三项只在作业**显式给了值**时才下发：没给就沿用入口脚本自己的默认，
    # 于是这道之前提交的作业 argv 一个字节都不变。
    # 为什么不像 fast_spatial 那样回落清单默认：那个默认是 16GB 的 OOM 实测，
    # 不回落会跑出崩掉的作业；而 fps／factor 的脚本默认本来就是可用值，
    # 回落只会让老作业的 argv 凭空多出几个参数，把"旧作业可复现"这条弄脏。
    for name, flag, cast in (("fps", "--fps", int),
                             ("fast_factor", "--fast-factor", int),
                             ("fast_sharpen", "--fast-sharpen", float)):
        if params.get(name) is not None:
            argv += [flag, str(cast(params[name]))]

    return RunSpec(
        runner="fastmetal", argv=argv, cwd=outputs, expected=[out], media="video",
        meta={"adapter": "fastmetal", "model": model_code,
              "model_dir": v["model"], "python": v["python"],
              "seed": seed, "width": width, "height": height, "frames": frames,
              "generation_steps": 3,
              "metrics_path": str(metrics)})


def _job_dir_root(job_id) -> Path:
    """本作业目录（绝对、已 resolve）。"""
    return (JOBS_DIR / str(job_id)).resolve()


def _is_platform_artifact(target: Path) -> bool:
    """这个绝对路径是不是**平台自己产出来的作业产物**。

    判据只有两条，都不查库：落在 `data/jobs/` 底下，且第一层目录名是作业号
    （纯数字）。够挡住"界面随手填一个 /etc/passwd"，也够挡住有人往
    `data/jobs/` 旁边丢一个目录再引用它。

    为什么需要这第二档：产线会把上游关键帧的产物注入 `image` 参数
    （`pipeline._upstream_image`），那个文件在**别的作业目录**里。原来只认
    "本作业目录"，于是本机接上 mlx-video 之后，每个要条件图的 video 节点
    都会在起跑前被判"条件图路径越出作业目录"——不是权限问题，是自己人
    被自己的闸门挡住。跨项目读产物比读全盘文件低两个量级，而 W12 的资产
    选择器接上之后这一档会再收一次（届时的判据是"这个 job_id 属于本项目"，
    那要查库，不归这一层）。
    """
    try:
        rel = target.relative_to(JOBS_DIR.resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0].isdigit()


def _mlx_video(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mlx_video()
    _need_runtime(probe)
    v = probe.values
    image = (params.get("image") or "").strip()
    if not image:
        raise RunnerUnavailable("I2V 需要条件图；资产选择器接上之前不能从界面提交")
    image_path = Path(image).expanduser()
    root = _job_dir_root(job["id"])
    if image_path.is_absolute():
        # 绝对路径以前直接放行——文件头的第 3 条硬约束写的是"不接受界面随手填
        # 一个 /etc/passwd"，代码却没执行它。绝对路径照样要过边界：worker 是拿
        # 这台机器的权限起进程的，任何能提交作业的人都能借它读全盘的文件。
        target = image_path.resolve()
    else:
        # 相对路径只允许落在本作业目录里；resolve 之后再做一次边界比对，
        # 挡住 ../ 和符号链接绕出去。
        target = resolve_within(root, str(image_path))
    if target is None or not (within(root, target) or _is_platform_artifact(target)):
        raise RunnerUnavailable("条件图路径越出作业目录")
    if not target.is_file():
        raise RunnerUnavailable(f"条件图不存在：{target.name}")

    out = _mp4_path(outputs, job["id"], "mlx-video")
    seed = int(params.get("seed", 42))
    frames = int(params.get("frames", 81))
    width = int(params.get("width", 1280))
    height = int(params.get("height", 704))
    steps = int(params.get("steps", 4))
    argv = [v["python"], "-m", "mlx_video.wan_2.generate",
            "--model-dir", v["model"],
            "--prompt", job["prompt_snapshot"],
            "--image", str(target),
            "--height", str(height), "--width", str(width),
            "--num-frames", str(frames), "--steps", str(steps),
            "--seed", str(seed), "--output-path", str(out)]
    # Lightning LoRA 是四步档的关键；有就挂，没有就按模型默认步数跑。
    # 这两个值不是用户参数，路径只从环境读，避免把任意本地文件喂给加载器。
    for key, flag in (("FASTVIDEO_MLX_VIDEO_LORA_HIGH", "--lora-high"),
                      ("FASTVIDEO_MLX_VIDEO_LORA_LOW", "--lora-low")):
        value = (os.environ.get(key) or "").strip()
        if value:
            argv.extend([flag, value, "1"])
    return RunSpec(
        runner="mlx-video", argv=argv, cwd=outputs, expected=[out], media="video",
        meta={"adapter": "mlx-video", "model": job["model_code"],
              "model_dir": v["model"], "python": v["python"],
              "seed": seed, "width": width, "height": height, "frames": frames,
              "steps": steps, "image": str(target)})


def _mflux(job, params: dict, outputs: Path) -> RunSpec:
    probe = runtime.probe_mflux()
    _need_runtime(probe)
    v = probe.values
    out = outputs / f"{job['id']}-mflux.png"
    seed = int(params.get("seed", 42))
    width = int(params.get("width", 1024))
    height = int(params.get("height", 1024))
    steps = int(params.get("steps", 4))
    # 权重用社区转好的 Q4 档，不用 `--model schnell` 拉官方仓库：
    #   1) 官方 black-forest-labs/FLUX.1-schnell 是 gated 仓库，worker 没有 HF token 就 401；
    #   2) 官方整包 33.7GB，且 `--quantize 8` 在 16GB 机器上装不下。
    # Q4 是同一套 FLUX.1-schnell 权重的 MLX 量化转换（Apache-2.0），9.6GB，
    # 量化级别已经写进权重，所以不能再传 `--quantize`（传了会和已存的量化打架）。
    # 2026-11-05 在 M4 Air 16GB 实测 512×512、4 步：墙钟 102s、MLX 峰值 9.97GB、
    # 进程峰值 footprint 12.3GB；1024×1024 未测，按 512 的余量不要在这台机器上默认跑。
    argv = [v["cli"], "--model", "mflux-community/flux-1-schnell-mflux-q4",
            "--base-model", "schnell",
            "--prompt", job["prompt_snapshot"],
            "--steps", str(steps), "--seed", str(seed),
            "--width", str(width), "--height", str(height),
            "--output", str(out)]
    return RunSpec(
        runner="mflux", argv=argv, cwd=outputs, expected=[out], media="image",
        meta={"adapter": "mflux", "model": job["model_code"],
              "cli": v["cli"], "seed": seed,
              "width": width, "height": height, "steps": steps})


# ---------- 本机常驻服务型引擎（W-B：Draw Things）----------
# 与上面几档的区别只在"怎么把活儿交出去"：这里是往一个已经在跑的本地服务
# 发一次 HTTP，产物从响应体的 base64 里落盘。请求与落盘都在子进程
# （`engine_http_run.py`）里做，worker 侧沿用 `_pump` 那套超时与取消，
# 长不出第二套执行封装。
DRAW_THINGS_PATH = "/sdapi/v1/txt2img"


def _draw_things(job, params: dict, outputs: Path, model_code: str) -> RunSpec:
    # 起跑前现查一次（fresh）：worker 可能刚从队列里领到一条几分钟前排的活，
    # 拿 15 秒缓存里的端点去发请求，会把"换了机器"这件事静默地漏过去。
    status = engines.probe("draw_things", fresh=True)
    if status.state != "ok":
        # unknown（没登记实例）与 missing（登记了但连不上）都由 engines 分好档，
        # 这里只转述，不再各写一套"不可用"的措辞。
        raise RunnerUnavailable(f"Draw Things 现在不能用：{status.detail}")
    endpoint = status.values.get("endpoint") or ""
    out = outputs / f"{job['id']}-draw_things.png"
    receipt = outputs / f"{job['id']}-draw-things-receipt.json"
    request_path = outputs / f"{job['id']}-draw-things-request.json"

    body = {"prompt": job["prompt_snapshot"],
            "width": int(params.get("width", 1024)),
            "height": int(params.get("height", 1024)),
            "steps": int(params.get("steps", 16))}
    negative = str(params.get("negative_prompt") or "").strip()
    if negative:
        body["negative_prompt"] = negative
    if params.get("guidance_scale") is not None:
        body["guidance_scale"] = float(params["guidance_scale"])
    if params.get("seed") is not None:
        body["seed"] = int(params["seed"])

    request = {"endpoint": endpoint, "path": DRAW_THINGS_PATH, "body": body,
               "output": str(out), "receipt": str(receipt), "timeout": 600}
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(ENGINE_HTTP_RUNNER), str(request_path)]
    return RunSpec(
        runner="draw_things", argv=argv, cwd=outputs, expected=[out], media="image",
        meta={"adapter": "draw_things", "model": model_code, "endpoint": endpoint,
              "engine_model": status.values.get("model") or "",
              "request_path": str(request_path), "result_path": str(receipt),
              "body": body})



# ---------- 文本：本机常驻 LLM 服务（A4：Ollama）----------
# 与 draw_things 同族（端点住 `engine_instance`，健康由 `engines.probe()` 现查），
# 但**不过** `_service_engine_gate` 那道"当前模型对不对"的核对：
# 这一族的模型名是每次请求里带的，不存在"服务自己选着另一档"这件事。
# 分族的那一行判断在 `engines.MODEL_GATED_ENGINES`，理由写在那里。
def _ollama(job, params: dict, outputs: Path, model: str) -> RunSpec:
    # 同 _draw_things：起跑前现查，不吃列表那一层给页面用的短缓存。
    status = engines.probe("ollama", fresh=True)
    if status.state != "ok":
        raise RunnerUnavailable(f"本机模型服务现在不能用：{status.detail}")
    endpoint = status.values.get("endpoint") or ""
    out = outputs / f"{job['id']}-ollama.md"
    request_path = outputs / f"{job['id']}-ollama-request.json"
    receipt = outputs / f"{job['id']}-ollama-receipt.json"

    request = {"endpoint": endpoint, "path": "/api/chat", "provider": "ollama",
               "model": model, "prompt": job["prompt_snapshot"],
               "params": {"max_tokens": params.get("max_tokens"),
                          "temperature": params.get("temperature"),
                          "think": params.get("think")},
               "output": str(out), "receipt": str(receipt), "timeout": 300}
    # 计划类请求要的是**结构化输出**：提示词里写着"只输出一个 JSON 数组"。
    # 那一句是给人读的约束，模型不一定守；`format` 才是服务端会守的那一道。
    # 所以这里按提示词的特征把它补上——补的依据是"这段提示词在要 JSON"，
    # 不是"这个 provider 大概支持 JSON"，后者是猜。
    prompt = job["prompt_snapshot"] or ""
    if "JSON 数组" in prompt and "只输出一个 JSON 数组" in prompt:
        request["format"] = {"type": "array", "items": {"type": "object"}}
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(LLM_RUNNER), str(request_path)]
    return RunSpec(
        runner="ollama", argv=argv, cwd=outputs, expected=[out], media="text",
        meta={"adapter": "ollama", "model": model, "endpoint": endpoint,
              "request_path": str(request_path), "result_path": str(receipt),
              "resident_models": status.values.get("models") or [],
              "formatted": request.get("format") is not None})

# ---------- 音频两腿（VoxCPM2 配音 / ACE-Step 1.5 音乐）----------
# 权重都走 mlx-community 的量化转换，不拉官方整包：
#   openbmb/VoxCPM2 主文件 4,580,080,592B（卡面 gated=false、apache-2.0），
#   MLX 8bit 版 3.2GB 级、由 `mlx-audio` 直接跑；ACE-Step 1.5 的 MLX 4bit 版
#   卡面写 2.2GB 主模型＋0.7GB VAE＋2.4GB 文本编码器。
# 这台 16GB 无风扇机器上"整包官方权重＋bf16"是装不下的，量化档才是可用档。
VOXCPM_MLX_MODEL = "mlx-community/VoxCPM2-8bit"
ACE_STEP_MLX_MODEL = "mlx-community/ACE-Step1.5-MLX-4bit"
# `mlx_audio.music.generate` 的 --lyrics/--lyrics-file 是必填互斥组：
# 没有歌词也要给一段结构标记，否则子进程直接 argparse 退出。
ACE_STEP_INSTRUMENTAL = "[inst]"


def _voxcpm(job, params: dict, outputs: Path) -> RunSpec:
    """配音：一条台词 → 一个 wav（可能分段，文件名由执行器决定）。

    产物用 `expected_glob` 事后找，不是偷懒：`mlx_audio.tts.generate` 在
    单段时写 `<prefix>.wav`、多段时写 `<prefix>_000.wav`、`_001.wav`……
    分段数只有跑完才知道。找不到任何文件就抛，绝不写半条成功。
    """
    probe = runtime.probe_mlx_audio("tts")
    _need_runtime(probe)
    text = (job["prompt_snapshot"] or "").strip()
    if not text:
        raise RunnerUnavailable("这条配音作业没有可朗读的文本")
    out_dir = outputs / ("%s-tts" % job["id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    speed = params.get("speed")
    if speed is None:
        speed = _spec_default(job, job["model_code"], "speed", 1.0)
    argv = [probe.values["cli"], "--model", VOXCPM_MLX_MODEL,
            "--text", text, "--speed", str(float(speed)),
            "--output_path", str(out_dir), "--file_prefix", str(job["id"]),
            "--audio_format", "wav", "--verbose"]
    instruct = str(params.get("instruct") or "").strip()
    if instruct:
        argv += ["--instruct", instruct]
    return RunSpec(
        runner="voxcpm2", argv=argv, cwd=out_dir, expected=[], media="audio",
        expected_glob="%s*.wav" % job["id"],
        meta={"adapter": "voxcpm2", "model": job["model_code"],
              "cli": probe.values["cli"], "hf_model": VOXCPM_MLX_MODEL,
              "speed": float(speed), "instruct": instruct})


def _ace_step(job, params: dict, outputs: Path) -> RunSpec:
    """音乐：一句风格描述（＋可选歌词）→ 一个 wav。

    `duration`／`steps`／`seed` 都照清单默认回落，理由与 FastMetal 那条一样：
    产线节点提交的作业 params 是空的，不回落就等于把清单里的默认丢掉。
    """
    probe = runtime.probe_mlx_audio("music")
    _need_runtime(probe)
    caption = (job["prompt_snapshot"] or "").strip()
    if not caption:
        raise RunnerUnavailable("这条音乐作业没有风格描述")
    out = outputs / ("%s-music.wav" % job["id"])
    model = job["model_code"]
    duration = int(params.get("duration")
                   or _spec_default(job, model, "duration", 30))
    steps = int(params.get("steps") or _spec_default(job, model, "steps", 27))
    seed = params.get("seed")
    argv = [probe.values["cli"], "--model", ACE_STEP_MLX_MODEL,
            "--caption", caption,
            "--lyrics", str(params.get("lyrics") or "").strip() or ACE_STEP_INSTRUMENTAL,
            "--duration", str(duration), "--steps", str(steps),
            "--output", str(out), "--verbose"]
    if seed is not None:
        argv += ["--seed", str(int(seed))]
    return RunSpec(
        runner="ace-step", argv=argv, cwd=outputs, expected=[out], media="audio",
        meta={"adapter": "ace-step", "model": job["model_code"],
              "cli": probe.values["cli"], "hf_model": ACE_STEP_MLX_MODEL,
              "duration": duration, "steps": steps,
              "lyrics": str(params.get("lyrics") or "").strip() or ACE_STEP_INSTRUMENTAL})



def _cloud(job, params: dict, outputs: Path, adapter) -> RunSpec:
    """云端：把一次调用写成请求 JSON，交给 cloud_run.py 子进程。

    argv 里只有请求文件路径；密钥值不进来，日志里也没有可泄露的东西。
    base URL 一律从环境变量读，清单里只留变量名。
    """
    key_env = adapters.resolved_key_env(adapter.code, adapter.capability, "cloud")
    if not adapters.key_present(key_env):
        raise RunnerUnavailable(f"环境变量 {key_env} 没有值；云端作业不能起")

    capability = job["capability"]
    if capability == "text":
        ext, media = "md", "text"
    elif capability == "video":
        ext, media = "mp4", "video"
    else:
        raise RunnerUnavailable(f"{capability} 云端执行还没有接线")

    request_path = outputs / f"{job['id']}-cloud-request.json"
    result_path = outputs / f"{job['id']}-cloud-result.json"
    out = outputs / f"{job['id']}-{adapter.code}.{ext}"
    request = {
        "provider": adapter.code,
        "capability": capability,
        "model": job["model_code"],
        "prompt": job["prompt_snapshot"],
        "params": params,
        "key_env": key_env,
        "output": str(out),
        "result": str(result_path),
    }
    if adapter.code == "wan":
        if not (os.environ.get("ALIYUN_WAN_BASE_URL") or "").strip():
            raise RunnerUnavailable(
                "没有配置 ALIYUN_WAN_BASE_URL（要带 Workspace 前缀的官方地址）")
        request["poll_interval"] = 5
        request["poll_timeout"] = 1800
    else:
        request["http_timeout"] = 120
    # 请求文件里没有任何密钥值；key_env 只是变量名。
    # 写完立刻落盘，子进程只认这一份快照，运行中被改环境也不影响这次调用。
    request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    argv = [sys.executable, str(CLOUD_RUNNER), str(request_path)]
    return RunSpec(
        runner=f"cloud:{adapter.code}", argv=argv, cwd=outputs,
        expected=[out], media=media,
        meta={"adapter": adapter.code, "model": job["model_code"],
              "provider": adapter.code, "request_path": str(request_path),
              "result_path": str(result_path)})


def build(job, params: dict, outputs: Path) -> RunSpec:
    """按能力与 provider 分发。没接线的组合明确拒绝，不找替代品。

    `execution_mode` 是 Job 行冻结的：本地作业永远走本地分支，
    云端作业才走 `_cloud`。这里没有"失败就换一条轨"的入口。
    """
    capability = job["capability"]
    provider = job["provider_code"]
    model = job["model_code"]
    mode = job["execution_mode"]
    if mode == "cloud":
        adapter = adapters.find(capability, "cloud", provider)
        if adapter is None:
            raise RunnerUnavailable(f"provider「{provider}」没有登记云端适配器")
        if not adapter.models:
            raise RunnerUnavailable(f"{provider} 的参数与模型尚未核验，不能执行")
        return _cloud(job, params, outputs, adapter)
    if capability == "video" and provider == "fastmetal":
        return _fastmetal(job, params, outputs, model)
    if capability == "video" and provider == "mlx-video":
        return _mlx_video(job, params, outputs)
    if capability == "image" and provider == "mflux":
        return _mflux(job, params, outputs)
    if capability == "image" and provider in ("draw_things", "drawthings"):
        return _draw_things(job, params, outputs, model)
    if capability == "text" and provider in ("ollama",):
        return _ollama(job, params, outputs, model)
    if capability == "tts" and provider in ("voxcpm2", "voxcpm"):
        return _voxcpm(job, params, outputs)
    if capability == "music" and provider in ("ace-step", "acestep", "ace_step"):
        return _ace_step(job, params, outputs)
    raise RunnerUnavailable(f"{capability}/{provider} 还没有执行接线")


def has_branch(capability: str, provider: str) -> bool:
    """这一对 capability/provider 在 `build()` 里有没有分支。

    存在的理由：音频中心那张能力表原来把 tts/music 的 `runner_wired` **写死成 False**，
    注释还写着"今天没有分支"——那是一句会在接线当天就变成假话的实话。
    现在它问这里，而这里与 `build()` 读的是同一串条件。
    """
    if capability == "text":
        return provider == "ollama"
    if capability == "video":
        return provider in ("fastmetal", "mlx-video")
    if capability == "image":
        return provider in ("mflux", "draw_things")
    if capability == "tts":
        return provider in ("voxcpm2", "voxcpm")
    if capability == "music":
        return provider in ("ace-step", "acestep", "ace_step")
    return False


def _magic_ok(path: Path, media: str) -> bool:
    try:
        head = path.open("rb").read(16)
    except OSError:
        return False
    if media == "video":
        # ISO-BMFF: 4 字节长度 + 'ftyp'。FastMetal 与 ffmpeg 都产出这个头。
        return len(head) >= 12 and head[4:8] == b"ftyp"
    if media == "image":
        return head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"\xff\xd8\xff"
    if media == "audio":
        # WAV＝RIFF....WAVE；mp3＝ID3；flac＝fLaC；m4a 走 ISO-BMFF。
        # 这条不是装饰：音频产物一旦被截断、或写成一张 HTML 错误页，
        # 时间线与成片会带着一个"看起来存在"的空音轨走下去。
        return (head[:4] == b"RIFF" and head[8:12] == b"WAVE") or \
            head[:3] == b"ID3" or head[:4] == b"fLaC" or \
            (len(head) >= 12 and head[4:8] == b"ftyp")
    return True


def verify(spec: RunSpec) -> list[Path]:
    """按媒体类型验真；返回实际存在的产物路径。缺一件就抛，不写半条成功。"""
    # `RunSpec` 是 frozen 的：找到的产物只带出去，不回头改那份规格。
    expected = list(spec.expected)
    if not expected and spec.expected_glob:
        expected = sorted(p for p in spec.cwd.glob(spec.expected_glob) if p.is_file())
        if not expected:
            raise RunnerUnavailable(
                "命令返回了，但 %s 里没有任何 %s 产物"
                % (spec.cwd.name, spec.expected_glob))
    missing = [str(p) for p in expected if not p.is_file()]
    if missing:
        raise RunnerUnavailable("命令返回了，但没有产物：" + "、".join(Path(p).name for p in missing))
    for path in expected:
        if path.stat().st_size <= 0:
            raise RunnerUnavailable(f"产物是空的：{path.name}")
        if not _magic_ok(path, spec.media):
            raise RunnerUnavailable(f"产物不是可识别的 {spec.media} 文件：{path.name}")
    return expected


def provider_receipt(spec: RunSpec) -> dict:
    """读 cloud_run.py 留下的回执，只留对账需要的字段。

    回执里可能有完整错误响应；这里按白名单挑，既避免把供应商原文
    整段塞进 /api/jobs 的响应，也保证 `request_id` 与 `usage` 一定在。
    """
    path = Path(spec.meta.get("result_path", ""))
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("ok", "provider", "model", "capability", "request_id",
                "task_request_id", "task_status", "http_status", "latency_ms",
                "bytes", "usage", "response_meta", "error"):
        if key in raw:
            keep[key] = raw[key]
    return keep


def metrics_summary(path: Path) -> dict:
    """从模型自己写的 metrics.json 里挑几项进 resources；原文件仍留在 outputs。

    不是整份搬进库：metrics 里有模型路径、完整调度表，既不必要也不该
    进 /api/jobs 的响应。
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("prompt", "height", "width", "num_frames", "seed",
                "decode_backend", "spatial_mode", "fast", "refine",
                "prompt_encode_s", "mlx_dit_load_s", "mlx_denoise_s",
                "vae_decode_export_s", "total_s", "dmd_denoising_steps"):
        if key in raw:
            keep[key] = raw[key]
    for src, dst in (("mlx_denoise_peak_bytes", "denoise_peak_gb"),
                     ("mlx_load_peak_bytes", "load_peak_gb")):
        value = raw.get(src)
        if isinstance(value, (int, float)) and value >= 0:
            keep[dst] = round(value / (1024 ** 3), 2)
    return keep


def cleanup_partial(spec: RunSpec) -> None:
    """失败时删掉半截产物，免下次重试被旧文件骗过。"""
    for path in spec.expected:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
