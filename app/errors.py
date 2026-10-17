"""应用异常：领域层抛，Web 层统一翻译成状态码与中文提示。

约定：凡是用户能看懂并能自己纠正的问题（填错数、缺字段、没权限），用 `UserError`；
凡是程序/数据完整性问题（快照对不上、审计链断了），用 `IntegrityError` 并显式
告知"这不是你操作错了"。两者都不允许被静默吞掉。
"""
from __future__ import annotations

__all__ = [
    "AppError",
    "UserError",
    "ValidationError",
    "NotFound",
    "PermissionDenied",
    "DataIntegrityError",
    "Conflict",
]


class AppError(Exception):
    """应用层异常基类。"""

    status = 400

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class UserError(AppError):
    """用户可自行纠正的输入问题。"""

    status = 400


class ValidationError(UserError):
    status = 422


class NotFound(AppError):
    status = 404


class PermissionDenied(AppError):
    status = 403


class Conflict(AppError):
    status = 409


class DataIntegrityError(AppError):
    """数据/密码学完整性问题：必须让用户知道严重性，而不是当成普通错误。"""

    status = 409
