"""国密算法测试：SM3 / SM4-GCM / SM2 的标准向量与边界行为。

向量来源：
- SM3：GB/T 32905-2016 附录 A 示例（"abc"、"abcd"×16）；
- SM4：GB/T 32907-2016 附录 A 示例；
- SM2：GB/T 32918.5-2016 附录 A 示例（私钥/随机数/消息 → r‖s 的确定值）。

这些向量是"实现正确"的唯一客观标准：自己和自己对拍只能证明一致性，证明不了正确性。
"""
from __future__ import annotations

import unittest

from core.crypto import sm2, sm4
from core.crypto.sm3 import SM3, sm3_hex


class TestSM3(unittest.TestCase):
    def test_standard_vector_abc(self) -> None:
        self.assertEqual(
            sm3_hex(b"abc"),
            "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0",
        )

    def test_standard_vector_abcd16(self) -> None:
        self.assertEqual(
            sm3_hex(b"abcd" * 16),
            "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732",
        )

    def test_empty(self) -> None:
        self.assertEqual(
            sm3_hex(b""),
            "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b",
        )

    def test_incremental_matches_oneshot(self) -> None:
        data = "中小农户智能财务管理系统".encode("utf-8") * 37
        h = SM3()
        for i in range(0, len(data), 7):
            h.update(data[i : i + 7])
        self.assertEqual(h.hexdigest(), sm3_hex(data))

    def test_multi_block_length_field(self) -> None:
        # 64 字节以上才会写满「消息总比特数」字段；自实现摘要最常在这里错
        self.assertEqual(sm3_hex(b"a" * 64), sm3_hex(b"a" * 63 + b"a"))
        self.assertNotEqual(sm3_hex(b"a" * 64), sm3_hex(b"a" * 65))

    def test_copy_isolated(self) -> None:
        h = SM3(b"abc")
        clone = h.copy()
        clone.update(b"def")
        self.assertNotEqual(h.hexdigest(), clone.hexdigest())
        self.assertEqual(h.hexdigest(), sm3_hex(b"abc"))

    def test_rejects_str(self) -> None:
        with self.assertRaises(TypeError):
            SM3().update("字符串必须先编码")  # type: ignore[arg-type]


class TestSM4(unittest.TestCase):
    KEY = bytes.fromhex("0123456789abcdeffedcba9876543210")
    BLOCK = bytes.fromhex("0123456789abcdeffedcba9876543210")

    def test_standard_vector(self) -> None:
        self.assertEqual(
            sm4.SM4(self.KEY).encrypt_block(self.BLOCK).hex(),
            "681edf34d206965e86b3e94f536e4246",
        )

    def test_decrypt_roundtrip(self) -> None:
        cipher = sm4.SM4(self.KEY)
        self.assertEqual(cipher.decrypt_block(cipher.encrypt_block(self.BLOCK)), self.BLOCK)

    def test_key_length(self) -> None:
        with self.assertRaises(ValueError):
            sm4.SM4(b"\x00" * 15)
        with self.assertRaises(ValueError):
            sm4.SM4(b"\x00" * 32)

    def test_gcm_roundtrip_with_aad(self) -> None:
        key = bytes(range(16))
        plain = "2026年4月，买化肥 500 公斤，2,000.00 元".encode("utf-8")
        ct, tag = sm4.gcm_encrypt(key, b"\x01" * 12, plain, aad=b"EV-1024")
        self.assertNotEqual(ct, plain)
        self.assertEqual(sm4.gcm_decrypt(key, b"\x01" * 12, ct, tag, aad=b"EV-1024"), plain)

    def test_gcm_detects_tamper(self) -> None:
        key, nonce = bytes(16), bytes(12)
        ct, tag = sm4.gcm_encrypt(key, nonce, b"hello world", aad=b"x")
        with self.assertRaises(sm4.GCMError):
            sm4.gcm_decrypt(key, nonce, ct[:-1] + bytes([ct[-1] ^ 1]), tag, aad=b"x")
        with self.assertRaises(sm4.GCMError):
            sm4.gcm_decrypt(key, nonce, ct, tag, aad=b"y")
        with self.assertRaises(sm4.GCMError):
            sm4.gcm_decrypt(bytes([1]) * 16, nonce, ct, tag, aad=b"x")

    def test_seal_layout_and_roundtrip(self) -> None:
        key = bytes(range(16))
        packed = sm4.seal(key, b"evidence-bytes", aad=b"EV-1")
        self.assertEqual(len(packed), 12 + len(b"evidence-bytes") + sm4.TAG_BYTES)
        self.assertEqual(sm4.open_sealed(key, packed, aad=b"EV-1"), b"evidence-bytes")

    def test_seal_rejects_bad_nonce(self) -> None:
        with self.assertRaises(ValueError):
            sm4.seal(bytes(16), b"x", nonce=b"short")

    def test_empty_plaintext(self) -> None:
        key, nonce = bytes(16), bytes(12)
        ct, tag = sm4.gcm_encrypt(key, nonce, b"")
        self.assertEqual(ct, b"")
        self.assertEqual(sm4.gcm_decrypt(key, nonce, ct, tag), b"")


class TestSM2(unittest.TestCase):
    """GB/T 32918.5 附录 A 的确定性示例。"""

    D = 0x3945208F7B2144B13F36E38AC6D39F95889393692860B51A42FB81EF4DF7C5B8
    K = 0x59276E27D506861A16680F3AD9C02DCCEF3CC1FA3CDBE4CE6D54B80DEAC1BC21
    MSG = b"message digest"
    PUB_X = 0x09F9DF311E5421A150DD7D161E4BC5C672179FAD1833FC076BB08FF356F35020
    PUB_Y = 0xCCEA490CE26775A52DC6EA718CC1AA600AED05FBF35E084A6632F6072DA9AD13
    R = 0xF5A03B0648D2C4630EEAC513E1BB81A15944DA3827D5B74143AC7EACEEE720B3
    S = 0xB1B6AA29DF212FD8763182BC0D421CA1BB9038FD1F7F42D4840B69C485BBC1AA

    def setUp(self) -> None:
        self.priv = sm2.PrivateKey(self.D)
        self.pub = self.priv.public_key

    def test_public_key_derivation(self) -> None:
        self.assertEqual((self.pub.x, self.pub.y), (self.PUB_X, self.PUB_Y))

    def test_standard_signature_vector(self) -> None:
        self.assertEqual(sm2.sign(self.priv, self.MSG, k=self.K), (self.R, self.S))

    def test_verify_standard_signature(self) -> None:
        self.assertTrue(sm2.verify(self.pub, self.MSG, (self.R, self.S)))

    def test_random_sign_verify(self) -> None:
        r, s = sm2.sign(self.priv, "农户账本快照".encode("utf-8"))
        self.assertTrue(sm2.verify(self.pub, "农户账本快照".encode("utf-8"), (r, s)))

    def test_negative_cases(self) -> None:
        r, s = sm2.sign(self.priv, self.MSG)
        self.assertFalse(sm2.verify(self.pub, self.MSG + b"!", (r, s)))
        self.assertFalse(sm2.verify(self.pub, self.MSG, (r ^ 1, s)))
        self.assertFalse(sm2.verify(self.pub, self.MSG, (r, s ^ 1)))
        self.assertFalse(sm2.verify(self.pub, self.MSG, (0, s)))
        self.assertFalse(sm2.verify(self.pub, self.MSG, (r, sm2.N)))
        other = sm2.PrivateKey.generate().public_key
        self.assertFalse(sm2.verify(other, self.MSG, (r, s)))

    def test_user_id_changes_signature(self) -> None:
        r, s = sm2.sign(self.priv, self.MSG, user_id=b"village-4401")
        self.assertFalse(sm2.verify(self.pub, self.MSG, (r, s)))
        self.assertTrue(sm2.verify(self.pub, self.MSG, (r, s), user_id=b"village-4401"))

    def test_serialization_roundtrip(self) -> None:
        self.assertEqual(sm2.PublicKey.from_bytes(self.pub.to_bytes()), self.pub)
        self.assertEqual(sm2.PrivateKey.from_bytes(self.priv.to_bytes()), self.priv)

    def test_signature_encoding(self) -> None:
        sig = (self.R, self.S)
        self.assertEqual(sm2.decode_signature(sm2.encode_signature(sig)), sig)
        self.assertEqual(sm2.decode_der(sm2.encode_der(sig)), sig)

    def test_invalid_public_key_rejected(self) -> None:
        with self.assertRaises(sm2.SM2Error):
            sm2.PublicKey(1, 1)

    def test_invalid_private_key_rejected(self) -> None:
        with self.assertRaises(sm2.SM2Error):
            sm2.PrivateKey(0)
        with self.assertRaises(sm2.SM2Error):
            sm2.PrivateKey(sm2.N)


if __name__ == "__main__":
    unittest.main()
