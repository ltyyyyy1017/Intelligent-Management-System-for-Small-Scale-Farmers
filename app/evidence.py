"""原始凭证库：文件落盘即加密，库里只留哈希与相对路径。

为什么"文件本身加密、库只留哈希"：数据库备份与凭证目录经常被分别拷走（U 盘、
网盘、维修送修），两份都不完整时谁也解不开；而哈希留在库里，即使文件被换掉，
也能在核验时发现。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from core.crypto.sm3 import sm3_hex

from . import repo
from .clock import now_iso
from .config import Config
from .db import next_counter, transaction
from .errors import NotFound, UserError
from .ids import evidence_id

__all__ = ["EvidenceService", "MAX_EVIDENCE_BYTES"]

MAX_EVIDENCE_BYTES = 20 * 1024 * 1024  # 20 MB：手机照片与扫描件足够

_KIND_BY_SUFFIX = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image", ".bmp": "image",
    ".pdf": "pdf", ".wav": "audio", ".mp3": "audio", ".m4a": "audio",
    ".xlsx": "xlsx", ".csv": "csv",
}


@dataclass
class EvidenceService:
    conn: object
    cfg: Config
    keys: object
    actor: str

    @staticmethod
    def kind_of(filename: str) -> str:
        return _KIND_BY_SUFFIX.get(Path(filename or "").suffix.lower(), "other")

    def store(self, data: bytes, kind: str, original_name: str = "") -> str:
        if not data:
            raise UserError("凭证文件为空")
        if len(data) > MAX_EVIDENCE_BYTES:
            raise UserError(f"凭证文件超过 {MAX_EVIDENCE_BYTES // 1024 // 1024} MB，请先压缩")
        if kind not in ("image", "pdf", "audio", "xlsx", "csv", "other"):
            kind = "other"

        with transaction(self.conn):
            seq = next_counter(self.conn, "evidence")
            eid = evidence_id(seq)
            rel = f"{eid}.bin"
            sealed = self.keys.seal(data, aad=eid.encode("utf-8"))
            path = self.cfg.evidence_dir / rel
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(sealed)
            os.replace(tmp, path)
            self.conn.execute(
                "INSERT INTO evidence(id, content_hash, hash_alg, rel_path, kind, size, "
                "original_name, captured_at, created_by) VALUES (?, ?, 'SM3', ?, ?, ?, ?, ?, ?)",
                (eid, sm3_hex(data), rel, kind, len(data), original_name or None,
                 now_iso(), self.actor),
            )
        return eid

    def get(self, eid: str) -> dict:
        row = self.conn.execute("SELECT * FROM evidence WHERE id = ?", (eid,)).fetchone()
        if not row:
            raise NotFound(f"找不到凭证 {eid}")
        return dict(row)

    def read(self, eid: str) -> bytes:
        row = self.get(eid)
        path = self.cfg.evidence_dir / row["rel_path"]
        if not path.exists():
            raise NotFound(f"凭证文件缺失：{eid}")
        data = self.keys.unseal(path.read_bytes(), aad=eid.encode("utf-8"))
        if sm3_hex(data) != row["content_hash"]:
            from .errors import DataIntegrityError

            raise DataIntegrityError(f"凭证 {eid} 内容与登记哈希不一致", detail="evidence_tampered")
        return data

    def verify(self, eid: str) -> dict:
        try:
            self.read(eid)
            return {"ok": True, "id": eid}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "id": eid, "error": str(exc)}

    def list(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, content_hash, kind, size, original_name, captured_at FROM evidence "
            "ORDER BY captured_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]
