#!/usr/bin/env python3
"""检查图片导入恢复记录；默认只读，--apply 执行补偿。不启动应用或迁移。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="只处理带合法导入意图的文件")
    args = parser.parse_args()
    from config import DB_PATH
    if not DB_PATH.is_file():
        parser.error("数据库不存在，请先正常初始化平台或设置 FASTVIDEO_DB")
    from asset_import_recovery import recover
    result = recover(apply=args.apply)
    print(json.dumps({"applied": args.apply, **result}, ensure_ascii=False))
    return 1 if result["pending"] or result["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
