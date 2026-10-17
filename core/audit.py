"""审计日志哈希链：防篡改、防删除。

对应 docs/技术方案V2.md §8.4：

    hash_0 = SM3("GENESIS" || village_id)
    hash_n = SM3( hash_{n-1} || ts || actor || action || object_ref || payload_digest )

任何一条被改动或删除，其后所有 hash 都对不上。校验只需 O(n) 重算。

两个刻意的设计：
1. **时间戳由调用方显式传入**，本模块不读系统时钟——否则同一份日志在两次
   校验中会得到不同结果，"可复算"就不成立了。
2. **seq 参与哈希**，且校验时要求连续：删除一条会让后续条目全部对不上，
   而不是"少一条但看起来仍然自洽"。
"""
from __future__ import annotations

import hmac
import json

from .crypto.sm3 import sm3_hex

__all__ = [
    "AuditChain",
    "AuditError",
    "GENESIS_LABEL",
    "genesis_hash",
    "payload_digest",
    "entry_hash",
]

GENESIS_LABEL = "GENESIS"


class AuditError(ValueError):
    pass


def _digest(data: bytes) -> str:
    return sm3_hex(data)


def genesis_hash(village_id: str) -> str:
    """链首：SM3("GENESIS" || village_id)。换村即换链，不可跨村拼接。"""
    return _digest(f"{GENESIS_LABEL}||{village_id}".encode("utf-8"))


def payload_digest(payload: dict | None) -> str:
    return _digest(json.dumps(payload or {}, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def entry_hash(
    prev: str, seq: int, ts: str, actor: str, action: str, object_ref: str, digest: str
) -> str:
    """单条审计记录的哈希。持久化层与内存链共用这一个函数，避免两处实现漂移。"""
    body = "|".join([prev, str(seq), ts, actor, action, object_ref, digest]).encode("utf-8")
    return _digest(body)


class AuditChain:
    """内存版哈希链。持久化时把 entries 原样写库，校验时重新计算。"""

    def __init__(self, village_id: str, entries: list[dict] | None = None) -> None:
        self.village_id = village_id
        self.entries: list[dict] = []
        if entries:
            for e in entries:
                self.entries.append(dict(e))

    def _genesis(self) -> str:
        return genesis_hash(self.village_id)

    @staticmethod
    def _body(prev: str, seq: int, ts: str, actor: str, action: str, ref: str, digest: str) -> bytes:
        return "|".join([prev, str(seq), ts, actor, action, ref, digest]).encode("utf-8")

    def append(
        self,
        actor: str,
        action: str,
        object_ref: str,
        payload: dict | None = None,
        ts: str = "",
    ) -> dict:
        digest = payload_digest(payload)
        prev = self.entries[-1]["hash"] if self.entries else self._genesis()
        seq = len(self.entries) + 1
        entry = {
            "seq": seq,
            "ts": ts,
            "actor": actor,
            "action": action,
            "object_ref": object_ref,
            "payload_digest": digest,
            "prev_hash": prev,
            "hash": entry_hash(prev, seq, ts, actor, action, object_ref, digest),
        }
        self.entries.append(entry)
        return entry

    def verify(self) -> dict:
        """全链校验，返回首个断点。O(n) 重算，村内账目规模下毫秒级。"""
        prev = self._genesis()
        for idx, entry in enumerate(self.entries):
            expected_seq = idx + 1
            if entry.get("seq") != expected_seq:
                return {"ok": False, "broken_at": idx, "reason": "seq 不连续（疑似删除）"}
            if not hmac.compare_digest(str(entry.get("prev_hash", "")), prev):
                return {"ok": False, "broken_at": idx, "reason": "prev_hash 不匹配"}
            expected = entry_hash(
                prev,
                entry["seq"],
                str(entry.get("ts", "")),
                entry["actor"],
                entry["action"],
                entry["object_ref"],
                entry["payload_digest"],
            )
            if not hmac.compare_digest(expected, str(entry.get("hash", ""))):
                return {"ok": False, "broken_at": idx, "reason": "hash 不匹配（疑似篡改）"}
            prev = entry["hash"]
        return {"ok": True, "length": len(self.entries)}

    def head(self) -> str | None:
        return self.entries[-1]["hash"] if self.entries else None
