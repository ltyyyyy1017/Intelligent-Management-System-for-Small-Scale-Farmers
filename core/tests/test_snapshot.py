import unittest
from datetime import date

from core import snapshot
from core.costing import compute
from core.models import Event
from core.tests import fixtures


def _run():
    return compute(
        fixtures.events(), fixtures.plots(), fixtures.params(),
        fixtures.rule(), fixtures.machinery_use(),
    )


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.events = fixtures.events()
        self.result = _run()
        self.snap = snapshot.make_snapshot(self.result, self.events, "村委-张三", "2026")

    def test_事件集哈希与顺序无关(self):
        shuffled = list(reversed(self.events))
        self.assertEqual(
            snapshot.event_set_hash(self.events), snapshot.event_set_hash(shuffled)
        )

    def test_改金额必改哈希(self):
        tampered = list(self.events)
        first = tampered[0]
        tampered[0] = Event(**{**first.__dict__, "amount_fen": first.amount_fen + 1})
        self.assertNotEqual(
            snapshot.event_set_hash(self.events), snapshot.event_set_hash(tampered)
        )

    def test_增删事件必改Merkle根(self):
        root = snapshot.merkle_root(snapshot.merkle_leaves(self.events))
        self.assertNotEqual(root, snapshot.merkle_root(snapshot.merkle_leaves(self.events[:-1])))
        self.assertEqual(root, self.snap["merkle_root"])

    def test_重放一致(self):
        r = snapshot.replay(self.snap, self.result, self.events, fixtures.plots(),
                            fixtures.params(), fixtures.rule(), fixtures.machinery_use())
        self.assertTrue(r["ok"], msg=r)
        self.assertEqual(r["diff"], [])

    def test_篡改结果被重放发现(self):
        bad = dict(self.result)
        bad["result"] = dict(self.result["result"])
        bad["result"]["net_income_fen"] += 100  # 偷偷多算 1 元
        r = snapshot.replay(self.snap, bad, self.events, fixtures.plots(),
                            fixtures.params(), fixtures.rule(), fixtures.machinery_use())
        self.assertFalse(r["ok"])
        self.assertIn("result", r["diff"])

    def test_篡改事件被重放发现(self):
        tampered = list(self.events)
        tampered[0] = Event(**{**tampered[0].__dict__, "amount_fen": tampered[0].amount_fen + 1})
        r = snapshot.replay(self.snap, self.result, tampered, fixtures.plots(),
                            fixtures.params(), fixtures.rule(), fixtures.machinery_use())
        self.assertFalse(r["ok"])
        self.assertFalse(r["checks"]["event_set_hash"])

    def test_签章载荷包含三个版本(self):
        self.assertIn("rule_version", self.snap)
        self.assertIn("param_version", self.snap)
        self.assertIn("signature_payload_hash", self.snap)
        self.assertEqual(self.snap["signed_by"], "村委-张三")

    def test_待签字节稳定且与时间无关(self):
        again = snapshot.make_snapshot(self.result, self.events, "村委-张三", "2026")
        self.assertEqual(snapshot.signing_bytes(self.snap), snapshot.signing_bytes(again))
        self.assertEqual(snapshot.snapshot_digest(self.snap), snapshot.snapshot_digest(again))

    def test_时间戳不同则待签字节不同(self):
        later = snapshot.make_snapshot(
            self.result, self.events, "村委-张三", "2026", created_at="2026-12-31T23:59:59+08:00"
        )
        self.assertNotEqual(snapshot.signing_bytes(self.snap), snapshot.signing_bytes(later))

    def test_SM2签章可被独立验证(self):
        from core.crypto import sm2

        key = sm2.PrivateKey.generate()
        payload = snapshot.signing_bytes(self.snap)
        sig = sm2.sign(key, payload)
        signed = snapshot.attach_signature(
            self.snap, sm2.encode_signature(sig).hex(), key.public_key.to_hex()
        )
        # 银行侧动作：拿待签字节 + 签名 + 公钥验签
        self.assertTrue(
            sm2.verify(
                sm2.PublicKey.from_hex(signed["public_key"]),
                snapshot.signing_bytes(signed),
                sm2.decode_signature(bytes.fromhex(signed["signature"])),
            )
        )
        # 改一个字段，签名即失效
        tampered = dict(signed)
        tampered["merkle_root"] = "0" * 64
        self.assertFalse(
            sm2.verify(
                sm2.PublicKey.from_hex(signed["public_key"]),
                snapshot.signing_bytes(tampered),
                sm2.decode_signature(bytes.fromhex(signed["signature"])),
            )
        )


if __name__ == "__main__":
    unittest.main()
