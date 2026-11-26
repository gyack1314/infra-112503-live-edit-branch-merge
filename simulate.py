"""兼容入口：``python3 simulate.py examples/xxx.jsonl``。"""

import sys
from pathlib import Path

# 兼容 ``python3 -I``：隔离模式不会把脚本目录放上 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
