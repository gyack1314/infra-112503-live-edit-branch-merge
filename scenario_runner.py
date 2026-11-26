"""导入/运行兼容别名：场景回放器 ``python3 scenario_runner.py <path>``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel.simulator import (  # noqa: E402
    LocalBroadcaster,
    run_directory,
    run_path,
    run_rows,
)

__all__ = ["run_path", "run_rows", "run_directory", "LocalBroadcaster"]

if __name__ == "__main__":
    from edit_channel.cli import main

    raise SystemExit(main())
