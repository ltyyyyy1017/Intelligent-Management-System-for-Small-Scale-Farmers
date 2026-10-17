"""村委签章密钥（SM2）。

职责边界：私钥只存在于本机、只用于签快照；验签只需要公钥。
公钥可以公开——它出现在报表、二维码、外发材料包里，银行拿它验签即可，
不需要任何联网或第三方 CA（农村场景没有 CA 可用，也不需要）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.crypto import sm2

from .config import Config
from .errors import DataIntegrityError

__all__ = [
    "SIGN_KEY_AAD",
    "load_signing_key",
    "public_key_hex",
    "sign_digest_bytes",
    "verify_signature",
    "signature_short",
]

SIGN_KEY_AAD = b"nfm-sign-v1"


def load_signing_key(cfg: Config, keys) -> sm2.PrivateKey:
    """取本村签章私钥；不存在则生成。私钥以 DEK 加密存放。"""
    cfg.ensure_dirs()
    path = cfg.key_dir / "sign.key"
    if path.exists():
        try:
            raw = keys.unseal(path.read_bytes(), aad=SIGN_KEY_AAD)
            data = json.loads(raw.decode("utf-8"))
            return sm2.PrivateKey(int(data["d"], 16))
        except Exception as exc:  # noqa: BLE001
            raise DataIntegrityError("签章密钥损坏或无法解密", detail="sign_key_corrupt") from exc

    priv = sm2.PrivateKey.generate()
    payload = json.dumps(
        {"format": "nfm-sign-key-v1", "d": priv.to_hex()}, ensure_ascii=False
    ).encode("utf-8")
    sealed = keys.seal(payload, aad=SIGN_KEY_AAD)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(sealed)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return priv


def public_key_hex(priv: sm2.PrivateKey) -> str:
    return priv.public_key.to_hex()


def sign_digest_bytes(priv: sm2.PrivateKey, data: bytes) -> str:
    """对任意字节签名，返回 64 字节裸签名的十六进制。"""
    return sm2.encode_signature(sm2.sign(priv, data)).hex()


def verify_signature(public_hex: str, data: bytes, signature_hex: str) -> bool:
    try:
        pub = sm2.PublicKey.from_hex(public_hex)
        sig = sm2.decode_signature(bytes.fromhex(signature_hex))
    except Exception:  # noqa: BLE001 - 验签失败就是失败，不抛给界面
        return False
    return sm2.verify(pub, data, sig)


def signature_short(signature_hex: str, n: int = 16) -> str:
    return (signature_hex or "")[:n]
