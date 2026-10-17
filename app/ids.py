"""业务标识符生成。

对外一律用 token / 业务号，不用自增主键：自增主键会泄露"这个村有多少户、
今年记了多少笔"，而业务号可以按需重置。格式固定，便于人读与人工核对：

    F-ABCDEF2345   农户代币（10 位 Crockford Base32）
    E-000123       事件号（按村内顺序，6 位）
    EV-000123      凭证号
    S-2026-0001    快照号（期 + 序号）
    R-000123       报表文件号
"""
from __future__ import annotations

import re
import secrets

from core.crypto.sm3 import sm3

__all__ = [
    "BASE32",
    "token_for",
    "event_id",
    "evidence_id",
    "snapshot_id",
    "report_id",
    "new_session_token",
    "new_csrf_token",
    "is_token",
]

# Crockford Base32：去掉 I/L/O/U，避免人工抄写时 1/l、0/O 混淆
BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _b32(data: bytes, length: int) -> str:
    bits = int.from_bytes(data, "big")
    out = []
    for _ in range(length):
        out.append(BASE32[bits & 31])
        bits >>= 5
    return "".join(reversed(out))


def token_for(village_id: str, seq: int, device_secret: bytes) -> str:
    """农户代币：F- + 10 位 Base32。

    输入含 device_secret，使得「同一序号在不同村/不同设备」得到不同代币，
    避免跨村撞号后被反推；序号仍参与，便于村内人工对账。
    """
    digest = sm3(f"{village_id}|{seq}".encode("utf-8") + device_secret)
    return "F-" + _b32(digest[:7], 10)


_TOKEN_RE = re.compile(r"^F-[0-9A-HJKMNP-TV-Z]{10}$")


def is_token(value: str) -> bool:
    return bool(_TOKEN_RE.match(value or ""))


def event_id(seq: int) -> str:
    if seq <= 0:
        raise ValueError("事件序号必须为正")
    return f"E-{seq:06d}"


def evidence_id(seq: int) -> str:
    return f"EV-{seq:06d}"


def snapshot_id(period: str, seq: int) -> str:
    return f"S-{period}-{seq:04d}"


def report_id(seq: int) -> str:
    return f"R-{seq:06d}"


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
