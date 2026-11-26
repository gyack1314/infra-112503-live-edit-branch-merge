"""脚本入口别名：``python3 edit_channel.py examples/...``。

正常 ``import edit_channel`` 会解析到同名包目录；本文件仅用于直接按脚本路径调用 CLI。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
