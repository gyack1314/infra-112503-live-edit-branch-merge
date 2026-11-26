"""编辑消息通道的异常类型。

所有可预期的契约/流程错误都继承 :class:`DeskError`，
消息入口会把它们转换成 ``ok=false`` 的回执，而不是让整条演练中断。
"""

from __future__ import annotations

from typing import Any


class DeskError(ValueError):
    """通道处理失败。``code`` 供程序化判断，``detail`` 携带结构化上下文。"""

    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, **detail: Any):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail

    def to_receipt(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": str(self),
            "error_code": self.code,
            "detail": self.detail,
        }


class ValidationError(DeskError):
    code = "invalid_record"


class UnknownProgram(DeskError):
    code = "unknown_program"


class UnknownEdit(DeskError):
    code = "unknown_edit"


class UnknownCaster(DeskError):
    code = "unknown_caster"


class UnknownRevision(DeskError):
    code = "unknown_revision"


class UnknownBase(DeskError):
    code = "unknown_base"


class StaleDecisionBase(DeskError):
    code = "stale_decision_base"


class PredecessorNotConfirmed(DeskError):
    code = "predecessor_unconfirmed"


class SpliceRequired(DeskError):
    code = "splice_required"


class ConflictingRelease(DeskError):
    code = "conflicting_accept"
