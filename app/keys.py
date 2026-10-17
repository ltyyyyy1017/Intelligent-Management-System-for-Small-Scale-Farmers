"""设备密钥与两级密钥管理（KEK/DEK）。

对应 docs/技术方案V2.md §8.3、§8.4。分层的目的不是"多一层更安全"，而是：

- **DEK**（数据密钥）负责加密凭证文件与敏感字段，长期不变；
- **KEK**（主密钥）由设备密钥文件（可选再加管理员口令）派生，只用于包裹 DEK。

这样换管理员口令时只需重新包裹 DEK，不必重加密全部数据——村委换人不需要
把几百份凭证重新导一遍，也就不会因为"换密码太麻烦"而干脆不换。

诚实的边界：设备密钥文件就在本机。能物理接触这台电脑并拿到 data/ 目录的人，
可以解密数据。这一层防的是"拷走数据库文件"和"U 盘丢失"，不是防"坐在电脑前的人"。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from core.crypto.sm3 import sm3
from core.crypto.sm4 import open_sealed, seal

from .config import Config
from .errors import DataIntegrityError, UserError

__all__ = ["KeyStore", "hash_password", "verify_password", "derive_key"]

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """口令 → 32 字节密钥。用 scrypt（内存硬）而非裸 SHA，抬高离线爆破成本。"""
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )


def hash_password(password: str) -> tuple[str, str]:
    """返回 (hash_hex, salt_hex)。"""
    salt = secrets.token_bytes(16)
    return derive_key(password, salt).hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(derive_key(password, salt).hex(), hash_hex)


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    try:  # Windows 上 chmod 作用有限，但至少不会比默认更宽
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sm4_key(dek: bytes) -> bytes:
    return sm3(b"sm4|" + dek)[:16]


@dataclass(frozen=True)
class KeyStore:
    """设备密钥库。enc_key 用于包裹 DEK，token_secret 用于代币派生。"""

    enc_key: bytes
    token_secret: bytes
    device_id: str
    dek: bytes

    # ---- 构造 ----

    @staticmethod
    def _split(master: bytes) -> tuple[bytes, bytes]:
        return sm3(b"enc|" + master)[:16], sm3(b"tok|" + master)

    @classmethod
    def open(cls, cfg: Config, passphrase: str | None = None) -> "KeyStore":
        cfg.ensure_dirs()
        key_path = cfg.key_dir / "device.key"
        dek_path = cfg.key_dir / "dek.bin"

        if not key_path.exists():
            _write_private(
                key_path,
                json.dumps(
                    {
                        "format": "nfm-device-key-v1",
                        "master": secrets.token_hex(32),
                        "wrapped": False,
                        "created_at": "",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )

        try:
            raw = json.loads(key_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataIntegrityError("设备密钥文件损坏", detail="device_key_corrupt") from exc

        if raw.get("wrapped"):
            if not passphrase:
                raise UserError("设备密钥已设置口令保护，请先输入口令", detail="passphrase_required")
            salt = bytes.fromhex(raw["salt"])
            try:
                master = open_sealed(derive_key(passphrase, salt), bytes.fromhex(raw["sealed"]))
            except Exception as exc:  # noqa: BLE001
                raise UserError("口令不正确，无法解开设备密钥", detail="bad_passphrase") from exc
        else:
            master = bytes.fromhex(raw["master"])

        enc_key, token_secret = cls._split(master)
        device_id = "DEV-" + sm3(master)[:8].hex().upper()

        if not dek_path.exists():
            _write_private(dek_path, seal(enc_key, secrets.token_bytes(32), aad=b"nfm-dek-v1"))
        try:
            dek = open_sealed(enc_key, dek_path.read_bytes(), aad=b"nfm-dek-v1")
        except Exception as exc:  # noqa: BLE001
            raise DataIntegrityError("数据密钥损坏或与设备密钥不匹配", detail="dek_corrupt") from exc

        return cls(enc_key=enc_key, token_secret=token_secret, device_id=device_id, dek=dek)

    # ---- 数据加解密（凭证文件、敏感字段） ----

    def seal(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        return seal(_sm4_key(self.dek), plaintext, aad=aad)

    def unseal(self, packed: bytes, aad: bytes = b"") -> bytes:
        try:
            return open_sealed(_sm4_key(self.dek), packed, aad=aad)
        except Exception as exc:  # noqa: BLE001
            raise DataIntegrityError("本地加密数据校验失败（可能被改动）", detail="gcm_failed") from exc

    def seal_text(self, text: str, aad: bytes = b"") -> bytes:
        return self.seal(text.encode("utf-8"), aad=aad)

    def unseal_text(self, blob: bytes, aad: bytes = b"") -> str:
        return self.unseal(blob, aad=aad).decode("utf-8")

    # ---- 口令保护（可选） ----

    @staticmethod
    def protect(cfg: Config, passphrase: str) -> None:
        """把设备密钥文件改为口令包裹。已有数据不需要重加密。"""
        key_path = cfg.key_dir / "device.key"
        raw = json.loads(key_path.read_text(encoding="utf-8"))
        if raw.get("wrapped"):
            raise UserError("设备密钥已经是口令保护状态")
        master = bytes.fromhex(raw["master"])
        salt = secrets.token_bytes(16)
        wrapped = seal(derive_key(passphrase, salt), master, aad=b"nfm-master-v1")
        _write_private(
            key_path,
            json.dumps(
                {
                    "format": "nfm-device-key-v1",
                    "wrapped": True,
                    "salt": salt.hex(),
                    "sealed": wrapped.hex(),
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )

    @staticmethod
    def unprotect(cfg: Config, passphrase: str) -> None:
        key_path = cfg.key_dir / "device.key"
        raw = json.loads(key_path.read_text(encoding="utf-8"))
        if not raw.get("wrapped"):
            raise UserError("设备密钥未设置口令保护")
        salt = bytes.fromhex(raw["salt"])
        try:
            master = open_sealed(derive_key(passphrase, salt), bytes.fromhex(raw["sealed"]))
        except Exception as exc:  # noqa: BLE001
            raise UserError("口令不正确") from exc
        _write_private(
            key_path,
            json.dumps(
                {"format": "nfm-device-key-v1", "master": master.hex(), "wrapped": False},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
