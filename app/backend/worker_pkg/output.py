"""子进程输出的有界流式日志边界；只处理记录，不影响执行或产物。"""
from __future__ import annotations

import codecs
import json
import re
from collections.abc import Iterable
from typing import TextIO

from log import sanitize_msg

# Unix 绝对路径；URL 内的 / 与相对路径不匹配。
_ABSOLUTE_PATH = re.compile(r'''(?<![\w:/])/(?:[^\s"'<>|;,)\]]+)''')
_BREAK = re.compile(r"[\r\n]")
_MAX_LINE = 8192


def private_output_values(job) -> tuple[str, ...]:
    """作业正文的已知入口；不把参数整体或输入引用写进日志。"""
    row = dict(job)
    values = [row.get("prompt_snapshot")]
    try:
        params = json.loads(row.get("params_json") or "{}")
    except (TypeError, ValueError):
        params = {}
    if isinstance(params, dict):
        values.extend(params.get(key) for key in
                      ("prompt", "negative_prompt", "negative", "text", "caption"))
    return tuple(value for value in values if isinstance(value, str) and value)


class OutputLog:
    """跨 read 块保留完整行后脱敏；超长无分隔行隐藏，避免无界缓存。"""

    def __init__(self, target: TextIO, private_values: Iterable[str]):
        self.target = target
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        values: set[str] = set()
        for value in private_values:
            if value:
                values.update(value.splitlines())
                values.add(json.dumps(value, ensure_ascii=False)[1:-1])
        self.private_values = sorted(values - {""}, key=len, reverse=True)
        self.pending = ""
        self.hidden = False

    def _record(self, line: str) -> None:
        for value in self.private_values:
            line = line.replace(value, "<正文或密钥已隐藏>")
        self.target.write(_ABSOLUTE_PATH.sub("<path>", sanitize_msg(line, max_len=0)))
        self.target.flush()

    def write(self, chunk: bytes) -> None:
        self._feed(self.decoder.decode(chunk))

    def _feed(self, text: str) -> None:
        self.pending += text
        while match := _BREAK.search(self.pending):
            line, self.pending = self.pending[:match.end()], self.pending[match.end():]
            if self.hidden or len(line) > _MAX_LINE:
                self._record("<超长输出已隐藏>" + line[-1])
            else:
                self._record(line)
            self.hidden = False
        if len(self.pending) > _MAX_LINE:
            self.pending = ""
            self.hidden = True

    def finish(self) -> None:
        self._feed(self.decoder.decode(b"", final=True))
        if self.hidden:
            self._record("<超长输出已隐藏>\n")
        elif self.pending:
            self._record(self.pending)
        self.pending = ""
        self.hidden = False
