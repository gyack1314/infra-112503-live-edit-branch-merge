"""媒体区间、拼接点与媒体标识的纯函数工具。

节目时间用整数帧位置；区间统一为半开区间 ``[start, end)``，
因此 ``[20, 40)`` 与 ``[40, 60)`` 只是相接，不算相交。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .errors import ValidationError


def parse_span(value: Any) -> tuple[int, int]:
    """把消息里的两元素区间解析成 ``(start, end)`` 半开区间。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValidationError("span 必须是 [start, end] 两元素区间")
    start, end = value
    if isinstance(start, bool) or isinstance(end, bool) or type(start) is not int or type(end) is not int:
        raise ValidationError("span 的端点必须是整数帧位置")
    if start < 0 or end < 0:
        raise ValidationError("媒体区间端点不能为负")
    if start >= end:
        raise ValidationError("span 必须满足 start < end（半开区间）")
    return start, end


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """半开区间严格相交；端点相接（如 40 == 40）不相交。"""
    return a[0] < b[1] and b[0] < a[1]


def overlap_interval(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int] | None:
    if not overlaps(a, b):
        return None
    return max(a[0], b[0]), min(a[1], b[1])


def union_span(spans: Iterable[tuple[int, int]]) -> tuple[int, int]:
    spans = list(spans)
    if not spans:
        raise ValidationError("发布版本至少要包含一个编辑区间")
    return min(s for s, _ in spans), max(e for _, e in spans)


def normalize_splices(values: Sequence[Any]) -> list[int]:
    if not isinstance(values, (list, tuple)):
        raise ValidationError("splices 应为整数拼接点数组")
    points: set[int] = set()
    for point in values:
        if isinstance(point, bool) or type(point) is not int or point < 0:
            raise ValidationError("拼接点必须是非负整数")
        points.add(point)
    return sorted(points)


def first_usable_splice(
    spans: Iterable[tuple[int, int]], splices: Sequence[int]
) -> tuple[int | None, list[str]]:
    """返回目标区间可用的首个拼接点。

    优先取落在目标并集 ``[lo, hi)`` 内的最小拼接点；
    若区间内部没有，则取不早于 ``lo`` 的第一个允许拼接点
    （可能恰好落在 ``hi`` 或更后，此时给出告警）。
    没有任何拼接契约时退回区间起点并告警。
    """
    warnings: list[str] = []
    lo, hi = union_span(spans)
    splices = list(splices)
    inside = [p for p in splices if lo <= p < hi]
    if inside:
        return min(inside), warnings
    at_or_after = [p for p in splices if p >= lo]
    if at_or_after:
        point = min(at_or_after)
        if point >= hi:
            warnings.append(
                f"首个可用拼接点 {point} 已不早于区间末端 {hi}，该区间可能已经播过"
            )
        return point, warnings
    warnings.append("节目契约未提供可用拼接点，退回目标区间起点")
    return lo, warnings


def next_splice_after(splices: Sequence[int], media: int | None) -> int | None:
    """指定播放头之后的第一个未来拼接点（严格大于当前媒体位置）。"""
    if media is None:
        return min(splices) if splices else None
    future = [p for p in splices if p > media]
    return min(future) if future else None


def media_ref(program: str, branch: str, frame: int | None = None) -> str:
    """稳定的本地媒体标识。

    不带帧位置时返回分支时间线标识 ``urn:media:P/full``；
    带帧位置时返回该帧的 URN，例如 ``urn:media:P/full#11``。
    """
    base = f"urn:media:{program}/{branch}"
    return base if frame is None else f"{base}#{frame}"


def short_media_ref(program: str, branch: str, frame: int | None = None) -> str:
    base = f"{program}/{branch}"
    return base if frame is None else f"{base}@{frame}"
