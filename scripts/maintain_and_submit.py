#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from blogauto.cli import cmd_submit


def main() -> int:
    parser = argparse.ArgumentParser(description="提交前扫描并更新主页，提交后生成变动目录")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    try:
        cmd_submit(Path(args.workspace).resolve(), args.message, args.no_open)
    except Exception as exc:
        print(f"执行失败: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
