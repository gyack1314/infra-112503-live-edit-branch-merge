"""导入/运行兼容别名：``from desk import Desk`` / ``python3 desk.py``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel import *  # noqa: F401,F403,E402
from edit_channel import EditChannel, LocalBroadcaster, run_path, run_rows  # noqa: E402

Desk = EditChannel

__all__ = ["Desk", "EditChannel", "LocalBroadcaster", "run_path", "run_rows"]

if __name__ == "__main__":
    from edit_channel.cli import main

    raise SystemExit(main())
