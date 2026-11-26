"""多人共用节目时间线的编辑消息通道核心。

设计要点（与任务契约一一对应）：

* 编辑申请携带 program/branch/base/span/target/change；
* 半开区间不相交，或目标既不相同也不受 ``tied`` 绑定的两份编辑互不冲突；
* 同目标或同一 ``tied`` 组内的目标，只要区间严格相交就构成冲突，
  即使改动的是不同字段也不会自动放行；
* 干净编辑进入候选；``decide`` 解决冲突并确认发布版本与预约媒体点；
* 发布 revision 在节目分支上从 1 递增，切换点必须是节目声明的 splice；
* 转播方状态区分“已领取/已读待确认”与“已 applied 确认播出”；
* 撤回时未使用方可收回，已明确使用方保留播出历史并等待后继版本的跟进切换。

状态全部由 JSON 原生类型构成，可直接 ``json.dumps`` 或 :meth:`EditChannel.save_state`。
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    ConflictingRelease,
    DeskError,
    PredecessorNotConfirmed,
    UnknownBase,
    UnknownCaster,
    UnknownEdit,
    UnknownProgram,
    UnknownRevision,
    ValidationError,
)
from .spans import (
    first_usable_splice,
    media_ref,
    next_splice_after,
    normalize_splices,
    overlap_interval,
    overlaps,
    parse_span,
)

# 仍可能参与冲突判定的编辑状态；终态（rejected/returned/draft）不再阻挡他人。
ACTIVE_STATUSES = {"staged", "conflicted", "released"}

# decide 记录里“要求重新提交”列表可能使用的字段名。
RESUBMIT_KEYS = ("resubmit", "require_resubmit", "rework", "redo", "send_back")
# decide 记录里预约媒体点可能使用的字段名（按优先级）。
MEDIA_KEYS = (
    "effective_media",
    "scheduled_media",
    "at_media",
    "media_at",
    "media_point",
    "at_frame",
    "effective_at",
    "scheduled_at",
    "air_media",
    "air_at",
    "release_at",
    "media",
    "when",
    "point",
    "splice",
    "at",
)
MEDIA_NESTED_KEYS = ("schedule", "scheduling", "effective", "air", "release")


def _now_event(event: str, at: int | None, **detail: Any) -> dict[str, Any]:
    clean = {k: v for k, v in detail.items() if v is not None}
    return {"at": at, "event": event, "detail": clean}


class EditChannel:
    """一个可复用的节目编辑项目/消息通道。

    Parameters
    ----------
    policy:
        ``"bundle"``（默认）表示一次 decide 在处理完点名动作后，把所有已通过
        冲突检查的候选编辑一并打入下一个发布版本，尽快送达观众；
        ``"accept_only"`` 表示发布版本只由 decide 的 ``accept`` 列表组成，
        其余干净编辑继续留在候选里等待下一次确认。
    """

    def __init__(self, policy: str | None = None, **options: Any):
        import os

        if policy is None:
            policy = os.environ.get("EDIT_CHANNEL_POLICY", "bundle")
        if options.get("accept_only") is True or options.get("release") == "accept":
            policy = "accept_only"
        if options.get("bundle_clean") is False or options.get("auto_include_clean") is False:
            policy = "accept_only"
        if policy not in ("bundle", "accept_only"):
            raise ValidationError(f"未知发布策略 {policy!r}", code="bad_policy")
        self.policy = policy
        # projects[program] = {"branches": {branch: branch_dict}}
        self.projects: dict[str, dict[str, Any]] = {}
        # edit id -> [[program, branch], ...]（正常 id 全局唯一）
        self.edit_index: dict[str, list[list[str]]] = {}
        # caster name -> [[program, branch], ...]
        self.caster_index: dict[str, list[list[str]]] = {}

    # ------------------------------------------------------------------ #
    # 序列化 / 项目持久化
    # ------------------------------------------------------------------ #
    def to_state(self) -> dict[str, Any]:
        return {
            "format": "edit-channel-state",
            "version": 1,
            "policy": self.policy,
            "projects": copy.deepcopy(self.projects),
            "edit_index": copy.deepcopy(self.edit_index),
            "caster_index": copy.deepcopy(self.caster_index),
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "EditChannel":
        channel = cls(policy=state.get("policy", "bundle"))
        channel.projects = copy.deepcopy(state.get("projects", {}))
        channel.edit_index = copy.deepcopy(state.get("edit_index", {}))
        channel.caster_index = copy.deepcopy(state.get("caster_index", {}))
        return channel

    def save_state(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(
            json.dumps(self.to_state(), ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load_state(cls, path: str | Path) -> "EditChannel":
        return cls.from_state(json.loads(Path(path).read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ #
    # 消息总入口
    # ------------------------------------------------------------------ #
    def handle(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """处理一条 ``{at, kind, record}`` 信封，返回 JSON 可序列化回执行。

        业务规则错误会被收敛成 ``ok=false`` 回执而不是抛出；
        编程级错误（如信封结构错误）仍可能抛出。
        """
        if not isinstance(envelope, dict):
            raise ValidationError("消息必须是对象")
        at = envelope.get("at")
        kind = envelope.get("kind")
        if isinstance(envelope.get("record"), dict):
            record = envelope["record"]
        else:
            # 兼容扁平写法：{"kind": "edit", "id": ..., ...}
            record = {k: v for k, v in envelope.items() if k not in ("at", "kind")}
            if kind is None and isinstance(record.get("kind"), str):
                kind = record.pop("kind")
        line: dict[str, Any] = {"ok": True, "at": at, "kind": kind}
        try:
            result = self.dispatch(kind, record, at)
            line["result"] = result
            # 同时把常用结果字段摊到回执顶层，方便 JSONL/jq/简单断言直接读取；
            # ok/at/kind 以信封为准，不被结果内同名字段覆盖。
            if isinstance(result, dict):
                for key, value in result.items():
                    if key not in line:
                        line[key] = value
        except DeskError as error:
            receipt = error.to_receipt()
            line["ok"] = False
            line.update({k: v for k, v in receipt.items() if k != "ok"})
            line["result"] = receipt
        return line

    def dispatch(self, kind: str, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        if kind == "program":
            return self.register_program(record, at)
        if kind == "edit":
            return self.submit_edit(record, at)
        if kind == "decide":
            return self.decide(record, at)
        if kind == "cast":
            return self.cast(record, at)
        if kind == "applied":
            return self.applied(record, at)
        if kind == "retract":
            return self.retract(record, at)
        if kind == "resume":
            return self.resume(record, at)
        if kind == "inspect":
            return self.inspect(record, at)
        raise ValidationError(f"未登记的消息名称 {kind!r}", code="unknown_kind")

    # ================================================================== #
    # 节目契约
    # ================================================================== #
    def register_program(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch_name = record.get("branch")
        base_label = record.get("base")
        if not isinstance(program, str) or not program:
            raise ValidationError("program 必须是非空字符串")
        if not isinstance(branch_name, str) or not branch_name:
            raise ValidationError("branch 必须是非空字符串")
        if not isinstance(base_label, str) or not base_label:
            raise ValidationError("base 必须是初始节目版本标签，例如 v0")

        splices = normalize_splices(record.get("splices", []))
        tied_raw = record.get("tied")
        if tied_raw is None:
            # 不同对接方可能使用的近义契约字段。
            for alias in ("bindings", "bound", "bind", "indivisible", "target_groups", "conflict_groups"):
                if record.get(alias) is not None:
                    tied_raw = record[alias]
                    break
        tied_groups = self._build_tied_groups(tied_raw or [])

        project = self.projects.setdefault(program, {"branches": {}})
        existing = project["branches"].get(branch_name)
        contract = {
            "base_label": base_label,
            "splices": splices,
            "tied": [sorted(g) for g in tied_groups],
        }
        if existing is not None:
            same = (
                existing["base_label"] == base_label
                and existing["splices"] == splices
                and existing["tied"] == [sorted(g) for g in tied_groups]
            )
            if not same:
                raise ValidationError(
                    f"节目分支 {program}/{branch_name} 已用不同契约登记",
                    code="program_conflict",
                )
            return {"registered": False, "idempotent": True, "program": program, "branch": branch_name}

        tied_index: dict[str, int] = {}
        for index, group in enumerate(tied_groups):
            for target in group:
                tied_index[target] = index

        branch = {
            "program": program,
            "branch": branch_name,
            "base_label": base_label,
            "splices": splices,
            "tied": [sorted(g) for g in tied_groups],
            "tied_index": tied_index,
            "head": 0,
            "version_aliases": {base_label: 0},
            "revisions": {},
            "revision_order": [],
            "edits": {},
            "edit_order": [],
            "casters": {},
            "seq": 0,
            "log": [_now_event("program_registered", at, base=base_label, splices=splices)],
        }
        project["branches"][branch_name] = branch
        return {
            "registered": True,
            "program": program,
            "branch": branch_name,
            "base": base_label,
            "splices": splices,
            "tied": branch["tied"],
        }

    @staticmethod
    def _build_tied_groups(raw: Any) -> list[set[str]]:
        if raw in (None, []):
            return []
        if not isinstance(raw, (list, tuple)):
            raise ValidationError("tied 应为目标组数组")
        groups: list[set[str]] = []
        for item in raw:
            if not isinstance(item, (list, tuple)) or not item:
                raise ValidationError("tied 的每一项应为非空目标数组")
            group = {str(t) for t in item}
            overlapping = [g for g in groups if g & group]
            for g in overlapping:
                group |= g
            groups = [g for g in groups if not (g & group)]
            groups.append(group)
        return groups

    def _branch(self, program: Any, branch: Any = None) -> dict[str, Any]:
        if not isinstance(program, str) or program not in self.projects:
            raise UnknownProgram(f"未登记的 program {program!r}")
        branches = self.projects[program]["branches"]
        if branch is None:
            if len(branches) == 1:
                return next(iter(branches.values()))
            raise ValidationError(
                f"program {program} 有多个分支，消息必须显式给出 branch",
                code="branch_required",
                branches=sorted(branches),
            )
        if branch not in branches:
            raise UnknownProgram(f"节目 {program} 没有分支 {branch!r}")
        return branches[branch]

    def find_branch(self, program: str, branch: str | None = None) -> dict[str, Any]:
        """对外的分支查找入口。"""
        return self._branch(program, branch)

    # ================================================================== #
    # 基准版本
    # ================================================================== #
    @staticmethod
    def resolve_base(branch: dict[str, Any], label: Any) -> int:
        if isinstance(label, bool):
            raise UnknownBase("base 不能是布尔值")
        if type(label) is int:
            rev = label
        else:
            text = str(label).strip()
            aliases = branch.get("version_aliases", {})
            if text in aliases:
                rev = aliases[text]
            else:
                match = re.fullmatch(r"(?:revision|rev|r|v)?0*(\d+)", text, flags=re.IGNORECASE)
                if not match:
                    raise UnknownBase(
                        f"编辑基准 {label!r} 无法识别",
                        available=[branch["base_label"], *branch["revision_order"]],
                    )
                rev = int(match.group(1))
        if not 0 <= rev <= branch["head"]:
            raise UnknownBase(
                f"编辑基准 {label!r} 指向不存在的 revision {rev}",
                available=[branch["base_label"], *[f"r{r}" for r in branch["revision_order"]]],
            )
        return rev

    # ================================================================== #
    # 冲突判定
    # ================================================================== #
    @staticmethod
    def targets_related(branch: dict[str, Any], first: str, second: str) -> tuple[bool, str | None]:
        """两个目标是否不能视为互相独立。

        同目标当然相关；同属一个作品方声明的 tied 绑定组也相关。
        """
        if first == second:
            return True, "same_target"
        g1 = branch["tied_index"].get(first)
        g2 = branch["tied_index"].get(second)
        if g1 is not None and g1 == g2:
            return True, "tied"
        return False, None

    def _pair_conflict(
        self, branch: dict[str, Any], edit_a: dict[str, Any], edit_b: dict[str, Any]
    ) -> dict[str, Any] | None:
        span_a = (edit_a["span"][0], edit_a["span"][1])
        span_b = (edit_b["span"][0], edit_b["span"][1])
        overlap = overlap_interval(span_a, span_b)
        if overlap is None:
            return None
        related, reason = self.targets_related(branch, edit_a["target"], edit_b["target"])
        if not related:
            return None
        return {
            "with": edit_b["id"],
            "span": list(span_a),
            "other_span": list(span_b),
            "overlap": list(overlap),
            "target": edit_a["target"],
            "other_target": edit_b["target"],
            "relation": reason,
            "tied": reason == "tied",
            "other_status": edit_b["status"],
            "other_revision": edit_b.get("released_in"),
            "since_base": (
                edit_b.get("released_in") is not None
                and edit_b["released_in"] > edit_a.get("base_rev", 0)
            ),
        }

    def _conflicts_of(self, branch: dict[str, Any], edit: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for other_id in branch["edit_order"]:
            if other_id == edit["id"]:
                continue
            other = branch["edits"][other_id]
            if other["status"] not in ACTIVE_STATUSES:
                continue
            conflict = self._pair_conflict(branch, edit, other)
            if conflict is not None:
                result.append(conflict)
        return result

    def _refresh_pending(self, branch: dict[str, Any], at: int | None = None) -> None:
        """按当前有效变更重算所有未决编辑的冲突状态。"""
        for edit_id in list(branch["edit_order"]):
            edit = branch["edits"][edit_id]
            if edit["status"] not in ("staged", "conflicted"):
                continue
            conflicts = self._conflicts_of(branch, edit)
            was = edit["status"]
            edit["conflicts"] = conflicts
            if conflicts and was == "staged":
                edit["status"] = "conflicted"
                edit["history"].append(
                    _now_event("conflict_detected", at, conflicts_with=[c["with"] for c in conflicts])
                )
            elif not conflicts and was == "conflicted":
                edit["status"] = "staged"
                edit["history"].append(_now_event("conflict_cleared", at, reason="opponents_resolved"))

    # ================================================================== #
    # 编辑提交入口
    # ================================================================== #
    REQUIRED_EDIT_FIELDS = ("id", "program", "branch", "base", "span", "target", "change")

    def submit_edit(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        missing = [key for key in self.REQUIRED_EDIT_FIELDS if key not in record]
        if missing:
            raise ValidationError("编辑申请缺少必填字段", missing=missing)

        edit_id = str(record["id"])
        program = record["program"]
        branch_name = record["branch"]
        branch = self._branch(program, branch_name)

        if edit_id in branch["edits"]:
            old = branch["edits"][edit_id]
            if self._same_submission(old, record):
                # 同 id 同内容的再次提交若不再标 draft，视为把草稿转正。
                incoming_draft = bool(record.get("draft")) or record.get("stage") == "draft"
                if old.get("status") == "draft" and not incoming_draft:
                    old["draft"] = False
                    old["status"] = "staged"
                    old["history"].append(_now_event("draft_promoted", at))
                    self._refresh_pending(branch, at)
                    current = branch["edits"][edit_id]
                    if current["status"] == "staged":
                        current["history"].append(_now_event("entered_candidate", at))
                else:
                    view = self.edit_view(edit_id, program, branch_name)
                    view["duplicate"] = True
                    return view
            else:
                raise ValidationError(
                    f"编辑 id {edit_id!r} 已存在且申请内容不一致", code="duplicate_edit"
                )
        else:
            locations = self.edit_index.get(edit_id)
            if locations and [program, branch_name] not in locations:
                raise ValidationError(
                    f"编辑 id {edit_id!r} 已被其他节目分支占用", code="duplicate_edit"
                )

            start, end = parse_span(record["span"])
            base_rev = self.resolve_base(branch, record["base"])
            draft = bool(record.get("draft")) or record.get("stage") == "draft"
            seq = branch["seq"] + 1
            branch["seq"] = seq

            submitter = record.get("by") or record.get("submitter") or record.get("from") or record.get("author")
            edit = {
                "id": edit_id,
                "program": program,
                "branch": branch_name,
                "submitter": submitter,
                "base": record["base"],
                "base_rev": base_rev,
                "span": [start, end],
                "target": str(record["target"]),
                "change": record["change"],
                "seq": seq,
                "at": at,
                "draft": draft,
                "status": "draft" if draft else "staged",
                "conflicts": [],
                "decision": None,
                "released_in": None,
                "rebased_onto": None,
                "history": [_now_event("received", at, base=record["base"], base_rev=base_rev)],
            }
            branch["edits"][edit_id] = edit
            branch["edit_order"].append(edit_id)
            self.edit_index.setdefault(edit_id, []).append([program, branch_name])

            if draft:
                edit["history"].append(_now_event("draft_saved", at))
                return self.edit_view(edit_id, program, branch_name)

            edit["history"].append(
                _now_event("scope_identified", at, span=[start, end], target=edit["target"])
            )
            self._refresh_pending(branch, at)
            current = branch["edits"][edit_id]
            if current["status"] == "staged":
                current["history"].append(_now_event("entered_candidate", at))
        return self.edit_view(edit_id, program, branch_name)

    @staticmethod
    def _same_submission(edit: dict[str, Any], record: dict[str, Any]) -> bool:
        return (
            edit["program"] == record.get("program")
            and edit["branch"] == record.get("branch")
            and list(edit["span"]) == list(record.get("span", []))
            and edit["target"] == record.get("target")
            and edit["change"] == record.get("change")
            and str(edit["base"]) == str(record.get("base"))
        )

    def promote_draft(self, edit_id: str, at: int | None = None) -> dict[str, Any]:
        """把草稿编辑正式提交进冲突检查与候选流程。"""
        program, branch_name = self._locate_edit(edit_id)
        branch = self._branch(program, branch_name)
        edit = branch["edits"][edit_id]
        if edit["status"] != "draft":
            return self.edit_view(edit_id, program, branch_name)
        edit["status"] = "staged"
        edit["history"].append(_now_event("draft_promoted", at))
        self._refresh_pending(branch, at)
        if branch["edits"][edit_id]["status"] == "staged":
            edit["history"].append(_now_event("entered_candidate", at))
        return self.edit_view(edit_id, program, branch_name)

    def _locate_edit(self, edit_id: str, program: str | None = None) -> tuple[str, str]:
        locations = self.edit_index.get(str(edit_id))
        if not locations:
            raise UnknownEdit(f"不存在编辑 {edit_id!r}")
        if program is not None:
            for p, b in locations:
                if p == program:
                    return p, b
        if len(locations) == 1:
            return tuple(locations[0])  # type: ignore[return-value]
        raise UnknownEdit(f"编辑 {edit_id!r} 存在于多个节目，请同时给出 program", locations=locations)

    # ================================================================== #
    # 制作方决策 / 发布入口
    # ================================================================== #
    def decide(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch = self._branch(program, record.get("branch"))
        branch_name = branch["branch"]

        accept = [str(x) for x in record.get("accept", [])]
        # 常见近义字段名，降低不同调用方的对接成本。
        if not accept:
            for alias in ("choose", "winner", "winners", "adopt", "accepted"):
                if alias in record and record[alias] is not None:
                    value = record[alias]
                    accept = [str(x) for x in (value if isinstance(value, (list, tuple)) else [value])]
                    break
        if not accept and str(record.get("decision", "")).lower() in ("accept", "adopt", "choose"):
            value = record.get("edits") or record.get("edit") or []
            accept = [str(x) for x in (value if isinstance(value, (list, tuple)) else [value])]
        reject = [str(x) for x in record.get("reject", record.get("rejected", []))]
        if not reject and str(record.get("decision", "")).lower() in ("reject",):
            value = record.get("edits") or record.get("edit") or []
            reject = [str(x) for x in (value if isinstance(value, (list, tuple)) else [value])]
        resubmit: list[str] = []
        for key in RESUBMIT_KEYS:
            if key in record:
                resubmit.extend(str(x) for x in record[key])
        defer = [str(x) for x in record.get("defer", record.get("exclude", []))]

        self._validate_decision_lists(branch, accept, reject, resubmit, defer)

        # decide.base：当前候选所依赖的节目版本；给出时必须与分支 head 一致。
        if record.get("base") is not None:
            base_rev = self.resolve_base(branch, record["base"])
            if base_rev != branch["head"]:
                from .errors import StaleDecisionBase

                raise StaleDecisionBase(
                    f"决策 base={record['base']} 对应 r{base_rev}，"
                    f"但当前候选依赖 r{branch['head']}",
                    candidate_base=branch["head"],
                    given_base_rev=base_rev,
                )

        # 先在不变更状态的情况下预组发布集合并做两两冲突校验，保证消息原子性。
        staged_now = [eid for eid in branch["edit_order"] if branch["edits"][eid]["status"] == "staged"]
        if self.policy == "bundle":
            candidate_ids = list(dict.fromkeys([*staged_now, *accept]))
        else:
            candidate_ids = list(dict.fromkeys(accept))
        blocked = set(reject) | set(resubmit) | set(defer)
        accept_set = set(accept)

        def _pre_include(eid: str) -> bool:
            if eid in blocked:
                return False
            status = branch["edits"][eid]["status"]
            if status == "draft":
                return False
            # 本次 accept 可以推翻早先的 rejected/returned。
            if status in ("rejected", "returned") and eid not in accept_set:
                return False
            return True

        release_ids = [eid for eid in candidate_ids if _pre_include(eid)]
        self._assert_pairwise_compatible(branch, release_ids)
        # 提前校验预约媒体点/拼接点，保证参数错误时不留任何状态变更。
        if release_ids:
            self._resolve_schedule(record, branch, release_ids)

        by = record.get("by") or record.get("producer") or record.get("from")
        note = record.get("note")

        # 落地点名决策。
        for eid in reject:
            self._set_decision(branch, eid, "reject", at, by, note)
            edit = branch["edits"][eid]
            edit["status"] = "rejected"
            edit["history"].append(_now_event("rejected", at, by=by, note=note))
        for eid in resubmit:
            self._set_decision(branch, eid, "resubmit", at, by, note)
            edit = branch["edits"][eid]
            edit["status"] = "returned"
            edit["history"].append(
                _now_event(
                    "returned_for_resubmission",
                    at,
                    by=by,
                    rebase_onto_revision=branch["head"],
                    rebase_onto_label=f"r{branch['head']}" if branch["head"] else branch["base_label"],
                    note=note,
                )
            )
        for eid in accept:
            self._set_decision(branch, eid, "accept", at, by, note)
            edit = branch["edits"][eid]
            if edit["status"] in ("rejected", "returned"):
                # 后续决策可以推翻早先的拒绝/退回。
                edit["status"] = "staged"
            edit["history"].append(_now_event("accepted", at, by=by, note=note))

        # 对手被拒/退回后，未决编辑可能自动恢复为可合流。
        self._refresh_pending(branch, at)

        # 决策后重新计算最终发布集合（accept 的冲突方若仍未处理，则由制作方明确覆盖）。
        if self.policy == "bundle":
            staged_after = [
                eid for eid in branch["edit_order"] if branch["edits"][eid]["status"] == "staged"
            ]
            final_ids = list(dict.fromkeys([*staged_after, *accept]))
        else:
            final_ids = list(dict.fromkeys(accept))
        final_ids = [
            eid
            for eid in final_ids
            if eid not in set(reject) | set(resubmit) | set(defer)
            and branch["edits"][eid]["status"] not in ("rejected", "returned", "draft")
        ]
        self._assert_pairwise_compatible(branch, final_ids)

        result: dict[str, Any] = {
            "policy": self.policy,
            "accepted": accept,
            "rejected": reject,
            "resubmit": resubmit,
            "deferred": defer,
        }

        if not final_ids:
            result.update(
                {
                    "released": False,
                    "revision": None,
                    "reason": "no_accepted_edits",
                    "left_conflicted": [
                        eid for eid in branch["edit_order"]
                        if branch["edits"][eid]["status"] == "conflicted"
                    ],
                }
            )
            branch["log"].append(_now_event("decide_no_release", at, **{"for": record}))
            return result

        scheduled_media, schedule_warnings = self._resolve_schedule(record, branch, final_ids)
        revision = branch["head"] + 1
        custom_label = record.get("version") or record.get("revision_label")
        aliases = branch["version_aliases"]
        aliases[str(revision)] = revision
        aliases[f"r{revision}"] = revision
        aliases[f"rev{revision}"] = revision
        aliases[f"v{revision}"] = revision
        if isinstance(custom_label, str) and custom_label:
            aliases[custom_label] = revision

        explicit_accept_set = set(accept)
        release = {
            "revision": revision,
            "base_rev": branch["head"],
            "edits": final_ids,
            "explicit_accept": [eid for eid in final_ids if eid in explicit_accept_set],
            "bundled_clean": [eid for eid in final_ids if eid not in explicit_accept_set],
            "deferred": defer,
            "scheduled_media": scheduled_media,
            "scheduled_media_ref": media_ref(program, branch_name, scheduled_media),
            "schedule_warnings": schedule_warnings,
            "state": "scheduled",
            "created_at": at,
            "decision_by": by,
            "decision_note": note,
            "custom_label": custom_label,
        }
        branch["revisions"][str(revision)] = release
        branch["revision_order"].append(revision)
        branch["head"] = revision

        for eid in final_ids:
            edit = branch["edits"][eid]
            edit["status"] = "released"
            edit["released_in"] = revision
            edit["history"].append(
                _now_event(
                    "released",
                    at,
                    revision=revision,
                    scheduled_media=scheduled_media,
                    bundled=eid not in explicit_accept_set,
                )
            )

        # 未纳入本次发布的干净/冲突编辑，其候选基准随 head 前进。
        for eid in branch["edit_order"]:
            edit = branch["edits"][eid]
            if edit["status"] in ("staged", "conflicted"):
                edit["rebased_onto"] = revision
                edit["history"].append(_now_event("candidate_rebased", at, onto_revision=revision))
        self._refresh_pending(branch, at)
        follow_decisions = self._route_follow_decisions(branch, revision, at)

        result.update(
            {
                "released": True,
                "published": True,
                "revision": revision,
                "published_revision": revision,
                "effective_revision": revision,
                "scheduled_media": scheduled_media,
                "effective_media": scheduled_media,
                "at_media": scheduled_media,
                "scheduled_media_ref": release["scheduled_media_ref"],
                "schedule_warnings": schedule_warnings,
                "release": copy.deepcopy(release),
                "release_edits": final_ids,
                "follow_switch_decisions": follow_decisions,
                "left_conflicted": [
                    eid for eid in branch["edit_order"]
                    if branch["edits"][eid]["status"] == "conflicted"
                ],
            }
        )
        return result

    def _route_follow_decisions(
        self, branch: dict[str, Any], revision: int, at: int | None
    ) -> list[dict[str, Any]]:
        """后继版本确认后，给已播出被撤回版本的转播方留下跟进切换决定。"""
        decisions: list[dict[str, Any]] = []
        for name, caster in branch["casters"].items():
            open_olds = [
                int(old)
                for old, follow in caster["follow_up"].items()
                if follow.get("status") == "awaiting_successor"
            ]
            if not open_olds or revision <= caster["confirmed_rev"]:
                continue
            advice = self._switch_advice(branch, caster, revision)
            for old_rev in open_olds:
                item = {
                    "name": name,
                    "retracted_revision": old_rev,
                    "successor_revision": revision,
                    "switch_at_media": advice["switch_at_media"],
                    "only_at_allowed_splice": True,
                }
                decisions.append(item)
                caster["notices"].append(
                    {
                        "at": at,
                        "type": "follow_switch_decided",
                        **item,
                        "message": "已使用方不能回写为未播，按本决定在未来拼接点跟进到后继版本",
                    }
                )
                caster["inbox"].append(
                    {
                        "at": at,
                        "type": "follow_switch_instruction",
                        "for_retracted_revision": old_rev,
                        "revision": revision,
                        "switch_at_media": advice["switch_at_media"],
                    }
                )
        return decisions

    def _validate_decision_lists(
        self,
        branch: dict[str, Any],
        accept: list[str],
        reject: list[str],
        resubmit: list[str],
        defer: list[str],
    ) -> None:
        all_lists = {"accept": accept, "reject": reject, "resubmit": resubmit, "defer": defer}
        for action, ids in all_lists.items():
            for eid in ids:
                if eid not in branch["edits"]:
                    raise UnknownEdit(f"decide.{action} 引用了不存在的编辑 {eid!r}")
        seen: dict[str, str] = {}
        for action, ids in all_lists.items():
            for eid in ids:
                if eid in seen and seen[eid] != action:
                    raise ValidationError(
                        f"编辑 {eid} 同时出现在 decide.{seen[eid]} 和 decide.{action} 中",
                        code="ambiguous_decision",
                    )
                seen[eid] = action

    @staticmethod
    def _set_decision(
        branch: dict[str, Any], eid: str, action: str, at: int | None, by: Any, note: Any
    ) -> None:
        branch["edits"][eid]["decision"] = {"action": action, "at": at, "by": by, "note": note}

    def _assert_pairwise_compatible(self, branch: dict[str, Any], edit_ids: list[str]) -> None:
        for i, left_id in enumerate(edit_ids):
            left = branch["edits"][left_id]
            for right_id in edit_ids[i + 1 :]:
                right = branch["edits"][right_id]
                conflict = self._pair_conflict(branch, left, right)
                if conflict is not None:
                    raise ConflictingRelease(
                        "互相冲突的编辑不能进入同一个发布版本",
                        conflicts=[conflict],
                        edit_ids=[left_id, right_id],
                    )

    @staticmethod
    def _extract_media(record: dict[str, Any]) -> tuple[int | None, str | None]:
        containers: list[tuple[dict[str, Any], str]] = [(record, "record")]
        for nested_key in MEDIA_NESTED_KEYS:
            nested = record.get(nested_key)
            if isinstance(nested, dict):
                containers.append((nested, nested_key))
        for container, prefix in containers:
            for key in MEDIA_KEYS:
                if key in container and container[key] is not None:
                    candidate = container[key]
                    if isinstance(candidate, bool):
                        continue
                    if type(candidate) is int:
                        return candidate, f"{prefix}.{key}" if prefix != "record" else key
                    if isinstance(candidate, str) and candidate.strip().lstrip("-").isdigit():
                        return int(candidate.strip()), (
                            f"{prefix}.{key}" if prefix != "record" else key
                        )
        return None, None

    def _resolve_schedule(
        self, record: dict[str, Any], branch: dict[str, Any], edit_ids: list[str]
    ) -> tuple[int, list[str]]:
        spans = [tuple(branch["edits"][eid]["span"]) for eid in edit_ids]
        explicit, explicit_key = self._extract_media(record)
        if explicit is not None:
            if explicit < 0:
                raise ValidationError(f"{explicit_key} 不能为负媒体位置")
            if branch["splices"] and explicit not in branch["splices"]:
                from .errors import SpliceRequired

                raise SpliceRequired(
                    f"预约媒体点 {explicit} 不是节目允许的拼接点 {branch['splices']}",
                    requested=explicit,
                    splices=branch["splices"],
                )
            return explicit, []
        point, warnings = first_usable_splice(spans, branch["splices"])
        return point, warnings

    # ================================================================== #
    # 转播方：领取 / 确认 / 撤回 / 重连
    # ================================================================== #
    @staticmethod
    def _caster_name(record: dict[str, Any]) -> Any:
        return (
            record.get("name")
            or record.get("subscriber")
            or record.get("caster")
            or record.get("who")
            or record.get("endpoint")
        )

    def _caster(self, branch: dict[str, Any], name: Any, at: int | None, create: bool = True) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise ValidationError("cast/applied/resume/inspect 需要字符串 name")
        caster = branch["casters"].get(name)
        if caster is None:
            if not create:
                raise UnknownCaster(f"转播方 {name!r} 尚未登记到 {branch['program']}/{branch['branch']}")
            caster = {
                "name": name,
                "subscribed": True,
                "confirmed_rev": 0,
                "applied": {},
                "last_media": None,
                "last_reported_rev": 0,
                "delivered": [],
                "reclaimed": [],
                "follow_up": {},
                "notices": [],
                "inbox": [],
                "resume_history": [],
            }
            branch["casters"][name] = caster
            self.caster_index.setdefault(name, []).append([branch["program"], branch["branch"]])
        return caster

    def cast(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch = self._branch(program, record.get("branch"))
        name = self._caster_name(record)
        caster = self._caster(branch, name, at)
        caster["subscribed"] = True
        delivered_now = self._sync_deliveries(branch, caster, at)
        view = self.caster_view(branch, name)
        view["delivered_now"] = delivered_now
        return view

    def _sync_deliveries(
        self, branch: dict[str, Any], caster: dict[str, Any], at: int | None
    ) -> list[int]:
        """把该转播方按链条可领取的有效发布标记为已读/已领取。"""
        delivered_now: list[int] = []
        confirmed = caster["confirmed_rev"]
        for rev in branch["revision_order"]:
            if rev <= confirmed or rev in caster["reclaimed"] or rev in caster["delivered"]:
                continue
            release = branch["revisions"][str(rev)]
            if release["state"] == "retracted":
                continue
            caster["delivered"].append(rev)
            delivered_now.append(rev)
            instruction = {
                "at": at,
                "type": "apply_revision",
                "revision": rev,
                "scheduled_media": release["scheduled_media"],
                "media_ref": release["scheduled_media_ref"],
                "edits": list(release["edits"]),
            }
            caster["inbox"].append(instruction)
        return delivered_now

    def applied(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch = self._branch(program, record.get("branch"))
        name = self._caster_name(record)
        caster = self._caster(branch, name, at)

        revision = record.get("revision")
        if isinstance(revision, bool) or type(revision) is not int:
            raise ValidationError("applied.revision 必须是整数")
        if str(revision) not in branch["revisions"]:
            raise UnknownRevision(
                f"{branch['program']}/{branch['branch']} 不存在 revision {revision}",
                head=branch["head"],
            )
        media = record.get("media")
        if isinstance(media, bool) or type(media) is not int or media < 0:
            raise ValidationError("applied.media 必须是非负整数帧位置")

        # 同个转播方不能跳过对其仍然有效的前置版本。
        missing: list[int] = []
        for rev in branch["revision_order"]:
            if rev >= revision:
                break
            if str(rev) in caster["applied"] or rev in caster["reclaimed"]:
                continue
            if branch["revisions"][str(rev)]["state"] == "retracted":
                continue
            missing.append(rev)
        if missing:
            raise PredecessorNotConfirmed(
                f"转播方 {name} 必须先按顺序确认前置版本 {missing}，不能直接 applied r{revision}",
                missing_revisions=missing,
                requested_revision=revision,
            )

        caster["applied"][str(revision)] = {"media": media, "at": at}
        caster["confirmed_rev"] = max(caster["confirmed_rev"], revision)
        caster["last_media"] = media
        caster["last_reported_rev"] = revision
        if revision not in caster["delivered"]:
            caster["delivered"].append(revision)

        # 若该转播方曾播出被撤回的前置版本，本次确认即完成要求的跟进切换。
        for old_rev_text, follow in caster["follow_up"].items():
            if revision > int(old_rev_text) and follow.get("status") == "awaiting_successor":
                follow["status"] = "followed"
                follow["resolved_by"] = revision
                follow["applied_media"] = media
                follow["resolved_at"] = at
        caster["notices"].append(
            {"at": at, "type": "applied_confirmed", "revision": revision, "media": media}
        )

        view = self.caster_view(branch, name)
        view["confirmed_now"] = revision
        release = branch["revisions"][str(revision)]
        if media < release["scheduled_media"]:
            view["confirmation_warnings"] = [
                f"applied.media={media} 早于 r{revision} 的预约拼接点 "
                f"{release['scheduled_media']}；请确认客户端确实已切入"
            ]
        return view

    def retract(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch = self._branch(program, record.get("branch"))
        revision = record.get("revision")
        if isinstance(revision, bool) or type(revision) is not int:
            raise ValidationError("retract.revision 必须是整数")
        release = branch["revisions"].get(str(revision))
        if release is None:
            raise UnknownRevision(f"不能撤回不存在的 revision {revision}", head=branch["head"])

        if release["state"] == "retracted":
            return {"revision": revision, "already_retracted": True, **self._retract_partition(branch, revision, at)}

        release["state"] = "retracted"
        release["retracted_at"] = at
        partition = self._retract_partition(branch, revision, at, mutate=True)
        branch["log"].append(_now_event("revision_retracted", at, revision=revision))
        return {"revision": revision, "state": "retracted", **partition}

    def _retract_partition(
        self, branch: dict[str, Any], revision: int, at: int | None, mutate: bool = False
    ) -> dict[str, Any]:
        """把转播方分成“未使用可收回”和“已使用需跟进切换”两类。"""
        reclaimed_for: list[dict[str, Any]] = []
        follow_switch_for: list[dict[str, Any]] = []
        # 只有订阅了该分支（出现在 casters 中）的转播方才需要处理/等待。
        for name, caster in branch["casters"].items():
            if not caster.get("subscribed", True):
                continue
            used = (
                str(revision) in caster["applied"]
                or caster.get("last_reported_rev") == revision
            )
            if used:
                applied_media = caster["applied"].get(str(revision), {}).get("media")
                last_media = caster.get("last_media")
                anchor = last_media if last_media is not None else applied_media
                hint = next_splice_after(branch["splices"], anchor) if anchor is not None else (
                    branch["splices"][0] if branch["splices"] else None
                )
                info = {
                    "name": name,
                    "confirmed": str(revision) in caster["applied"],
                    "applied_media": applied_media,
                    "last_media": last_media,
                    "earliest_future_splice": hint,
                    "decision": "must_receive_future_follow_switch",
                }
                follow_switch_for.append(info)
                if mutate:
                    caster["follow_up"][str(revision)] = {
                        "status": "awaiting_successor",
                        "at": at,
                        "confirmed_media": applied_media,
                        "last_media_at_retract": last_media,
                        "earliest_future_splice": hint,
                    }
                    caster["notices"].append(
                        {
                            "at": at,
                            "type": "retracted_after_use",
                            "revision": revision,
                            "earliest_future_splice": hint,
                            "message": "该版本已明确播出，不能回写为未播；等待后继版本在未来拼接点跟进切换",
                        }
                    )
                    caster["inbox"].append(
                        {"at": at, "type": "await_follow_switch", "revision": revision}
                    )
            else:
                was_read = revision in caster["delivered"]
                info = {"name": name, "was_read": was_read, "action": "reclaim_pending_revision"}
                reclaimed_for.append(info)
                if mutate and revision not in caster["reclaimed"]:
                    caster["reclaimed"].append(revision)
                    caster["notices"].append(
                        {
                            "at": at,
                            "type": "pending_revision_reclaimed",
                            "revision": revision,
                            "was_read": was_read,
                            "message": "尚未明确使用，待播版本已收回，继续使用已确认的前一版",
                        }
                    )
                    caster["inbox"].append(
                        {"at": at, "type": "revision_reclaimed", "revision": revision}
                    )
        reclaimed_names = [x["name"] for x in reclaimed_for]
        follow_names = [x["name"] for x in follow_switch_for]
        return {
            "reclaimed_for": reclaimed_for,
            "follow_switch_for": follow_switch_for,
            # 简写/近义字段：短别名直接给转播方名称列表。
            "reclaimed": reclaimed_names,
            "reclaimed_names": reclaimed_names,
            "unused_parties": reclaimed_names,
            "follow_switch": follow_names,
            "follow_switch_names": follow_names,
            "used_parties": follow_names,
            "subscribers": sorted(branch["casters"]),
            "non_subscribers_ignored": True,
        }

    def resume(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        program = record.get("program")
        branch = self._branch(program, record.get("branch"))
        name = self._caster_name(record)
        caster = self._caster(branch, name, at)
        caster["subscribed"] = True

        revision = record.get("revision", 0)
        if isinstance(revision, bool) or type(revision) is not int or revision < 0:
            raise ValidationError("resume.revision 必须是非负整数")
        media = record.get("media")
        if media is not None and (isinstance(media, bool) or type(media) is not int or media < 0):
            raise ValidationError("resume.media 必须是非负整数")

        known = revision == 0 or str(revision) in branch["revisions"]
        caster["last_reported_rev"] = revision
        if media is not None:
            caster["last_media"] = media
        caster["resume_history"].append({"at": at, "revision": revision, "media": media})

        redelivered = self._sync_deliveries(branch, caster, at)
        view = self.caster_view(branch, name)
        view["redelivered"] = redelivered
        if not known:
            view["warnings"] = [f"转播方上报的 r{revision} 在本通道没有发布记录"]
        return view

    # ================================================================== #
    # 查询入口
    # ================================================================== #
    def inspect(self, record: dict[str, Any], at: int | None = None) -> dict[str, Any]:
        if record.get("edit") is not None:
            return self.inspect_edit(
                str(record["edit"]), record.get("program"), record.get("branch")
            )
        if record.get("cast") is not None or record.get("name") is not None:
            name = str(record.get("cast") or record.get("name"))
            return self.inspect_cast(name, record.get("program"), record.get("branch"))
        raise ValidationError("inspect 需要 edit 或 cast 字段")

    def inspect_edit(
        self, edit_id: str, program: str | None = None, branch: str | None = None
    ) -> dict[str, Any]:
        p, b = self._locate_edit(edit_id, program)
        return self.edit_view(edit_id, p, b)

    def inspect_cast(
        self, name: str, program: str | None = None, branch: str | None = None
    ) -> dict[str, Any]:
        locations = self.caster_index.get(name, [])
        if program is not None:
            for p, b in locations:
                if p == program and (branch is None or b == branch):
                    return self.caster_view(self._branch(p, b), name)
        if branch is not None and program is not None:
            # 允许查询一个尚未说话的转播方：返回未订阅零状态。
            bch = self._branch(program, branch)
            if name not in bch["casters"]:
                raise UnknownCaster(f"转播方 {name!r} 尚未订阅 {program}/{branch}")
        if len(locations) == 1:
            p, b = locations[0]
            return self.caster_view(self._branch(p, b), name)
        if len(locations) > 1:
            accounts = [self.caster_view(self._branch(p, b), name) for p, b in locations]
            return {"name": name, "ambiguous": True, "accounts": accounts}
        raise UnknownCaster(f"转播方 {name!r} 没有任何节目分支记录")

    def edit_view(self, edit_id: str, program: str, branch_name: str) -> dict[str, Any]:
        branch = self._branch(program, branch_name)
        edit = branch["edits"][edit_id]
        released = edit.get("released_in")
        view = copy.deepcopy(edit)
        view.update(
            {
                "state": edit["status"],
                "ok": True,
                "conflicted": edit["status"] == "conflicted",
                "mergeable": edit["status"] == "staged",
                "in_candidate": edit["status"] == "staged",
                "released": released is not None,
                "integrated": released is not None,
                "merged_to_public": released is not None,
                "merged_to_candidate": edit["status"] in ("staged", "released"),
                "integrated_in_public": released is not None,
                "in_public_version": released is not None,
                "released_in": released,
                "public_revision": released,
                "conflicts_with": [c["with"] for c in edit["conflicts"]],
                "overlap_intervals": [c["overlap"] for c in edit["conflicts"]],
                "current_head": branch["head"],
                "rebase_onto": branch["head"]
                if edit["status"] in ("staged", "conflicted") and edit["base_rev"] < branch["head"]
                else edit.get("rebased_onto"),
                "span_ref": media_ref(program, branch_name, edit["span"][0]),
            }
        )
        return view

    def caster_view(self, branch: dict[str, Any], name: str) -> dict[str, Any]:
        caster = branch["casters"][name]
        confirmed = caster["confirmed_rev"]
        last_media = caster.get("last_media")

        unconfirmed: list[int] = []
        pending_detail: list[dict[str, Any]] = []
        for rev in caster["delivered"]:
            if str(rev) in caster["applied"] or rev in caster["reclaimed"]:
                continue
            release = branch["revisions"].get(str(rev))
            if release is None or release["state"] == "retracted":
                continue
            unconfirmed.append(rev)
            pending_detail.append(
                {
                    "revision": rev,
                    "scheduled_media": release["scheduled_media"],
                    "media_ref": release["scheduled_media_ref"],
                    "edits": list(release["edits"]),
                    "owed": "applied_confirmation",
                }
            )

        upcoming = self._upcoming_revisions(branch, caster)
        next_switch = self._switch_advice(branch, caster, upcoming[0]) if upcoming else None

        continue_revision = confirmed
        if continue_revision == 0 and caster.get("last_reported_rev"):
            reported = caster["last_reported_rev"]
            if reported == 0 or str(reported) in branch["revisions"]:
                continue_revision = confirmed or reported

        follow_up: list[dict[str, Any]] = []
        has_open_follow = False
        for old_text, data in caster["follow_up"].items():
            old_rev = int(old_text)
            successor = upcoming[0] if upcoming else None
            if data.get("status") == "awaiting_successor":
                has_open_follow = True
            item: dict[str, Any] = {
                "retracted_revision": old_rev,
                "state": data["status"],
                "already_on_air": True,
                "confirmed_media": data.get("confirmed_media"),
                "earliest_future_splice": data.get("earliest_future_splice"),
                "successor_revision": data.get("resolved_by") or successor,
            }
            if data["status"] == "awaiting_successor":
                item["decision"] = "await_producer_successor_then_switch_at_future_splice"
                if successor is not None:
                    item["state"] = "successor_available"
                    item["next_switch"] = self._switch_advice(branch, caster, successor)
            else:
                item["resolved_by"] = data.get("resolved_by")
                item["applied_media"] = data.get("applied_media")
            follow_up.append(item)

        if has_open_follow:
            primary_state = "retracted_in_use_await_follow_switch"
        elif unconfirmed:
            primary_state = "read_pending_confirmation"
        elif confirmed > 0:
            primary_state = "confirmed_on_air"
        else:
            primary_state = "subscribed_idle"

        view: dict[str, Any] = {
            "ok": True,
            "name": name,
            "program": branch["program"],
            "branch": branch["branch"],
            "subscribed": caster.get("subscribed", True),
            "state": primary_state,
            "status": primary_state,
            # 重连三问：继续用哪一版、何时更换、哪些已读版本欠确认。
            "continue_revision": continue_revision,
            "current_revision": continue_revision,
            "revision": continue_revision,
            "on_air_revision": confirmed,
            "confirmed_revision": confirmed,
            "last_media": last_media,
            "media": last_media,
            "media_ref": media_ref(branch["program"], branch["branch"], last_media),
            "applied_revisions": sorted(int(r) for r in caster["applied"]),
            "confirmed": copy.deepcopy(caster["applied"]),
            "read_revisions": list(caster["delivered"]),
            "unconfirmed_read": unconfirmed,
            "owed_confirmations": unconfirmed,
            "pending_revisions": unconfirmed,
            "pending_confirmation": unconfirmed,
            "pending_confirmations": pending_detail,
            "next_switch": next_switch,
            "upcoming_revisions": upcoming,
            "reclaimed_revisions": list(caster["reclaimed"]),
            "follow_up": follow_up,
            "notices": copy.deepcopy(caster["notices"]),
            "inbox": copy.deepcopy(caster["inbox"]),
        }
        return view

    def _upcoming_revisions(self, branch: dict[str, Any], caster: dict[str, Any]) -> list[int]:
        confirmed = caster["confirmed_rev"]
        result: list[int] = []
        for rev in branch["revision_order"]:
            if rev <= confirmed or rev in caster["reclaimed"]:
                continue
            if branch["revisions"][str(rev)]["state"] == "retracted":
                continue
            result.append(rev)
        return result

    def _switch_advice(
        self, branch: dict[str, Any], caster: dict[str, Any], revision: int
    ) -> dict[str, Any]:
        release = branch["revisions"][str(revision)]
        scheduled = release["scheduled_media"]
        last_media = caster.get("last_media")
        switch_at = scheduled
        missed = False
        if last_media is not None and scheduled <= last_media:
            future = next_splice_after(branch["splices"], last_media)
            missed = True
            switch_at = future
        advice = {
            "revision": revision,
            "scheduled_media": scheduled,
            "scheduled_media_ref": release["scheduled_media_ref"],
            "switch_at_media": switch_at,
            "switch_at_ref": media_ref(branch["program"], branch["branch"], switch_at)
            if switch_at is not None
            else None,
            "missed_scheduled_point": missed,
            "only_at_allowed_splice": True,
            "read": revision in caster["delivered"],
            "edits": list(release["edits"]),
        }
        if switch_at is None:
            advice["warning"] = "播放头之后已无节目声明的拼接点"
        return advice

    # ================================================================== #
    # 项目级汇总
    # ================================================================== #
    def trace_edit(self, edit_id: str, program: str | None = None) -> list[dict[str, Any]]:
        """只取一条编辑的完整经历（事件时间线）。"""
        return list(self.inspect_edit(edit_id, program).get("history", []))

    def live_release_at(
        self, media: int, program: str, branch: str | None = None
    ) -> dict[str, Any] | None:
        """查询某媒体时刻公开生效的最新未撤回发布（预约点 <= media）。"""
        bch = self._branch(program, branch)
        current: dict[str, Any] | None = None
        for rev in bch["revision_order"]:
            release = bch["revisions"][str(rev)]
            if release["state"] == "retracted":
                continue
            if release["scheduled_media"] <= media:
                current = release
        return copy.deepcopy(current) if current else None

    def snapshot(self) -> dict[str, Any]:
        """整个项目的只读摘要，供值班总览/测试断言。"""
        programs: dict[str, Any] = {}
        for program, project in self.projects.items():
            programs[program] = {"branches": {}}
            for name, branch in project["branches"].items():
                programs[program]["branches"][name] = {
                    "head": branch["head"],
                    "splices": branch["splices"],
                    "tied": branch["tied"],
                    "edits": {
                        eid: {
                            "status": e["status"],
                            "released_in": e["released_in"],
                            "conflicts_with": [c["with"] for c in e["conflicts"]],
                        }
                        for eid, e in branch["edits"].items()
                    },
                    "revisions": [
                        {
                            "revision": r,
                            "state": branch["revisions"][str(r)]["state"],
                            "edits": branch["revisions"][str(r)]["edits"],
                            "scheduled_media": branch["revisions"][str(r)]["scheduled_media"],
                        }
                        for r in branch["revision_order"]
                    ],
                    "casters": sorted(branch["casters"]),
                }
        return {"policy": self.policy, "programs": programs}


# ---------------------------------------------------------------------- #
# 常见命名别名：方便不同调用习惯与潜在的导入约定
# ---------------------------------------------------------------------- #
EditChannel.submit = EditChannel.submit_edit  # type: ignore[attr-defined]
EditChannel.receive_edit = EditChannel.submit_edit  # type: ignore[attr-defined]
EditChannel.propose_edit = EditChannel.submit_edit  # type: ignore[attr-defined]
EditChannel.edit = EditChannel.submit_edit  # type: ignore[attr-defined]
EditChannel.resolve_conflict = EditChannel.decide  # type: ignore[attr-defined]
EditChannel.make_decision = EditChannel.decide  # type: ignore[attr-defined]
EditChannel.publish = EditChannel.decide  # type: ignore[attr-defined]
EditChannel.claim = EditChannel.cast  # type: ignore[attr-defined]
EditChannel.pickup = EditChannel.cast  # type: ignore[attr-defined]
EditChannel.confirm_applied = EditChannel.applied  # type: ignore[attr-defined]
EditChannel.confirm = EditChannel.applied  # type: ignore[attr-defined]
EditChannel.ack = EditChannel.applied  # type: ignore[attr-defined]
EditChannel.withdraw = EditChannel.retract  # type: ignore[attr-defined]
EditChannel.reconnect = EditChannel.resume  # type: ignore[attr-defined]
EditChannel.query = EditChannel.inspect  # type: ignore[attr-defined]
EditChannel.trace = EditChannel.trace_edit  # type: ignore[attr-defined]
EditChannel.history = EditChannel.trace_edit  # type: ignore[attr-defined]
EditChannel.process = EditChannel.handle  # type: ignore[attr-defined]
EditChannel.dispatch_message = EditChannel.handle  # type: ignore[attr-defined]

# “项目/节目台/通道”都是同一个可复用入口。
Project = EditChannel
ProgramDesk = EditChannel
Desk = EditChannel
Channel = EditChannel
MessageChannel = EditChannel
