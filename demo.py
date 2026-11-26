#!/usr/bin/env python3
"""可重复使用入口的自检式演示：直接用 Python API + 本地模拟转播端。

运行：``python3 demo.py``
它会断言任务说明中的关键不变量；任何一条被破坏都会以非零码退出。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edit_channel import EditChannel, LocalBroadcaster  # noqa: E402


def main() -> int:
    desk = EditChannel()  # 默认 policy="bundle"：干净编辑随决策尽快合流

    # 1) 作品提供方登记分支、拼接点与不可分割目标组。
    desk.register_program(
        {
            "program": "gala",
            "branch": "main",
            "base": "v0",
            "splices": [10, 20, 30],
            "tied": [["person-mask", "person-caption"]],
        }
    )

    # 2) 制作台与主持人各自提交；区间相交且目标被 tied 绑定 -> 冲突。
    desk.submit_edit(
        {"id": "mask", "program": "gala", "branch": "main", "base": "v0",
         "span": [20, 30], "target": "person-mask", "change": "blur", "by": "desk"}
    )
    desk.submit_edit(
        {"id": "caption", "program": "gala", "branch": "main", "base": "v0",
         "span": [24, 28], "target": "person-caption", "change": "name", "by": "host"}
    )
    conflict = desk.inspect_edit("caption")
    assert conflict["conflicted"] and conflict["conflicts_with"] == ["mask"]
    assert conflict["conflicts"][0]["overlap"] == [24, 28]

    # 3) 另一条只在 [30,40) 的同目标编辑与 mask 端点相接，不冲突，进候选。
    desk.submit_edit(
        {"id": "caption-later", "program": "gala", "branch": "main", "base": "v0",
         "span": [30, 40], "target": "person-caption", "change": "next-name"}
    )
    assert desk.inspect_edit("caption-later")["mergeable"]

    # 4) 值班采用 mask、拒绝 caption；干净的 caption-later 自动合入 r1。
    decision = desk.decide(
        {"program": "gala", "accept": ["mask"], "reject": ["caption"], "by": "duty"}
    )
    assert decision["revision"] == 1
    assert decision["release"]["edits"] == ["mask", "caption-later"]
    assert decision["scheduled_media"] == 20

    # 5) 两个本地模拟转播端领取；A 在拼接点后确认，B 尚未使用。
    a = LocalBroadcaster(desk, "A", "gala", "main")
    b = LocalBroadcaster(desk, "B", "gala", "main")
    assert a.cast()["unconfirmed_read"] == [1]
    b.cast()
    a.seek(21)
    a.apply(1)

    # 6) 撤回 r1：B 收回待播，A 已播出不能回写，只能等后继版本。
    retract = desk.retract({"program": "gala", "branch": "main", "revision": 1})
    assert [x["name"] for x in retract["reclaimed_for"]] == ["B"]
    assert [x["name"] for x in retract["follow_switch_for"]] == ["A"]

    b.resume(revision=0, media=15)
    b_view = desk.inspect_cast("B")
    assert b_view["continue_revision"] == 0 and b_view["unconfirmed_read"] == []

    # 7) 后继修正版只能约在允许的拼接点；A 在未来拼接点跟进，B 可跳过已收回的 r1。
    desk.submit_edit(
        {"id": "fix", "program": "gala", "branch": "main", "base": "1",
         "span": [30, 40], "target": "person-mask", "change": "blur-v2"}
    )
    desk.decide({"program": "gala", "accept": ["fix"], "media": 30})

    a.resume(revision=1, media=26)
    a_view = desk.inspect_cast("A")
    assert a_view["continue_revision"] == 1
    assert a_view["next_switch"]["revision"] == 2
    assert a_view["next_switch"]["switch_at_media"] == 30
    assert a_view["follow_up"][0]["state"] == "successor_available"

    b.resume(revision=0, media=26)
    assert desk.inspect_cast("B")["unconfirmed_read"] == [2]
    b.apply(2, media=31)  # 不需要补确认已收回的 r1

    # 8) 编辑经历可追溯；项目可持久化后原样恢复。
    events = [e["event"] for e in desk.trace_edit("mask")]
    assert "released" in events
    state_path = Path(__file__).with_name(".demo-state.json")
    desk.save_state(state_path)
    restored = EditChannel.load_state(state_path)
    assert restored.inspect_cast("A")["next_switch"]["revision"] == 2
    state_path.unlink()

    print("demo 通过：冲突边界、tied 绑定、候选合流、拼接点、部分撤回与重连均符合约定。")
    print("A 重连视图摘要：", {
        "continue": a_view["continue_revision"],
        "next_switch_at": a_view["next_switch"]["switch_at_media"],
        "owed_confirmations": a_view["unconfirmed_read"],
        "media_id": a.media_id,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
