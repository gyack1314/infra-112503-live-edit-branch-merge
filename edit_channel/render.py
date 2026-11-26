"""把 JSON 回执渲染成中文演练文本。"""

from __future__ import annotations

import json
from typing import Any


def _span(span: list[int] | None) -> str:
    return f"[{span[0]},{span[1]})" if span else "?"


def _switch_text(nxt: dict[str, Any] | None) -> str:
    if not nxt:
        return "无"
    text = f"r{nxt['revision']}@media {nxt['switch_at_media']}"
    if nxt.get("missed_scheduled_point"):
        text += "（预约点已过，改约后续拼接点）"
    return text


def render_line(line: dict[str, Any]) -> str:
    at = line.get("at")
    kind = line.get("kind")
    prefix = f"[t={at} {kind}]"
    if not line.get("ok"):
        return f"{prefix} ✗ {line.get('error_code')}: {line.get('error')}"

    result = line.get("result", {})
    if kind == "program":
        if result.get("idempotent"):
            return f"{prefix} 节目契约已登记（幂等）{result['program']}/{result['branch']}"
        return (
            f"{prefix} 登记节目 {result['program']}/{result['branch']} "
            f"初始基准 {result['base']} 拼接点 {result['splices']} 绑定 {result['tied']}"
        )

    if kind == "edit":
        eid = result.get("id")
        if result.get("duplicate"):
            return f"{prefix} 编辑 {eid} 重复提交，返回既有状态 {result.get('state')}"
        if result.get("state") == "draft":
            return f"{prefix} 编辑 {eid} 已存草稿 {_span(result.get('span'))} target={result.get('target')}"
        if result.get("conflicted"):
            parts = []
            for c in result.get("conflicts", []):
                tie = "（目标绑定）" if c.get("tied") else ""
                parts.append(
                    f"与 {c['with']} 在 {_span(c['overlap'])} 相交"
                    f"（{c['target']}↔{c['other_target']}）{tie}"
                )
            return f"{prefix} 编辑 {eid} 待值班处理：" + "；".join(parts)
        if result.get("released"):
            return f"{prefix} 编辑 {eid} 已随 r{result['released_in']} 公开"
        return f"{prefix} 编辑 {eid} 检查通过，进入候选 {_span(result.get('span'))} target={result.get('target')}"

    if kind == "decide":
        if not result.get("released"):
            return (
                f"{prefix} 决策已记录但未形成发布 "
                f"accept={result.get('accepted')} reject={result.get('rejected')} "
                f"resubmit={result.get('resubmit')} 未决={result.get('left_conflicted')}"
            )
        rel = result
        return (
            f"{prefix} 发布 r{rel['revision']}，预约媒体点 {rel['scheduled_media']}"
            f"（{rel.get('scheduled_media_ref')}）；编辑 {rel['release_edits']}"
            + (f"；自动合流 {rel['release'].get('bundled_clean')}" if rel["release"].get("bundled_clean") else "")
            + (f"；未决 {rel.get('left_conflicted')}" if rel.get("left_conflicted") else "")
        )

    if kind == "cast":
        return (
            f"{prefix} {result['name']} 领取：继续 r{result['continue_revision']}，"
            f"已读待确认 {result['unconfirmed_read']}，"
            f"下次切换 {_switch_text(result.get('next_switch'))}"
        )

    if kind == "applied":
        return (
            f"{prefix} {result['name']} 确认已应用 r{result.get('confirmed_now')} "
            f"@media={result.get('last_media')}；待确认 {result['unconfirmed_read']}"
        )

    if kind == "retract":
        reclaimed = [x["name"] for x in result.get("reclaimed_for", [])]
        follow = [x["name"] for x in result.get("follow_switch_for", [])]
        return (
            f"{prefix} 撤回 r{result['revision']}：未使用方 {reclaimed} 已收回；"
            f"已使用方 {follow} 保留播出记录并等待未来拼接点跟进切换"
        )

    if kind == "resume":
        when = _switch_text(result.get("next_switch"))
        fu = [f"r{x['retracted_revision']}→{x['state']}" for x in result.get("follow_up", [])]
        return (
            f"{prefix} {result['name']} 重连：继续 r{result['continue_revision']}"
            f"@media={result.get('last_media')}，更换安排 {when}，"
            f"已读欠确认 {result['unconfirmed_read']}，跟进 {fu}"
        )

    if kind == "inspect":
        if "history" in result:
            events = " → ".join(h["event"] for h in result.get("history", []))
            return (
                f"{prefix} 编辑 {result['id']} 状态={result['state']} "
                f"公开=r{result.get('released_in')} 经历：{events}"
            )
        return (
            f"{prefix} 转播方 {result.get('name')} 继续 r{result.get('continue_revision')}，"
            f"欠确认 {result.get('unconfirmed_read')}，跟进 {len(result.get('follow_up', []))} 项"
        )

    return f"{prefix} {json.dumps(result, ensure_ascii=False)}"
