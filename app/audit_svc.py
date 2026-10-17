"""持久化审计哈希链服务。

与 `core.audit.AuditChain` 的关系：内核给出"怎么算"，这里负责"怎么存、怎么在
事务里追加"。两边共用 `genesis_hash` / `payload_digest` / `entry_hash` 三个函数，
不各写一套——否则校验工具与写入端算出的哈希会悄悄不一致。
"""
from __future__ import annotations

import json

from core.audit import AuditChain, entry_hash, genesis_hash, payload_digest

from . import repo
from .clock import now_iso
from .db import transaction

__all__ = ["AuditService"]


class AuditService:
    def __init__(self, conn, village_id: str = "") -> None:
        self.conn = conn
        self.village_id = village_id or self._village_id()

    def _village_id(self) -> str:
        v = repo.get_village(self.conn)
        return str(v.get("id") or "uninitialized")

    # ---- 写入 ----

    def append(
        self, actor: str, action: str, object_ref: str, payload: dict | None = None, ts: str = ""
    ) -> dict:
        """追加一条审计记录。已在外层事务中时直接复用，保证与业务写入同生共死。"""
        if self.conn.in_transaction:
            return self._append(actor, action, object_ref, payload, ts or now_iso())
        with transaction(self.conn):
            return self._append(actor, action, object_ref, payload, ts or now_iso())

    def _append(self, actor: str, action: str, object_ref: str, payload: dict | None, ts: str) -> dict:
        last = self.conn.execute(
            "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        seq = int(last["seq"]) + 1 if last else 1
        prev = str(last["hash"]) if last else genesis_hash(self.village_id)
        digest = payload_digest(payload)
        entry = {
            "seq": seq,
            "ts": ts,
            "actor": actor,
            "action": action,
            "object_ref": object_ref,
            "payload": payload or {},
            "payload_digest": digest,
            "prev_hash": prev,
            "hash": entry_hash(prev, seq, ts, actor, action, object_ref, digest),
        }
        repo.append_audit_row(self.conn, entry)
        return entry

    # ---- 校验与查询 ----

    def chain(self) -> AuditChain:
        rows = repo.audit_rows(self.conn)
        return AuditChain(
            self.village_id,
            [
                {
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "object_ref": r["object_ref"],
                    "payload_digest": r["payload_digest"],
                    "prev_hash": r["prev_hash"],
                    "hash": r["hash"],
                }
                for r in rows
            ],
        )

    def verify(self) -> dict:
        return self.chain().verify()

    def entries(self, limit: int = 200, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            out.append(item)
        return out

    def head(self) -> str | None:
        return self.chain().head()

    def export(self) -> dict:
        return {
            "village_id": self.village_id,
            "entries": [
                {
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "object_ref": r["object_ref"],
                    "payload_digest": r["payload_digest"],
                    "prev_hash": r["prev_hash"],
                    "hash": r["hash"],
                }
                for r in repo.audit_rows(self.conn)
            ],
            "verify": self.verify(),
        }
