"""备份与恢复。

设计取舍：**备份必须包含密钥**，否则恢复出来的账本是一堆解不开的密文。
代价是"备份 U 盘 = 数据 + 钥匙"，所以这里提供可选的整包口令加密：
`create_backup(cfg, passphrase)` 生成 `.nfmbak`，用 scrypt + SM4-GCM 封装整个 zip。
不带口令时生成普通 zip，并在文件名里标明 `-unencrypted`，避免有人误以为它加密了。

WAL 模式下不能直接复制 .db 文件（可能缺最近事务），这里用 SQLite 的在线备份 API
把一致性快照写进临时文件，再打包。
"""
from __future__ import annotations

import io
import secrets
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from core.crypto.sm4 import GCMError, open_sealed, seal

from .clock import now_iso
from .config import Config
from .errors import DataIntegrityError, UserError
from .keys import derive_key

__all__ = ["create_backup", "restore_backup", "list_backups", "MAGIC"]

MAGIC = b"NFMBAK1"


def _stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def _consistent_db_copy(cfg: Config, dest: Path) -> None:
    src = sqlite3.connect(cfg.db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_backup(cfg: Config, passphrase: str = "") -> Path:
    """打包数据目录关键内容；返回备份文件路径。"""
    if not cfg.db_path.exists():
        raise UserError("还没有数据库，先完成初始化")
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="nfm-bak-") as tmp:
        tmpdir = Path(tmp)
        db_copy = tmpdir / "village.db"
        _consistent_db_copy(cfg, db_copy)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", _manifest(cfg))
            z.write(db_copy, "village.db")
            for sub in ("evidence", "keys", "exports"):
                root = cfg.data_dir / sub
                if not root.exists():
                    continue
                for path in root.rglob("*"):
                    if path.is_file():
                        z.write(path, f"{sub}/{path.relative_to(root).as_posix()}")
        payload = buf.getvalue()

    if passphrase:
        salt = secrets.token_bytes(16)
        key = derive_key(passphrase, salt)[:16]
        out = cfg.backup_dir / f"nfm-backup-{_stamp()}.nfmbak"
        out.write_bytes(MAGIC + salt + seal(key, payload))
    else:
        out = cfg.backup_dir / f"nfm-backup-{_stamp()}-unencrypted.zip"
        out.write_bytes(payload)
    return out


def _manifest(cfg: Config) -> str:
    import json

    return json.dumps(
        {
            "format": "nfm-backup-v1",
            "created_at": now_iso(),
            "data_dir": str(cfg.data_dir),
            "contains": ["village.db", "evidence/", "keys/", "exports/"],
            "warning": "本备份含设备密钥；U 盘务必备份并上锁保管。",
        },
        ensure_ascii=False,
        indent=2,
    )


def restore_backup(cfg: Config, path: Path | str, passphrase: str = "", overwrite: bool = False) -> Path:
    """把备份恢复到 data 目录。默认拒绝覆盖已有数据。"""
    path = Path(path)
    if not path.exists():
        raise UserError("备份文件不存在")
    raw = path.read_bytes()
    if raw.startswith(MAGIC):
        if not passphrase:
            raise UserError("这是加密备份，请输入口令")
        salt, sealed = raw[len(MAGIC) : len(MAGIC) + 16], raw[len(MAGIC) + 16 :]
        try:
            raw = open_sealed(derive_key(passphrase, salt)[:16], sealed)
        except GCMError as exc:
            raise UserError("口令不对，或备份文件已损坏") from exc

    if cfg.db_path.exists() and not overwrite:
        raise UserError("目标目录已有数据；确认覆盖请加 --force（会先备份原数据）")

    cfg.ensure_dirs()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for member in z.namelist():
            if member.startswith("/") or ".." in member:
                raise DataIntegrityError("备份包内含非法路径，已拒绝恢复")
        z.extractall(cfg.data_dir)
    return cfg.data_dir


def list_backups(cfg: Config) -> list[dict]:
    if not cfg.backup_dir.exists():
        return []
    out = []
    for path in sorted(cfg.backup_dir.glob("nfm-backup-*"), reverse=True):
        out.append(
            {
                "name": path.name,
                "size": path.stat().st_size,
                "encrypted": path.suffix == ".nfmbak",
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
        )
    return out
