"""SM4 分组密码（GB/T 32907-2016）与 SM4-GCM 认证加密。

用途（docs/技术方案V2.md §8.4）：本地凭证文件、密钥包裹、数据库敏感字段加密。
选 GCM 而不是 CBC 的原因：AEAD 同时给出机密性与完整性，解密时能发现密文被改，
不需要再单独维护一个 MAC，也不会犯"先加密再校验顺序搞反"的经典错误。

实现说明：GCM 是通用构造，这里以 SM4 作为 128 位分组函数；`_Gcm` 的 H、J0、
计数器、GHASH 与标签计算均按 NIST SP 800-38D。纯标准库，无第三方依赖。
"""
from __future__ import annotations

import hmac
import secrets

__all__ = [
    "SM4",
    "gcm_encrypt",
    "gcm_decrypt",
    "seal",
    "open_sealed",
    "GCMError",
    "TAG_BYTES",
]

TAG_BYTES = 16

_SBOX = bytes(
    (
        0xD6, 0x90, 0xE9, 0xFE, 0xCC, 0xE1, 0x3D, 0xB7, 0x16, 0xB6, 0x14, 0xC2, 0x28, 0xFB, 0x2C, 0x05,
        0x2B, 0x67, 0x9A, 0x76, 0x2A, 0xBE, 0x04, 0xC3, 0xAA, 0x44, 0x13, 0x26, 0x49, 0x86, 0x06, 0x99,
        0x9C, 0x42, 0x50, 0xF4, 0x91, 0xEF, 0x98, 0x7A, 0x33, 0x54, 0x0B, 0x43, 0xED, 0xCF, 0xAC, 0x62,
        0xE4, 0xB3, 0x1C, 0xA9, 0xC9, 0x08, 0xE8, 0x95, 0x80, 0xDF, 0x94, 0xFA, 0x75, 0x8F, 0x3F, 0xA6,
        0x47, 0x07, 0xA7, 0xFC, 0xF3, 0x73, 0x17, 0xBA, 0x83, 0x59, 0x3C, 0x19, 0xE6, 0x85, 0x4F, 0xA8,
        0x68, 0x6B, 0x81, 0xB2, 0x71, 0x64, 0xDA, 0x8B, 0xF8, 0xEB, 0x0F, 0x4B, 0x70, 0x56, 0x9D, 0x35,
        0x1E, 0x24, 0x0E, 0x5E, 0x63, 0x58, 0xD1, 0xA2, 0x25, 0x22, 0x7C, 0x3B, 0x01, 0x21, 0x78, 0x87,
        0xD4, 0x00, 0x46, 0x57, 0x9F, 0xD3, 0x27, 0x52, 0x4C, 0x36, 0x02, 0xE7, 0xA0, 0xC4, 0xC8, 0x9E,
        0xEA, 0xBF, 0x8A, 0xD2, 0x40, 0xC7, 0x38, 0xB5, 0xA3, 0xF7, 0xF2, 0xCE, 0xF9, 0x61, 0x15, 0xA1,
        0xE0, 0xAE, 0x5D, 0xA4, 0x9B, 0x34, 0x1A, 0x55, 0xAD, 0x93, 0x32, 0x30, 0xF5, 0x8C, 0xB1, 0xE3,
        0x1D, 0xF6, 0xE2, 0x2E, 0x82, 0x66, 0xCA, 0x60, 0xC0, 0x29, 0x23, 0xAB, 0x0D, 0x53, 0x4E, 0x6F,
        0xD5, 0xDB, 0x37, 0x45, 0xDE, 0xFD, 0x8E, 0x2F, 0x03, 0xFF, 0x6A, 0x72, 0x6D, 0x6C, 0x5B, 0x51,
        0x8D, 0x1B, 0xAF, 0x92, 0xBB, 0xDD, 0xBC, 0x7F, 0x11, 0xD9, 0x5C, 0x41, 0x1F, 0x10, 0x5A, 0xD8,
        0x0A, 0xC1, 0x31, 0x88, 0xA5, 0xCD, 0x7B, 0xBD, 0x2D, 0x74, 0xD0, 0x12, 0xB8, 0xE5, 0xB4, 0xB0,
        0x89, 0x69, 0x97, 0x4A, 0x0C, 0x96, 0x77, 0x7E, 0x65, 0xB9, 0xF1, 0x09, 0xC5, 0x6E, 0xC6, 0x84,
        0x18, 0xF0, 0x7D, 0xEC, 0x3A, 0xDC, 0x4D, 0x20, 0x79, 0xEE, 0x5F, 0x3E, 0xD7, 0xCB, 0x39, 0x48,
    )
)

_FK = (0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC)
_CK = (
    0x00070E15, 0x1C232A31, 0x383F464D, 0x545B6269,
    0x70777E85, 0x8C939AA1, 0xA8AFB6BD, 0xC4CBD2D9,
    0xE0E7EEF5, 0xFC030A11, 0x181F262D, 0x343B4249,
    0x50575E65, 0x6C737A81, 0x888F969D, 0xA4ABB2B9,
    0xC0C7CED5, 0xDCE3EAF1, 0xF8FF060D, 0x141B2229,
    0x30373E45, 0x4C535A61, 0x686F767D, 0x848B9299,
    0xA0A7AEB5, 0xBCC3CAD1, 0xD8DFE6ED, 0xF4FB0209,
    0x10171E25, 0x2C333A41, 0x484F565D, 0x646B7279,
)

_MASK = 0xFFFFFFFF
_R = 0xE1000000000000000000000000000000
_GF_MASK = (1 << 128) - 1


class GCMError(ValueError):
    """认证标签校验失败：密文被改动、密钥或 AAD 不匹配。"""


def _rotl(x: int, n: int) -> int:
    n &= 31
    return ((x << n) | (x >> (32 - n))) & _MASK if n else x & _MASK


def _tau(a: int) -> int:
    return (
        (_SBOX[(a >> 24) & 0xFF] << 24)
        | (_SBOX[(a >> 16) & 0xFF] << 16)
        | (_SBOX[(a >> 8) & 0xFF] << 8)
        | _SBOX[a & 0xFF]
    )


def _t(a: int) -> int:
    """轮函数 T = L(τ(·))：线性变换 2/10/18/24 位循环移位。"""
    b = _tau(a)
    return b ^ _rotl(b, 2) ^ _rotl(b, 10) ^ _rotl(b, 18) ^ _rotl(b, 24)


def _t_key(a: int) -> int:
    """密钥扩展 T'：13/23 位循环移位。"""
    b = _tau(a)
    return b ^ _rotl(b, 13) ^ _rotl(b, 23)


class SM4:
    """SM4 分组密码（128 位分组、128 位密钥）。"""

    __slots__ = ("_rk",)
    block_size = 16
    key_size = 16

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray)) or len(key) != 16:
            raise ValueError("SM4 密钥必须是 16 字节")
        k = [int.from_bytes(key[i : i + 4], "big") for i in range(0, 16, 4)]
        k = [k[i] ^ _FK[i] for i in range(4)]
        rk = []
        for i in range(32):
            nxt = k[i] ^ _t_key(k[i + 1] ^ k[i + 2] ^ k[i + 3] ^ _CK[i])
            k.append(nxt)
            rk.append(nxt)
        self._rk = tuple(rk)

    def _crypt(self, block: bytes, rk: tuple[int, ...]) -> bytes:
        x = [int.from_bytes(block[i : i + 4], "big") for i in range(0, 16, 4)]
        for i in range(32):
            x.append(x[i] ^ _t(x[i + 1] ^ x[i + 2] ^ x[i + 3] ^ rk[i]))
        out = (x[35], x[34], x[33], x[32])  # 反序变换 R
        return b"".join(v.to_bytes(4, "big") for v in out)

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("SM4 分组必须 16 字节")
        return self._crypt(block, self._rk)

    def decrypt_block(self, block: bytes) -> bytes:
        if len(block) != 16:
            raise ValueError("SM4 分组必须 16 字节")
        return self._crypt(block, self._rk[::-1])

    def encrypt_ecb(self, data: bytes) -> bytes:
        """ECB 仅用于标准向量自测与短块处理，业务代码不应直接使用。"""
        if len(data) % 16:
            raise ValueError("ECB 输入必须按 16 字节对齐")
        return b"".join(self.encrypt_block(data[i : i + 16]) for i in range(0, len(data), 16))


def _gf_mul(x: int, y: int) -> int:
    """GF(2^128) 乘法（GCM 的 GHASH 核心），按 SP 800-38D 的位串约定。"""
    z = 0
    v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        if v & 1:
            v = (v >> 1) ^ _R
        else:
            v >>= 1
    return z & _GF_MASK


def _ghash(h: int, data: bytes) -> int:
    y = 0
    for i in range(0, len(data), 16):
        block = data[i : i + 16]
        if len(block) < 16:
            block = block + b"\x00" * (16 - len(block))
        y = _gf_mul(y ^ int.from_bytes(block, "big"), h)
    return y


def _gcm_j0(h: int, sm4: SM4, nonce: bytes) -> bytes:
    if len(nonce) == 12:
        return nonce + b"\x00\x00\x00\x01"
    s = (16 - (len(nonce) % 16)) % 16
    padded = nonce + b"\x00" * s + b"\x00" * 8 + (len(nonce) * 8).to_bytes(8, "big")
    return _ghash(h, padded).to_bytes(16, "big")


def _gcm_ctr(sm4: SM4, j0: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    prefix = int.from_bytes(j0[:12], "big")
    out = bytearray()
    for i in range(0, len(data), 16):
        ctr = prefix << 32 | ((int.from_bytes(j0[12:], "big") + i // 16 + 1) & 0xFFFFFFFF)
        ks = sm4.encrypt_block(ctr.to_bytes(16, "big"))
        chunk = data[i : i + 16]
        out += bytes(a ^ b for a, b in zip(chunk, ks))
    return bytes(out)


def _gcm_tag(sm4: SM4, h: int, j0: bytes, aad: bytes, ct: bytes) -> bytes:
    aad_pad = aad + b"\x00" * ((16 - len(aad) % 16) % 16)
    ct_pad = ct + b"\x00" * ((16 - len(ct) % 16) % 16)
    lengths = (len(aad) * 8).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big")
    s = _ghash(h, aad_pad + ct_pad + lengths)
    mask = int.from_bytes(sm4.encrypt_block(j0), "big")
    return (mask ^ s).to_bytes(16, "big")[:TAG_BYTES]


def gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    """SM4-GCM 加密，返回 (密文, 16 字节标签)。"""
    sm4 = SM4(key)
    h = int.from_bytes(sm4.encrypt_block(b"\x00" * 16), "big")
    j0 = _gcm_j0(h, sm4, nonce)
    ct = _gcm_ctr(sm4, j0, plaintext)
    return ct, _gcm_tag(sm4, h, j0, aad, ct)


def gcm_decrypt(
    key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes = b""
) -> bytes:
    """SM4-GCM 解密；标签不符抛 GCMError（绝不返回部分明文）。"""
    if len(tag) != TAG_BYTES:
        raise GCMError("GCM 标签长度错误")
    sm4 = SM4(key)
    h = int.from_bytes(sm4.encrypt_block(b"\x00" * 16), "big")
    j0 = _gcm_j0(h, sm4, nonce)
    expected = _gcm_tag(sm4, h, j0, aad, ciphertext)
    if not hmac.compare_digest(expected, tag):
        raise GCMError("GCM 认证失败：密文/附加数据被篡改，或密钥不正确")
    return _gcm_ctr(sm4, j0, ciphertext)


def seal(key: bytes, plaintext: bytes, aad: bytes = b"", nonce: bytes | None = None) -> bytes:
    """把「随机数 + 密文 + 标签」封成一个自描述字节串，便于直接落盘。

    布局：12 字节 nonce ｜ 密文 ｜ 16 字节标签。nonce 由 CSPRNG 生成，
    同一密钥下重复使用 nonce 会毁掉 GCM 的安全性，因此这里不接受调用方省略随机性。
    """
    nonce = secrets.token_bytes(12) if nonce is None else nonce
    if len(nonce) != 12:
        raise ValueError("seal 只接受 12 字节 nonce（96 位 IV，GCM 推荐长度）")
    ct, tag = gcm_encrypt(key, nonce, plaintext, aad)
    return nonce + ct + tag


def open_sealed(key: bytes, packed: bytes, aad: bytes = b"") -> bytes:
    """解开 seal() 的字节串。"""
    if len(packed) < 12 + TAG_BYTES:
        raise GCMError("密文长度不足，文件可能被截断")
    nonce, ct, tag = packed[:12], packed[12:-TAG_BYTES], packed[-TAG_BYTES:]
    return gcm_decrypt(key, nonce, ct, tag, aad)
