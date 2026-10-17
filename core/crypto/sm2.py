"""SM2 椭圆曲线数字签名（GB/T 32918.2-2016，推荐曲线 sm2p256v1）。

用途（docs/技术方案V2.md §8.4）：村委经办人对报表快照签名，配合设备标识形成
双要素——单靠设备，村委可以说"不是我签的"；单靠人，农户可以说"设备被改过"。

签名口径：SM2-with-SM3，含 ZA（用户标识默认 "1234567812345678"）。
签名输出默认「r‖s」64 字节裸编码，同时提供 DER 编解码以便与银行侧工具互通。

性能：仿射坐标 + 双倍点算法，i5 上单次签名约 10–20 ms、验签约 20–40 ms；
本系统每期报表只签一次、银行侧按需验签，完全够用。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .sm3 import sm3

__all__ = [
    "SM2Error",
    "CURVE",
    "PublicKey",
    "PrivateKey",
    "sign",
    "verify",
    "encode_signature",
    "decode_signature",
    "encode_der",
    "decode_der",
    "DEFAULT_USER_ID",
]

# 推荐曲线参数（GB/T 32918.5-2016 附录）
P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
A = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFC
B = 0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93
N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
GX = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
GY = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0

CURVE = {"p": P, "a": A, "b": B, "n": N, "gx": GX, "gy": GY}
DEFAULT_USER_ID = b"1234567812345678"

Point = tuple[int, int] | None
G: Point = (GX, GY)


class SM2Error(ValueError):
    pass


def _inv(x: int, modulus: int = P) -> int:
    """模逆元。**注意**：坐标运算模 p，标量（签名公式）运算模 n，两者不可混用。"""
    x %= modulus
    if x == 0:
        raise SM2Error("模逆元不存在（除数为 0）")
    return pow(x, modulus - 2, modulus)


def point_add(p: Point, q: Point) -> Point:
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0]:
        if (p[1] + q[1]) % P == 0:
            return None
        return point_double(p)
    lam = ((q[1] - p[1]) * _inv(q[0] - p[0])) % P
    x = (lam * lam - p[0] - q[0]) % P
    return (x, (lam * (p[0] - x) - p[1]) % P)


def point_double(p: Point) -> Point:
    if p is None or p[1] % P == 0:
        return None
    lam = ((3 * p[0] * p[0] + A) * _inv(2 * p[1])) % P
    x = (lam * lam - 2 * p[0]) % P
    return (x, (lam * (p[0] - x) - p[1]) % P)


def point_mul(k: int, p: Point) -> Point:
    """k·P。k 先对 n 取模；负系数按 -k·P = (n-k)·P 处理。"""
    k %= N
    if k == 0 or p is None:
        return None
    result: Point = None
    addend: Point = p
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_double(addend)
        k >>= 1
    return result


def is_on_curve(p: Point) -> bool:
    if p is None:
        return True
    x, y = p
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


@dataclass(frozen=True)
class PublicKey:
    """公钥点。对外序列化用 04‖X‖Y（65 字节，与非压缩点格式一致）。"""

    x: int
    y: int

    def __post_init__(self) -> None:
        if not is_on_curve((self.x, self.y)):
            raise SM2Error("公钥不在 sm2p256v1 曲线上")

    @property
    def point(self) -> Point:
        return (self.x, self.y)

    def to_bytes(self) -> bytes:
        return b"\x04" + self.x.to_bytes(32, "big") + self.y.to_bytes(32, "big")

    def to_hex(self) -> str:
        return self.to_bytes().hex()

    @classmethod
    def from_bytes(cls, data: bytes) -> "PublicKey":
        if len(data) == 65 and data[0] == 0x04:
            return cls(int.from_bytes(data[1:33], "big"), int.from_bytes(data[33:], "big"))
        if len(data) == 64:
            return cls(int.from_bytes(data[:32], "big"), int.from_bytes(data[32:], "big"))
        raise SM2Error("公钥字节长度必须是 65（含 0x04 前缀）或 64")

    @classmethod
    def from_hex(cls, text: str) -> "PublicKey":
        return cls.from_bytes(bytes.fromhex(text))


@dataclass(frozen=True)
class PrivateKey:
    d: int

    def __post_init__(self) -> None:
        if not (1 <= self.d <= N - 2):
            raise SM2Error("私钥必须落在 [1, n-2]")

    @classmethod
    def generate(cls) -> "PrivateKey":
        return cls(secrets.randbelow(N - 2) + 1)

    @classmethod
    def from_bytes(cls, data: bytes) -> "PrivateKey":
        if len(data) != 32:
            raise SM2Error("私钥必须是 32 字节")
        return cls(int.from_bytes(data, "big"))

    def to_bytes(self) -> bytes:
        return self.d.to_bytes(32, "big")

    def to_hex(self) -> str:
        return self.to_bytes().hex()

    @property
    def public_key(self) -> PublicKey:
        pt = point_mul(self.d, G)
        assert pt is not None
        return PublicKey(*pt)


def _za(public: PublicKey, user_id: bytes) -> bytes:
    if len(user_id) * 8 > 0xFFFF:
        raise SM2Error("用户标识过长")
    entl = (len(user_id) * 8).to_bytes(2, "big")
    return sm3(
        entl
        + user_id
        + A.to_bytes(32, "big")
        + B.to_bytes(32, "big")
        + GX.to_bytes(32, "big")
        + GY.to_bytes(32, "big")
        + public.x.to_bytes(32, "big")
        + public.y.to_bytes(32, "big")
    )


def _e(message: bytes, public: PublicKey, user_id: bytes) -> int:
    return int.from_bytes(sm3(_za(public, user_id) + message), "big")


def sign(
    private: PrivateKey,
    message: bytes,
    user_id: bytes = DEFAULT_USER_ID,
    k: int | None = None,
) -> tuple[int, int]:
    """SM2-with-SM3 签名。k 仅供测试注入，生产一律用 CSPRNG。"""
    public = private.public_key
    e = _e(message, public, user_id)
    d = private.d
    d1_inv = _inv((1 + d) % N, N)
    for _ in range(64):
        kk = k if k is not None else secrets.randbelow(N - 1) + 1
        if not (1 <= kk <= N - 1):
            raise SM2Error("随机数 k 必须落在 [1, n-1]")
        pt = point_mul(kk, G)
        assert pt is not None
        r = (e + pt[0]) % N
        if r == 0 or (r + kk) % N == 0:
            if k is not None:
                raise SM2Error("注入的 k 使 r 退化，换一个 k")
            continue
        s = (d1_inv * (kk - r * d)) % N
        if s == 0:
            if k is not None:
                raise SM2Error("注入的 k 使 s 为 0，换一个 k")
            continue
        return r, s
    raise SM2Error("连续 64 次未能生成合法签名，随机源可能异常")


def verify(
    public: PublicKey,
    message: bytes,
    signature: tuple[int, int],
    user_id: bytes = DEFAULT_USER_ID,
) -> bool:
    """验签。任何非法输入都返回 False，绝不抛异常给调用方。"""
    try:
        r, s = signature
        if not (1 <= r <= N - 1 and 1 <= s <= N - 1):
            return False
        e = _e(message, public, user_id)
        t = (r + s) % N
        if t == 0:
            return False
        pt = point_add(point_mul(s, G), point_mul(t, public.point))
        if pt is None:
            return False
        return (e + pt[0]) % N == r
    except Exception:  # noqa: BLE001 - 验签是安全边界，一律判否
        return False


# ---- 编码 ----

def encode_signature(signature: tuple[int, int]) -> bytes:
    """裸编码 r‖s，各 32 字节，共 64 字节。"""
    r, s = signature
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def decode_signature(data: bytes) -> tuple[int, int]:
    if len(data) != 64:
        raise SM2Error("裸签名必须是 64 字节")
    return int.from_bytes(data[:32], "big"), int.from_bytes(data[32:], "big")


def _der_int(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return b"\x02" + bytes([len(raw)]) + raw


def encode_der(signature: tuple[int, int]) -> bytes:
    """DER：SEQUENCE { INTEGER r, INTEGER s }，便于与 x509/银行工具互通。"""
    body = _der_int(signature[0]) + _der_int(signature[1])
    return b"\x30" + bytes([len(body)]) + body


def decode_der(data: bytes) -> tuple[int, int]:
    if len(data) < 8 or data[0] != 0x30:
        raise SM2Error("不是合法的 DER SEQUENCE")
    length = data[1]
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[2 : 2 + n], "big")
        idx = 2 + n
    else:
        idx = 2
    values = []
    end = idx + length
    while idx < end:
        if data[idx] != 0x02:
            raise SM2Error("DER 中缺少 INTEGER")
        ln = data[idx + 1]
        values.append(int.from_bytes(data[idx + 2 : idx + 2 + ln], "big"))
        idx += 2 + ln
    if len(values) != 2:
        raise SM2Error("DER 中应有两个 INTEGER")
    return values[0], values[1]
