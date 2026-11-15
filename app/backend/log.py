"""平台统一结构化日志与消息脱敏模块（M1.3）。

提供标准库 `logging` 的单出口 `get_logger(name)` 与密钥/超长内容脱敏 `sanitize_msg()`：
- 不影响任何 CLI / 子进程（`llm_http_run.py`、`engine_http_run.py`、`rife_pass.py` 等）的 stdout 协议；
- 所有运行日志统一写往 `stderr`，并自动掩码环境变量密钥明文与常见 Bearer / `sk-` 令牌。
"""
from __future__ import annotations

import logging
import os
import re
import sys

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_CONFIGURED = False

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_ENV_SECRET_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_KEK")


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    base = logging.getLogger("fastvideo")
    if not base.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        base.addHandler(handler)
    base.setLevel(logging.INFO)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取挂载在 fastvideo 命名空间下的统一 logger。"""
    _configure_root()
    full_name = (
        name if name == "fastvideo" or name.startswith("fastvideo.")
        else f"fastvideo.{name}"
    )
    return logging.getLogger(full_name)


def sanitize_msg(msg: object, max_len: int = 300) -> str:
    """对日志文本进行密钥脱敏与长度截断，防止敏感信息外泄。"""
    text = str(msg or "")
    text = _SECRET_RE.sub("***", text)
    for key, val in os.environ.items():
        if key.upper().endswith(_ENV_SECRET_SUFFIXES) and len(val) >= 6:
            text = text.replace(val, "***")
    if max_len > 0 and len(text) > max_len:
        return text[:max_len] + "...[已截断]"
    return text
