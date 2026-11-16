"""执行器 protocol 域。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CLOUD_RUNNER = Path(__file__).resolve().parents[1] / "cloud_run.py"

ENGINE_HTTP_RUNNER = Path(__file__).resolve().parents[1] / "engine_http_run.py"

LLM_RUNNER = Path(__file__).resolve().parents[1] / "llm_http_run.py"


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
    # worker 起子进程时用**环境变量白名单**（见 worker._child_env），
    # 这里声明"我这个 runner 额外需要哪几个变量名"。
    # 云端 runner 用它放行 `key_env` 点名的那一个密钥——只放这一个，
    # 不是把整个环境交出去。本地模型 runner 不需要，留空。
    pass_env: tuple[str, ...] = ()
