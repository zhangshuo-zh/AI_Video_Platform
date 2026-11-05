"""本地运行时探测：仓库在哪、权重在哪、CLI 能不能起。

W06 的健康检查只回答"这个 provider 有没有登记、运行时目录在不在"；
W09 要真起子进程，就必须再往前走一步：**解释器、入口脚本、权重目录、
模型文件**四样缺一样都跑不起来。这一层只做文件系统检查，不 import MLX、
不加载权重、不发网络请求——点开系统设置页不会把 12GB 模型读进内存。

三条边界：

1. 路径一律来自环境变量，仓库里不写死 /Users/...；run.sh 会在本机存在
   `~/FastVideo` 时补一个默认值，部署到 Studio 只要覆盖环境变量。
2. 探测结果分成 ok／missing／unknown。unknown 是"还没配置，平台查不了"，
   missing 是"配了，但东西不在"——人要做的事不同，不能混成一句"不可用"。
3. 这里返回的路径只给 worker 与系统设置页用，不进 Job 的公开响应；
   产物与日志仍只暴露相对路径。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class Runtime:
    """一次探测的结果。state 三档与 adapters.health 共用一套词。"""

    state: str                     # ok / missing / unknown
    detail: str
    values: dict                   # 只给内部与系统设置页，不公开

    def as_health(self) -> dict:
        return {"state": self.state, "detail": self.detail}


def _missing(detail: str, **values) -> Runtime:
    return Runtime("missing", detail, values)


def _unknown(detail: str, **values) -> Runtime:
    return Runtime("unknown", detail, values)


def _ok(detail: str, **values) -> Runtime:
    return Runtime("ok", detail, values)


def fastmetal_home() -> Path | None:
    raw = _env("FASTVIDEO_FASTMETAL_HOME")
    return Path(raw).expanduser() if raw else None


def fastmetal_model_dir(model_code: str) -> Path | None:
    """模型目录：显式 env 优先，否则按档位在仓库下找。

    1.3B 与 5B 的权重目录名不同；指向同一个 env 会让"1.3B 能跑"和
    "5B 也能跑"变成同一句话，所以只给 1.3B 一个显式覆盖，5B 按目录名找。
    """
    raw = _env("FASTVIDEO_FASTMETAL_MODEL") if model_code == "fastmetal-1.3b-qad" else ""
    if raw:
        return Path(raw).expanduser()
    home = fastmetal_home()
    if home is None:
        return None
    name = "FastMetal-5B-QAD" if "5b" in model_code.lower() else "FastMetal-1.3B-QAD"
    return home / name


def fastmetal_python(home: Path) -> Path | None:
    for rel in (".venv/bin/python", "venv/bin/python"):
        candidate = home / rel
        if candidate.is_file():
            return candidate
    return None


def _fastmetal_script(home: Path, model_code: str) -> Path:
    name = ("mlx_wan22_generate.py" if "5b" in model_code.lower()
            else "mlx_wan_prompt_to_video.py")
    return home / "examples" / "inference" / "basic" / name


def _model_files_missing(root: Path) -> list[str]:
    """FastMetal 权重目录的五件套。缺哪件就说哪件，不用一句"权重坏了"糊过去。"""
    needed = ("mlx_dit.json", "mlx_dit.safetensors", "text_encoder", "tokenizer", "vae")
    return [name for name in needed if not (root / name).exists()]


def probe_fastmetal(model_code: str) -> Runtime:
    home = fastmetal_home()
    if home is None:
        return _unknown("设 FASTVIDEO_FASTMETAL_HOME 指向 FastVideo 仓库后才会检查")
    if not home.is_dir():
        return _missing(f"FASTVIDEO_FASTMETAL_HOME 指向的目录不存在：{home}",
                        home=str(home))
    python = fastmetal_python(home)
    if python is None:
        return _missing("仓库里没有 .venv/bin/python；先在 FastVideo 下建虚拟环境",
                        home=str(home))
    script = _fastmetal_script(home, model_code)
    if not script.is_file():
        return _missing(f"入口脚本不在：{script}", home=str(home), python=str(python))
    model = fastmetal_model_dir(model_code)
    if model is None or not model.is_dir():
        return _missing(
            f"权重目录不在：{model or '（未设置）'}；1.3B 可用 "
            "FASTVIDEO_FASTMETAL_MODEL 覆盖",
            home=str(home), python=str(python), script=str(script))
    missing = _model_files_missing(model)
    if missing:
        return _missing(f"权重目录缺：{'、'.join(missing)}",
                        home=str(home), python=str(python), script=str(script),
                        model=str(model))
    return _ok("解释器、入口脚本与权重目录都在（未加载权重）",
               home=str(home), python=str(python), script=str(script),
               model=str(model))


def mlx_video_home() -> Path | None:
    raw = _env("FASTVIDEO_MLX_VIDEO_HOME")
    return Path(raw).expanduser() if raw else None


def mlx_video_python(home: Path) -> Path | None:
    for rel in (".venv/bin/python", "venv/bin/python"):
        candidate = home / rel
        if candidate.is_file():
            return candidate
    return None


def mlx_video_model_dir() -> Path | None:
    raw = _env("FASTVIDEO_MLX_VIDEO_MODEL")
    return Path(raw).expanduser() if raw else None


def probe_mlx_video() -> Runtime:
    home = mlx_video_home()
    if home is None:
        return _unknown("设 FASTVIDEO_MLX_VIDEO_HOME 指向 mlx-video 仓库后才会检查")
    if not home.is_dir():
        return _missing(f"FASTVIDEO_MLX_VIDEO_HOME 指向的目录不存在：{home}",
                        home=str(home))
    python = mlx_video_python(home)
    if python is None:
        return _missing("仓库里没有 .venv/bin/python", home=str(home))
    package = home / "mlx_video"
    if not package.is_dir():
        return _missing("仓库里没有 mlx_video/ 包；先按 README 安装",
                        home=str(home), python=str(python))
    model = mlx_video_model_dir()
    if model is None:
        return _missing("设 FASTVIDEO_MLX_VIDEO_MODEL 指向转换后的 Wan2.2 I2V 目录",
                        home=str(home), python=str(python))
    if not (model / "config.json").is_file():
        return _missing(f"模型目录里没有 config.json：{model}",
                        home=str(home), python=str(python), model=str(model))
    return _ok("解释器、mlx_video 包与转换后的权重目录都在（未加载权重）",
               home=str(home), python=str(python), model=str(model))


def mflux_bin() -> Path | None:
    """mflux 的 CLI：显式 FASTVIDEO_MFLUX_BIN > home 下的 venv/bin > PATH。

    最后那档不是"猜"：uv tool install 会把 mflux-generate 放进 ~/.local/bin，
    它本来就是一个可执行的绝对路径，which 找到的就是它。
    """
    raw = _env("FASTVIDEO_MFLUX_BIN")
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_file() else None
    home = _env("FASTVIDEO_MFLUX_HOME")
    if home:
        root = Path(home).expanduser()
        for rel in (".venv/bin/mflux-generate", "bin/mflux-generate"):
            candidate = root / rel
            if candidate.is_file():
                return candidate
        return None
    found = shutil.which("mflux-generate")
    return Path(found) if found else None


def probe_mflux() -> Runtime:
    home = _env("FASTVIDEO_MFLUX_HOME")
    if not home and not _env("FASTVIDEO_MFLUX_BIN") and not shutil.which("mflux-generate"):
        return _unknown("设 FASTVIDEO_MFLUX_HOME（或把 mflux-generate 放进 PATH）后才会检查")
    cli = mflux_bin()
    if cli is None:
        return _missing("找不到 mflux-generate；按 mflux README 安装后再设路径",
                        home=home)
    return _ok("mflux-generate 在；首次运行才会下载权重" if not home else
               "mflux-generate 与运行时目录都在；首次运行才会下载权重",
               home=home, cli=str(cli))


def mlx_audio_bin(which: str = "tts") -> Path | None:
    """找 mlx-audio 的两个入口脚本。

    装法只有一种被验证过：`uv tool install mlx-audio` ⇒ 入口落在 `~/.local/bin/`，
    名字里带点（`mlx_audio.tts.generate`／`mlx_audio.music.generate`），
    而 `~/.local/bin` 常常不在 PATH 里——所以既查 PATH 也查那个固定目录，
    还支持用 `FASTVIDEO_MLX_AUDIO_BIN` 直接指到可执行文件（与 mflux 同一套规矩）。
    """
    env = _env("FASTVIDEO_MLX_AUDIO_BIN")
    if env:
        cand = Path(env)
        return cand if cand.is_file() else None
    name = "mlx_audio.%s.generate" % ("tts" if which == "tts" else "music")
    found = shutil.which(name)
    if found:
        return Path(found)
    for base in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin")):
        cand = base / name
        if cand.is_file():
            return cand
    return None


# mlx-audio 的模型注册表是"按目录里有没有这一档"来认的：
# `mlx_audio/<类别>/models/<模型类型>.py`。音乐这一类在 0.6.4 里只有 minimax_music3，
# 所以 ACE-Step 的 MLX 权重虽然已经下到本机，跑起来仍是 `ValueError: Model type
# acestep not supported for music`——那是一条**运行时就读得出**的事实，
# 不该等到作业失败才让人知道。
_MUSIC_MODEL_TYPES = {"ace-step": "acestep"}


def _supported_music_types(site_root: Path) -> list[str]:
    models = site_root / "mlx_audio" / "music" / "models"
    if not models.is_dir():
        return []
    # 一档模型可能是一个 .py，也可能是一个包目录（minimax_music3/ 就是目录）——
    # 只 glob("*.py") 会把整张注册表读成空，而"读成空"在这里等于"检查没做却说 ok"。
    names = {p.stem for p in models.glob("*.py") if p.stem != "__init__"}
    names |= {p.name for p in models.iterdir() if p.is_dir() and not p.name.startswith("__")}
    return sorted(names)


def probe_mlx_audio(which: str = "tts") -> Runtime:
    cli = mlx_audio_bin(which)
    if cli is None:
        return _missing(
            "找不到 mlx_audio.%s.generate；先 `uv tool install mlx-audio`，"
            "或把可执行文件路径写进 FASTVIDEO_MLX_AUDIO_BIN"
            % ("tts" if which == "tts" else "music"))
    if which == "music":
        # 入口脚本本身是个带 shebang 的小启动器，site-packages 在 shebang 那个
        # 虚拟环境里（uv tool 的布局：…/tools/mlx-audio/lib/python3.12/site-packages）。
        # 猜相对目录会猜空——猜空就等于"检查没做但说 ok"，那是假绿。
        candidates: list[Path] = []
        try:
            shebang = Path(cli).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        except OSError:
            shebang = ""
        if shebang.startswith("#!"):
            env_root = Path(shebang[2:].strip()).parent.parent
            candidates = sorted((env_root / "lib").glob("python*/site-packages"))
        supported = _supported_music_types(candidates[0]) if candidates else []
        if supported and "acestep" not in supported:
            return _missing(
                "mlx-audio 的音乐注册表里没有 acestep 这一档（现有：%s）；"
                "权重已在本机，等 mlx-audio 支持后再配默认轨"
                % "、".join(supported), cli=str(cli))
        return _ok("mlx-audio 音乐入口与 acestep 档都在", cli=str(cli))
    return _ok("mlx-audio 入口在；权重首次运行才会下载", cli=str(cli))


def probe(code: str, model_code: str = "") -> Runtime:
    """按 adapter code 分发。没登记的本地 provider 不猜运行方式。"""
    if code == "fastmetal":
        return probe_fastmetal(model_code)
    if code == "mlx-video":
        return probe_mlx_video()
    if code == "mflux":
        return probe_mflux()
    if code == "voxcpm2":
        return probe_mlx_audio("tts")
    if code == "ace-step":
        return probe_mlx_audio("music")
    return _unknown("这个本地 provider 还没有运行时探测规则")
