"""导入/运行兼容别名：``python3 channel.py examples/x.jsonl``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel import *  # noqa: F401,F403,E402
from edit_channel import Channel, EditChannel, MessageChannel  # noqa: E402

__all__ = ["EditChannel", "Channel", "MessageChannel"]

if __name__ == "__main__":
    from edit_channel.cli import main

    raise SystemExit(main())
