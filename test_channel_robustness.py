"""补充健壮性测试：多分支隔离、幂等、扁平消息与项目快照。"""

import unittest

from edit_channel import EditChannel


def prog(branch="full", **over):
    rec = {
        "program": "P",
        "branch": branch,
        "base": "v0",
        "splices": [10, 20, 30],
        "tied": [],
    }
    rec.update(over)
    return {"at": 0, "kind": "program", "record": rec}


def ed(eid, s=10, e=20, target="tone", branch="full", **over):
    rec = {
        "id": eid,
        "program": "P",
        "branch": branch,
        "base": "v0",
        "span": [s, e],
        "target": target,
        "change": f"c-{eid}",
    }
    rec.update(over)
    return {"at": 1, "kind": "edit", "record": rec}


class MultiBranchTests(unittest.TestCase):
    def test_revisions_and_casters_are_isolated_per_branch(self):
        desk = EditChannel()
        desk.handle(prog("full"))
        desk.handle(prog("lite", splices=[5, 15]))
        desk.handle(ed("f1", branch="full"))
        desk.handle(ed("l1", 5, 15, branch="lite"))
        desk.decide({"program": "P", "branch": "full", "accept": ["f1"]})
        desk.decide({"program": "P", "branch": "lite", "accept": ["l1"]})
        self.assertEqual(desk.find_branch("P", "full")["head"], 1)
        self.assertEqual(desk.find_branch("P", "lite")["head"], 1)
        self.assertEqual(
            desk.find_branch("P", "full")["revisions"]["1"]["scheduled_media"], 10
        )
        self.assertEqual(
            desk.find_branch("P", "lite")["revisions"]["1"]["scheduled_media"], 5
        )
        # full 的转播方不出现在 lite。
        desk.cast({"name": "A", "program": "P", "branch": "full"})
        retract_lite = desk.retract(
            {"program": "P", "branch": "lite", "revision": 1}
        )
        self.assertEqual(retract_lite["subscribers"], [])

    def test_decide_without_branch_on_multi_branch_program_is_clear_error(self):
        desk = EditChannel()
        desk.handle(prog("full"))
        desk.handle(prog("lite"))
        line = desk.handle({"kind": "decide", "program": "P", "accept": []})
        self.assertFalse(line["ok"])
        self.assertEqual(line["error_code"], "branch_required")


class IdempotencyAndEnvelopeTests(unittest.TestCase):
    def test_duplicate_edit_is_idempotent(self):
        desk = EditChannel()
        desk.handle(prog())
        first = desk.handle(ed("a"))
        second = desk.handle(ed("a"))
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["result"].get("duplicate"))
        self.assertEqual(len(desk.find_branch("P")["edit_order"]), 1)

    def test_conflicting_duplicate_id_is_rejected(self):
        desk = EditChannel()
        desk.handle(prog())
        desk.handle(ed("a", change="x"))
        line = desk.handle(ed("a", change="different"))
        self.assertFalse(line["ok"])
        self.assertEqual(line["error_code"], "duplicate_edit")

    def test_flat_message_shape_is_supported(self):
        desk = EditChannel()
        line = desk.handle(
            {"at": 0, "kind": "program", "program": "Q", "branch": "b",
             "base": "v0", "splices": [1], "tied": []}
        )
        self.assertTrue(line["ok"], line)
        line = desk.handle(
            {"kind": "edit", "id": "e", "program": "Q", "branch": "b",
             "base": "v0", "span": [0, 1], "target": "t", "change": 1}
        )
        self.assertEqual(line["result"]["state"], "staged")

    def test_inspect_unknown_returns_error_receipt(self):
        desk = EditChannel()
        desk.handle(prog())
        line = desk.handle({"kind": "inspect", "record": {"edit": "ghost"}})
        self.assertFalse(line["ok"])
        self.assertEqual(line["error_code"], "unknown_edit")

    def test_resubmit_only_decide_creates_no_revision(self):
        desk = EditChannel()
        desk.handle(prog())
        desk.handle(ed("a", 10, 15, "t1"))
        desk.handle(ed("b", 12, 18, "t1"))
        result = desk.decide({"program": "P", "resubmit": ["a", "b"]})
        self.assertFalse(result["released"])
        self.assertIsNone(result["revision"])
        self.assertEqual(desk.find_branch("P")["head"], 0)
        self.assertEqual(desk.inspect_edit("a")["state"], "returned")

    def test_snapshot_is_jsonable_and_shows_conflicts(self):
        import json

        desk = EditChannel()
        desk.handle(prog(tied=[["occlusion", "overlay"]]))
        desk.handle(ed("a", 10, 20, "occlusion"))
        desk.handle(ed("b", 12, 18, "overlay"))
        snap = desk.snapshot()
        json.dumps(snap)  # 不抛即可
        branch = snap["programs"]["P"]["branches"]["full"]
        self.assertEqual(branch["edits"]["b"]["conflicts_with"], ["a"])


if __name__ == "__main__":
    unittest.main()
