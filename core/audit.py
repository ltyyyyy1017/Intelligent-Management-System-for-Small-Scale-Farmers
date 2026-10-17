"""审计日志哈希链：防篡改、防删除。

对应 docs/技术方案V2.md §8.4。

    hash_0 = SM3("GENESIS" || village_id)
    hash_n = SM3( hash_{n-1} || ts || actor || action || object_ref || payload_digest )

任何一条被改动或删除，其后所有 hash 都对不上。
"""
from __future__ import annotations

import hashlib
import hmac
import json

__all__ = ["AuditChain", "AuditError"]

GENESIS_LABEL = "GENESIS"


class AuditError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()  # 生产替换为 SM3


class AuditChain:
    """内存版哈希链。持久化时把 entries 原样写库，校验时重新计算。"""

    def __init__(self, village_id: str, entries: list[dict] | None = None) -> None:
        self.village_id = village_id
        self.entries: list[dict] = []
        if entries:
            for e in entries:
                self.entries.append(dict(e))

    # ---- 追加 ----
    def append(self, actor: str, action: str, object_ref: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        payload_digest = _digest(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
        prev = self.entries[-1]["hash"] if self.entries else _digest(
            f"{GENESIS_LABEL}||{self.village_id}".encode("utf-8")
        )
        seq = len(self.entries) + 1
        body = "|".join([prev, str(seq), actor, action, object_ref, payload_digest])
        entry = {
            "seq": seq,
            "actor": actor,
            "action": action,
            "object_ref": object_ref,
            "payload_digest": payload_digest,
            "prev_hash": prev,
            "hash": _digest(body.encode("utf-8")),
        }
        self.entries.append(entry)
        return entry

    # ---- 校验 ----
    def verify(self) -> dict:
        """全链校验，返回首个断点。O(n) 重算，村内账目规模下毫秒级。"""
        prev = _digest(f"{GENESIS_LABEL}||{self.village_id}".encode("utf-8"))
        for idx, entry in enumerate(self.entries):
            expected_seq = idx + 1
            if entry.get("seq") != expected_seq:
                return {"ok": False, "broken_at": idx, "reason": "seq 不连续（疑似删除）"}
            if not hmac.compare_digest(str(entry.get("prev_hash", "")), prev):
                return {"ok": False, "broken_at": idx, "reason": "prev_hash 不匹配"}
            body = "|".join(
                [
                    prev,
                    str(entry["seq"]),
                    entry["actor"],
                    entry["action"],
                    entry["object_ref"],
                    entry["payload_digest"],
                ]
            )
            if not hmac.compare_digest(_digest(body.encode("utf-8")), str(entry.get("hash", ""))):
                return {"ok": False, "broken_at": idx, "reason": "hash 不匹配（疑似篡改）"}
            prev = entry["hash"]
        return {"ok": True, "length": len(self.entries)}

    def head(self) -> str | None:
        return self.entries[-1]["hash"] if self.entries else None
