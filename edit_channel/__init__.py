"""Python 编辑消息通道：多人节目时间线的合流、冲突决策与转播领取确认。

快速开始::

    from edit_channel import EditChannel

    desk = EditChannel()
    desk.handle({"at": 0, "kind": "program", "record": {...}})
    ...

主要入口：
* :meth:`EditChannel.submit_edit` / ``handle({"kind": "edit", ...})``
* :meth:`EditChannel.decide`（冲突采用 / 拒绝 / 要求重新提交）
* :meth:`EditChannel.cast` / :meth:`EditChannel.applied` /
  :meth:`EditChannel.retract` / :meth:`EditChannel.resume` / :meth:`EditChannel.inspect`
"""

from .core import (
    Channel,
    Desk,
    EditChannel,
    MessageChannel,
    ProgramDesk,
    Project,
)
from .errors import (
    ConflictingRelease,
    DeskError,
    PredecessorNotConfirmed,
    SpliceRequired,
    StaleDecisionBase,
    UnknownBase,
    UnknownCaster,
    UnknownEdit,
    UnknownProgram,
    UnknownRevision,
    ValidationError,
)
from .simulator import (
    LocalBroadcaster,
    SimulatedBroadcastEndpoint,
    load_supported_types,
    make_local_pair,
    read_jsonl,
    run_directory,
    run_path,
    run_rows,
)
from .spans import media_ref, next_splice_after, overlap_interval, overlaps, parse_span

__all__ = [
    "EditChannel",
    "Project",
    "ProgramDesk",
    "Desk",
    "Channel",
    "MessageChannel",
    "LocalBroadcaster",
    "SimulatedBroadcastEndpoint",
    "make_local_pair",
    "run_path",
    "run_rows",
    "run_directory",
    "read_jsonl",
    "load_supported_types",
    "media_ref",
    "overlaps",
    "overlap_interval",
    "parse_span",
    "next_splice_after",
    "DeskError",
    "ValidationError",
    "UnknownProgram",
    "UnknownEdit",
    "UnknownCaster",
    "UnknownRevision",
    "UnknownBase",
    "StaleDecisionBase",
    "PredecessorNotConfirmed",
    "SpliceRequired",
    "ConflictingRelease",
]

__version__ = "1.0.0"


def create_channel(policy: str = "bundle", **options) -> "EditChannel":
    """创建一个空的节目编辑消息通道/项目。"""
    return EditChannel(policy=policy, **options)


# 常见工厂命名别名。
new_channel = create_channel
new_desk = create_channel
open_project = EditChannel.load_state
