"""编辑消息通道的行为测试，逐条对应任务说明中的协调规则。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from edit_channel import (
    EditChannel,
    LocalBroadcaster,
    PredecessorNotConfirmed,
    SpliceRequired,
    UnknownBase,
    run_path,
)
from edit_channel.spans import first_usable_splice, overlap_interval, overlaps

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"


def program_message(**over):
    record = {
        "program": "P",
        "branch": "full",
        "base": "v0",
        "splices": [10, 20, 30, 40],
        "tied": [["occlusion", "overlay"]],
    }
    record.update(over)
    return {"at": 0, "kind": "program", "record": record}


def edit_message(eid, start, end, target, *, base="v0", at=1, change=None, **extra):
    record = {
        "id": eid,
        "program": "P",
        "branch": "full",
        "base": base,
        "span": [start, end],
        "target": target,
        "change": change if change is not None else f"change-{eid}",
    }
    record.update(extra)
    return {"at": at, "kind": "edit", "record": record}


def feed(channel, rows):
    return [channel.handle(row) for row in rows]


class SpanRuleTests(unittest.TestCase):
    def test_half_open_overlap_and_touching_boundary(self):
        self.assertTrue(overlaps((20, 40), (30, 39)))
        self.assertEqual(overlap_interval((20, 40), (30, 39)), (30, 39))
        # [20,40) 与 [40,60) 只在 40 相接，不相交。
        self.assertFalse(overlaps((20, 40), (40, 60)))
        self.assertIsNone(overlap_interval((20, 40), (40, 60)))
        self.assertFalse(overlaps((30, 39), (40, 60)))

    def test_first_usable_splice_inside_target(self):
        point, warnings = first_usable_splice([(20, 40), (40, 60)], [20, 40, 60])
        self.assertEqual(point, 20)
        self.assertEqual(warnings, [])
        point, _ = first_usable_splice([(11, 19)], [10, 20, 30])
        self.assertEqual(point, 20)  # 区间内没有，取不早于起点的首个拼接点


class EditSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.desk = EditChannel()
        self.desk.handle(program_message())

    def test_edit_requires_contract_fields(self):
        line = self.desk.handle({"at": 1, "kind": "edit", "record": {"id": "x"}})
        self.assertFalse(line["ok"])
        self.assertEqual(line["error_code"], "invalid_record")
        self.assertIn("missing", line["result"]["detail"])

    def test_unknown_program_and_base_are_reported(self):
        line = self.desk.handle(
            edit_message("x", 10, 20, "tone", base="nope")
        )
        self.assertFalse(line["ok"])
        self.assertEqual(line["error_code"], "unknown_base")
        with self.assertRaises(UnknownBase):
            self.desk.submit_edit(
                {
                    "id": "y",
                    "program": "P",
                    "branch": "full",
                    "base": "ghost",
                    "span": [1, 2],
                    "target": "t",
                    "change": 1,
                }
            )

    def test_disjoint_spans_are_independent_even_for_related_targets(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "occlusion", at=1),
                edit_message("c", 40, 60, "overlay", at=2),
            ],
        )
        a = self.desk.inspect_edit("a")
        c = self.desk.inspect_edit("c")
        self.assertEqual(a["state"], "staged")
        self.assertEqual(c["state"], "staged")
        self.assertEqual(c["conflicts"], [])

    def test_different_independent_targets_overlap_but_do_not_conflict(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "tone", at=1),
                edit_message("b", 25, 35, "caption", at=2),
            ],
        )
        self.assertEqual(self.desk.inspect_edit("a")["state"], "staged")
        self.assertEqual(self.desk.inspect_edit("b")["state"], "staged")

    def test_same_target_overlap_conflicts(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "caption", at=1),
                edit_message("b", 30, 50, "caption", at=2),
            ],
        )
        b = self.desk.inspect_edit("b")
        self.assertTrue(b["conflicted"])
        self.assertEqual(b["conflicts_with"], ["a"])
        self.assertEqual(b["overlap_intervals"], [[30, 40]])

    def test_tied_targets_conflict_even_when_fields_differ(self):
        feed(
            self.desk,
            [
                edit_message("mask", 20, 40, "occlusion", at=1),
                edit_message("cover", 30, 39, "overlay", at=2),
            ],
        )
        cover = self.desk.inspect_edit("cover")
        self.assertTrue(cover["conflicted"])
        conflict = cover["conflicts"][0]
        self.assertEqual(conflict["with"], "mask")
        self.assertTrue(conflict["tied"])
        self.assertEqual(conflict["overlap"], [30, 39])
        # 先到的一方也被拉出候选，进入对称的冲突状态。
        self.assertEqual(self.desk.inspect_edit("mask")["state"], "conflicted")

    def test_submitter_sees_public_integration(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "tone", at=1),
                {"at": 2, "kind": "decide", "record": {"program": "P", "accept": ["a"], "reject": []}},
            ],
        )
        view = self.desk.inspect_edit("a")
        self.assertTrue(view["released"])
        self.assertTrue(view["integrated_in_public"])
        self.assertEqual(view["released_in"], 1)
        events = [h["event"] for h in view["history"]]
        self.assertIn("released", events)


class DecisionAndReleaseTests(unittest.TestCase):
    def setUp(self):
        self.desk = EditChannel()
        self.desk.handle(program_message())

    def test_revisions_increment_from_one_and_default_splice(self):
        self.desk.handle(edit_message("a", 21, 39, "tone"))
        result = self.desk.decide({"program": "P", "accept": ["a"], "reject": []}, at=2)
        self.assertTrue(result["released"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["scheduled_media"], 30)  # [21,39) 内首个 splice=30
        self.desk.handle(edit_message("b", 40, 50, "caption", at=3))
        result2 = self.desk.decide({"program": "P", "accept": ["b"]}, at=4)
        self.assertEqual(result2["revision"], 2)
        self.assertEqual(result2["scheduled_media"], 40)

    def test_explicit_media_must_be_splice_for_successor(self):
        self.desk.handle(edit_message("a", 10, 20, "tone"))
        with self.assertRaises(SpliceRequired):
            self.desk.decide({"program": "P", "accept": ["a"], "media": 15})
        ok = self.desk.decide({"program": "P", "accept": ["a"], "media": 20})
        self.assertEqual(ok["scheduled_media"], 20)

    def test_accepting_conflicting_pair_is_atomically_rejected(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "occlusion", at=1),
                edit_message("b", 30, 39, "overlay", at=2),
            ],
        )
        with self.assertRaises(Exception) as ctx:
            self.desk.decide({"program": "P", "accept": ["a", "b"]})
        self.assertEqual(getattr(ctx.exception, "code", None), "conflicting_accept")
        # 原子性：没有产生 revision，编辑仍处于冲突状态。
        self.assertEqual(self.desk.find_branch("P")["head"], 0)
        self.assertEqual(self.desk.inspect_edit("a")["state"], "conflicted")

    def test_pick_winner_and_reject_loser(self):
        feed(
            self.desk,
            [
                edit_message("a", 20, 40, "occlusion", at=1),
                edit_message("b", 30, 39, "overlay", at=2),
            ],
        )
        result = self.desk.decide(
            {"program": "P", "accept": ["a"], "reject": ["b"], "by": "duty"}, at=4
        )
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["release_edits"], ["a"])
        self.assertEqual(self.desk.inspect_edit("b")["state"], "rejected")
        a = self.desk.inspect_edit("a")
        self.assertEqual(a["state"], "released")
        self.assertEqual(a["decision"]["by"], "duty")

    def test_require_both_parties_to_resubmit_against_newer_program(self):
        desk = EditChannel()
        desk.handle(program_message())
        feed(
            desk,
            [
                edit_message("x", 20, 40, "occlusion", at=1),
                {"at": 2, "kind": "decide", "record": {"program": "P", "accept": ["x"]}},
                edit_message("a", 25, 35, "occlusion", base="v0", at=3),
                edit_message("b", 28, 38, "overlay", base="v0", at=4),
            ],
        )
        a = desk.inspect_edit("a")
        self.assertTrue(a["conflicted"])
        self.assertEqual(a["conflicts"][0]["other_revision"], 1)
        self.assertTrue(a["conflicts"][0]["since_base"])

        result = desk.decide({"program": "P", "resubmit": ["a", "b"], "note": "请按 r1 重提"}, at=5)
        self.assertFalse(result["released"])
        for eid in ("a", "b"):
            view = desk.inspect_edit(eid)
            self.assertEqual(view["state"], "returned")
            self.assertEqual(
                view["history"][-1]["detail"]["rebase_onto_revision"], 1
            )

    def test_bundle_policy_carries_independent_clean_edit(self):
        desk = EditChannel(policy="bundle")
        desk.handle(program_message())
        feed(
            desk,
            [
                edit_message("a", 20, 40, "occlusion", at=1),
                edit_message("c", 40, 60, "caption", at=2),
            ],
        )
        result = desk.decide({"program": "P", "accept": ["a"], "reject": []}, at=3)
        self.assertEqual(result["release"]["edits"], ["a", "c"])
        self.assertEqual(result["release"]["bundled_clean"], ["c"])
        self.assertEqual(desk.inspect_edit("c")["released_in"], 1)

    def test_accept_only_policy_keeps_clean_edit_candidate(self):
        desk = EditChannel(policy="accept_only")
        desk.handle(program_message())
        feed(
            desk,
            [
                edit_message("a", 20, 40, "occlusion", at=1),
                edit_message("c", 40, 60, "caption", at=2),
            ],
        )
        result = desk.decide({"program": "P", "accept": ["a"]}, at=3)
        self.assertEqual(result["release"]["edits"], ["a"])
        c = desk.inspect_edit("c")
        self.assertEqual(c["state"], "staged")
        self.assertIsNone(c["released_in"])
        self.assertEqual(c["rebase_onto"], 1)

    def test_old_base_independent_edit_keeps_flowing_after_release(self):
        desk = EditChannel()
        desk.handle(program_message())
        desk.handle(edit_message("a", 20, 40, "occlusion", at=1))
        desk.decide({"program": "P", "accept": ["a"]}, at=2)
        # 基于 v0 的不相交编辑仍可接入，并进入下一候选。
        line = desk.handle(edit_message("c", 40, 60, "caption", at=3))
        self.assertTrue(line["ok"])
        self.assertEqual(line["result"]["state"], "staged")


class BroadcasterLifecycleTests(unittest.TestCase):
    def _scenario(self):
        desk = EditChannel()
        desk.handle(program_message(splices=[10, 20, 30]))
        desk.handle(edit_message("look", 10, 20, "tone", at=1))
        desk.handle({"at": 2, "kind": "decide", "record": {"program": "P", "accept": ["look"]}})
        return desk

    def test_cast_marks_read_pending_confirmation(self):
        desk = self._scenario()
        view = desk.cast({"name": "A", "program": "P", "branch": "full"}, at=3)
        self.assertEqual(view["unconfirmed_read"], [1])
        self.assertEqual(view["next_switch"]["revision"], 1)
        self.assertEqual(view["next_switch"]["switch_at_media"], 10)

    def test_cannot_skip_applicable_predecessor(self):
        desk = self._scenario()
        desk.handle(edit_message("look2", 20, 30, "tone", at=4))
        desk.decide({"program": "P", "accept": ["look2"]}, at=5)
        desk.cast({"name": "A", "program": "P", "branch": "full"})
        # cast 一次领取链条上的两个版本，都标记为已读待确认。
        view = desk.inspect_cast("A")
        self.assertEqual(view["unconfirmed_read"], [1, 2])
        with self.assertRaises(PredecessorNotConfirmed) as ctx:
            desk.applied(
                {"name": "A", "program": "P", "branch": "full", "revision": 2, "media": 21}
            )
        self.assertEqual(ctx.exception.detail["missing_revisions"], [1])
        # 按顺序确认后可以前进。
        desk.applied({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 11})
        ok = desk.applied(
            {"name": "A", "program": "P", "branch": "full", "revision": 2, "media": 21}
        )
        self.assertEqual(ok["confirmed_revision"], 2)

    def test_partial_retract_reclaims_unused_and_holds_used_for_follow_switch(self):
        desk = self._scenario()
        desk.cast({"name": "A", "program": "P", "branch": "full"}, at=3)
        desk.cast({"name": "B", "program": "P", "branch": "full"}, at=3)
        desk.applied(
            {"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 11}, at=4
        )
        retract = desk.retract({"program": "P", "branch": "full", "revision": 1}, at=5)
        self.assertEqual([x["name"] for x in retract["reclaimed_for"]], ["B"])
        used = retract["follow_switch_for"][0]
        self.assertEqual(used["name"], "A")
        self.assertEqual(used["earliest_future_splice"], 20)

        b = desk.resume(
            {"name": "B", "program": "P", "branch": "full", "revision": 0, "media": 8}, at=6
        )
        self.assertEqual(b["continue_revision"], 0)
        self.assertEqual(b["unconfirmed_read"], [])
        self.assertEqual(b["next_switch"], None)

        a = desk.resume(
            {"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 16}, at=7
        )
        self.assertEqual(a["continue_revision"], 1)  # 不能回写为未播
        self.assertEqual(a["unconfirmed_read"], [])
        self.assertEqual(a["follow_up"][0]["state"], "awaiting_successor")
        self.assertIsNone(a["next_switch"])

    def test_successor_routes_used_party_forward_and_lets_unused_party_skip(self):
        desk = self._scenario()
        desk.cast({"name": "A", "program": "P", "branch": "full"})
        desk.cast({"name": "B", "program": "P", "branch": "full"})
        desk.applied({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 11})
        desk.retract({"program": "P", "branch": "full", "revision": 1})
        desk.resume({"name": "B", "program": "P", "branch": "full", "revision": 0, "media": 8})
        desk.resume({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 16})

        desk.handle(edit_message("correction", 20, 30, "tone", at=8))
        decide2 = desk.decide({"program": "P", "accept": ["correction"], "media": 20}, at=9)
        self.assertEqual(
            decide2["follow_switch_decisions"],
            [
                {
                    "name": "A",
                    "retracted_revision": 1,
                    "successor_revision": 2,
                    "switch_at_media": 20,
                    "only_at_allowed_splice": True,
                }
            ],
        )

        a = desk.inspect_cast("A")
        self.assertEqual(a["next_switch"]["revision"], 2)
        self.assertEqual(a["next_switch"]["switch_at_media"], 20)
        self.assertEqual(a["follow_up"][0]["state"], "successor_available")
        self.assertEqual(a["follow_up"][0]["successor_revision"], 2)

        b = desk.inspect_cast("B")
        # r1 已对 B 收回，B 可以直接使用后继 r2，不被要求补确认 r1。
        self.assertEqual(b["next_switch"]["revision"], 2)
        self.assertEqual(b["unconfirmed_read"], [])  # inspect 只读，不产生新领取
        b_reconnect = desk.resume(
            {"name": "B", "program": "P", "branch": "full", "revision": 0, "media": 18}
        )
        self.assertEqual(b_reconnect["unconfirmed_read"], [2])
        applied_b = desk.applied(
            {"name": "B", "program": "P", "branch": "full", "revision": 2, "media": 21}
        )
        self.assertEqual(applied_b["confirmed_revision"], 2)

        # A 播放头若已越过预约点 20，则建议改约之后的拼接点 30。
        desk.resume({"name": "A", "program": "P", "branch": "full", "revision": 1, "media": 25})
        a_late = desk.inspect_cast("A")
        self.assertEqual(a_late["next_switch"]["missed_scheduled_point"], True)
        self.assertEqual(a_late["next_switch"]["switch_at_media"], 30)

    def test_non_subscriber_of_branch_is_not_counted(self):
        desk = self._scenario()
        # 另一个分支的转播方。
        desk.handle(
            program_message(branch="alt", base="v0", splices=[5, 15], tied=[])
        )
        desk.cast({"name": "Other", "program": "P", "branch": "alt"})
        desk.cast({"name": "A", "program": "P", "branch": "full"})
        retract = desk.retract({"program": "P", "branch": "full", "revision": 1})
        self.assertEqual(retract["subscribers"], ["A"])
        self.assertNotIn("Other", retract["subscribers"])

    def test_reconnect_lists_read_versions_still_owed_confirmation(self):
        desk = self._scenario()
        desk.cast({"name": "Z", "program": "P", "branch": "full"})
        view = desk.resume(
            {"name": "Z", "program": "P", "branch": "full", "revision": 0, "media": 9}
        )
        self.assertIn(1, view["unconfirmed_read"])
        self.assertEqual(view["pending_confirmations"][0]["owed"], "applied_confirmation")


class LocalEndpointAndPersistenceTests(unittest.TestCase):
    def test_local_broadcaster_endpoint(self):
        desk = EditChannel()
        desk.handle(program_message())
        desk.handle(edit_message("a", 10, 20, "tone"))
        desk.decide({"program": "P", "accept": ["a"], "media": 10})
        a = LocalBroadcaster(desk, "A", "P", "full")
        delivery = a.cast()
        self.assertEqual(delivery["unconfirmed_read"], [1])
        a.seek(11)
        result = a.apply(1)
        self.assertEqual(result["confirmed_revision"], 1)
        self.assertEqual(a.media_id, "urn:media:P/full#11")

    def test_state_save_and_load_roundtrip(self):
        desk = EditChannel()
        desk.handle(program_message())
        desk.handle(edit_message("a", 10, 20, "tone"))
        desk.decide({"program": "P", "accept": ["a"]})
        desk.cast({"name": "A", "program": "P", "branch": "full"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            desk.save_state(path)
            restored = EditChannel.load_state(path)
            self.assertEqual(restored.find_branch("P")["head"], 1)
            self.assertEqual(restored.inspect_cast("A")["unconfirmed_read"], [1])
            self.assertEqual(restored.inspect_edit("a")["released_in"], 1)


class ExampleScenariosTests(unittest.TestCase):
    def test_two_targets_tied_fixture(self):
        outcome = run_path(EXAMPLES / "two-targets-tied.jsonl")
        by_kind = {}
        for line in outcome["lines"]:
            self.assertTrue(line["ok"], msg=line)
            by_kind.setdefault(line["kind"], []).append(line["result"])
        b = next(r for r in by_kind["inspect"] if "history" in r and r["id"] == "b")
        self.assertEqual(b["state"], "conflicted")
        self.assertEqual(b["conflicts"][0]["with"], "a")
        self.assertTrue(b["conflicts"][0]["tied"])
        self.assertEqual(b["conflicts"][0]["overlap"], [30, 39])
        # bundle 默认策略下，独立的 c 随 decide 一并进入 r1 公开。
        c = next(r for r in by_kind["inspect"] if "history" in r and r["id"] == "c")
        self.assertEqual(c["released_in"], 1)
        cast = by_kind["cast"][0]
        self.assertEqual(cast["name"], "partner")
        self.assertEqual(cast["unconfirmed_read"], [1])

    def test_partial_live_withdraw_fixture(self):
        outcome = run_path(EXAMPLES / "partial-live-withdraw.jsonl")
        for line in outcome["lines"]:
            self.assertTrue(line["ok"], msg=line)
        desk = outcome["channel"]
        a = desk.inspect_cast("A")
        b = desk.inspect_cast("B")
        self.assertEqual(a["continue_revision"], 1)
        self.assertEqual(a["follow_up"][0]["state"], "awaiting_successor")
        self.assertEqual(b["continue_revision"], 0)
        self.assertEqual(b["unconfirmed_read"], [])

    def test_accept_only_policy_on_tied_fixture(self):
        outcome = run_path(EXAMPLES / "two-targets-tied.jsonl", policy="accept_only")
        desk = outcome["channel"]
        self.assertIsNone(desk.inspect_edit("c")["released_in"])
        self.assertEqual(desk.find_branch("P")["revisions"]["1"]["edits"], ["a"])

    def test_cli_module_runs_examples(self):
        proc = subprocess.run(
            [sys.executable, "-m", "edit_channel", "examples", "--text"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("two-targets-tied", proc.stdout)
        self.assertIn("跟进切换", proc.stdout)

    def test_cli_jsonl_single_file(self):
        proc = subprocess.run(
            [sys.executable, "simulate.py", "examples/partial-live-withdraw.jsonl"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["ok"] for row in rows))


if __name__ == "__main__":
    unittest.main()
