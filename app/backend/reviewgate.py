"""校验器 gate（W14）：确定性规则 + 稳定锚点。

它不是大模型评审，只回答"这段正文有没有明显的坏味道"：占位词、超长句、
没有收尾标点。规则少而固定，报告格式统一；人签的是"我看过了"，
不是把判断外包给脚本。

锚点用**字符偏移**（`c123`），不是句子序号：同一版正文里偏移稳定，
重跑 gate 不会把签字挪到另一句；正文一改版本就变，旧签字自然作废。

也可以单独跑：
    python reviewgate.py path/to/draft.md
输出一份与接口同形的 JSON 报告。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLACEHOLDERS = re.compile(r"(TODO|TBD|XXX|待补|待写|待定|\?\?\?|（待写）|\(待写\))")
SENTENCE_END = "。！？!?…；;”’」』）)"
MAX_SENTENCE = 120
MIN_UNTERMINATED = 12


class ReviewInvalid(Exception):
    """输入不成立（空稿、版本不符）。路由回 422／409。"""


def sentences(content: str) -> list[dict]:
    """按句末标点切句，保留每句的字符偏移与原文。

    不追求语言学正确：审核要的是"这一句在哪儿"，偏移比分词可靠。
    """
    out: list[dict] = []
    start = 0
    for i, ch in enumerate(content):
        if ch in "。！？!?…\n":
            end = i + 1
            text = content[start:end].strip()
            if text:
                out.append({"start": start, "end": end, "text": text})
            start = end
    tail = content[start:].strip()
    if tail:
        out.append({"start": start, "end": len(content), "text": tail})
    return out


def _finding(anchor: str, rule: str, severity: str, message: str,
             start: int | None = None, end: int | None = None,
             quote: str = "") -> dict:
    return {"anchor": anchor, "rule": rule, "severity": severity,
            "message": message, "start": start, "end": end, "quote": quote}


def run(content: str) -> tuple[str, list[dict], dict]:
    """返回 (passed/failed, findings, summary)。空稿直接 failed。"""
    if not content.strip():
        finding = _finding("document", "empty", "error", "正文是空的，没有可审的内容")
        return "failed", [finding], {"sentences": 0, "errors": 1, "warnings": 0}

    findings: list[dict] = []
    parts = sentences(content)
    for index, part in enumerate(parts, 1):
        text = part["text"]
        anchor = f"c{part['start']}"
        quote = text[:160]
        if PLACEHOLDERS.search(text):
            findings.append(_finding(
                anchor, "placeholder", "error",
                f"第 {index} 句还有占位词，交片前要填掉",
                part["start"], part["end"], quote))
        bare = text.rstrip()
        if len(bare) >= MIN_UNTERMINATED and bare[-1] not in SENTENCE_END:
            findings.append(_finding(
                anchor, "unterminated", "warning",
                f"第 {index} 句没有收尾标点，读起来像断了",
                part["start"], part["end"], quote))
        if len(text) > MAX_SENTENCE:
            findings.append(_finding(
                anchor, "long_sentence", "error",
                f"第 {index} 句 {len(text)} 字，超过 {MAX_SENTENCE} 字；先拆开",
                part["start"], part["end"], quote))

    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    summary = {"sentences": len(parts), "errors": errors, "warnings": warnings}
    return ("passed" if errors == 0 else "failed"), findings, summary


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args:
        content = Path(args[0]).read_text(encoding="utf-8")
        name = args[0]
    else:
        content = sys.stdin.read()
        name = "<stdin>"
    status, findings, summary = run(content)
    print(json.dumps({"source": name, "status": status,
                      "findings": findings, "summary": summary},
                     ensure_ascii=False, indent=2))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
