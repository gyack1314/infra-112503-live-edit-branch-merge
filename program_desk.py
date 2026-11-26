"""导入兼容别名：``from program_desk import ProgramDesk``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel import *  # noqa: F401,F403,E402
from edit_channel import EditChannel  # noqa: E402

ProgramDesk = EditChannel

__all__ = ["ProgramDesk", "EditChannel"]

if __name__ == "__main__":
    from edit_channel.cli import main

    raise SystemExit(main())
