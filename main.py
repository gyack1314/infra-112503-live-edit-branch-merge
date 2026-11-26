"""常见 CLI 入口名：``python3 main.py [paths...]``。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
