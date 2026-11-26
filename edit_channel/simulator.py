"""JSONL 演练记录回放 + 本地模拟转播端。

回放器尽量复用仓库根目录的 ``scenario_reader.iter_messages`` 做信封校验；
在包被单独拷贝、找不到该模块时退回内置的最小读取器。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from .core import EditChannel

DEFAULT_MESSAGE_TYPES = [
    "applied",
    "cast",
    "decide",
    "edit",
    "inspect",
    "program",
    "resume",
    "retract",
]


def load_supported_types(examples_dir: Path | None = None) -> set[str]:
    """从 examples/message_types.json 读取登记的消息名，失败时用内置清单。"""
    candidates = []
    if examples_dir is not None:
        candidates.append(Path(examples_dir) / "message_types.json")
    candidates.extend(
        [
            Path.cwd() / "examples" / "message_types.json",
            Path(__file__).resolve().parent.parent / "examples" / "message_types.json",
        ]
    )
    for path in candidates:
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return set(DEFAULT_MESSAGE_TYPES)


def read_jsonl(path: Path, supported: Iterable[str] | None = None) -> Iterator[dict[str, Any]]:
    """优先使用仓库的 scenario_reader，否则使用等价的内置读取器。"""
    try:  # 仓库根目录在 sys.path 上时（python -m edit_channel）
        from scenario_reader import iter_messages  # type: ignore

        yield from iter_messages(Path(path), set(supported) if supported else load_supported_types())
        return
    except ImportError:
        pass

    supported_set = set(supported) if supported else load_supported_types()
    previous = -1
    with Path(path).open(encoding="utf-8") as source:
        for line_number, raw in enumerate(source, 1):
            row = json.loads(raw)
            if not isinstance(row, dict) or set(row) != {"at", "kind", "record"}:
                raise ValueError(f"{path.name} 第 {line_number} 行：消息需要 at、kind 和 record 字段")
            at = row["at"]
            if type(at) is not int or at < 0 or at < previous:
                raise ValueError(f"{path.name} 第 {line_number} 行：到达秒点必须为非负整数且不能倒退")
            if row["kind"] not in supported_set:
                raise ValueError(f"{path.name} 第 {line_number} 行：未登记的消息名称 {row['kind']}")
            if not isinstance(row["record"], dict):
                raise ValueError(f"{path.name} 第 {line_number} 行：record 应为对象")
            previous = at
            yield row


def run_rows(
    rows: Iterable[dict[str, Any]],
    channel: EditChannel | None = None,
    policy: str = "bundle",
) -> dict[str, Any]:
    """处理一组消息信封，返回通道实例与逐消息回执。"""
    channel = channel or EditChannel(policy=policy)
    lines = [channel.handle(row) for row in rows]
    return {"channel": channel, "lines": lines}


def run_path(
    path: str | Path,
    channel: EditChannel | None = None,
    policy: str = "bundle",
) -> dict[str, Any]:
    """回放单个 JSONL 场景文件。"""
    path = Path(path)
    return run_rows(read_jsonl(path), channel=channel, policy=policy)


def run_directory(
    directory: str | Path, policy: str = "bundle"
) -> list[dict[str, Any]]:
    """顺序回放目录下全部 ``*.jsonl``，每个文件使用独立通道。"""
    directory = Path(directory)
    outputs: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        outcome = run_path(path, policy=policy)
        outputs.append({"scenario": path.stem, "path": str(path), **outcome})
    return outputs


# ---------------------------------------------------------------------- #
# 本地模拟转播端
# ---------------------------------------------------------------------- #
class LocalBroadcaster:
    """进程内模拟转播端：保存自己的播放头、已领取指令与确认记录。

    它只是 :class:`EditChannel` 上 cast/applied/resume 入口的薄封装，
    额外维护一个本地 ``playhead`` 媒体位置，方便演练“在拼接点之前/之后”。
    """

    def __init__(self, channel: EditChannel, name: str, program: str, branch: str):
        self.channel = channel
        self.name = name
        self.program = program
        self.branch = branch
        self.playhead: int | None = 0

    def cast(self, at: int | None = None) -> dict[str, Any]:
        return self.channel.cast(
            {"name": self.name, "program": self.program, "branch": self.branch}, at=at
        )

    def apply(self, revision: int, media: int | None = None, at: int | None = None) -> dict[str, Any]:
        media = self.playhead if media is None else media
        result = self.channel.applied(
            {
                "name": self.name,
                "program": self.program,
                "branch": self.branch,
                "revision": revision,
                "media": media,
            },
            at=at,
        )
        self.playhead = media
        return result

    # 语义别名
    confirm = apply

    def seek(self, media: int) -> int:
        """把模拟播放头移动到某媒体帧。"""
        self.playhead = media
        return media

    def resume(self, revision: int | None = None, media: int | None = None, at: int | None = None) -> dict[str, Any]:
        media = self.playhead if media is None else media
        record = {
            "name": self.name,
            "program": self.program,
            "branch": self.branch,
            "revision": 0 if revision is None else revision,
            "media": media,
        }
        result = self.channel.resume(record, at=at)
        self.playhead = media
        return result

    def reconnect(self, revision: int | None = None, media: int | None = None, at: int | None = None):
        return self.resume(revision=revision, media=media, at=at)

    @property
    def media_id(self) -> str:
        from .spans import media_ref

        return media_ref(self.program, self.branch, self.playhead)


class SimulatedBroadcastEndpoint(LocalBroadcaster):
    """更贴近“转播端”命名的别名。"""


def make_local_pair(
    name_a: str = "A",
    name_b: str = "B",
    program: str = "P",
    branch: str = "full",
    policy: str = "bundle",
) -> tuple[EditChannel, LocalBroadcaster, LocalBroadcaster]:
    """快速搭建一个通道 + 两个本地模拟转播端。"""
    channel = EditChannel(policy=policy)
    a = LocalBroadcaster(channel, name_a, program, branch)
    b = LocalBroadcaster(channel, name_b, program, branch)
    return channel, a, b
