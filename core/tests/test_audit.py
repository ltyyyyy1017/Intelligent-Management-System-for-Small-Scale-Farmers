import json
import unittest

from core.audit import AuditChain
from core.crypto.sm3 import sm3_hex


class AuditChainTest(unittest.TestCase):
    def setUp(self):
        self.chain = AuditChain("4401xx-某村")
        for i in range(5):
            self.chain.append(
                "村委-张三", "event.confirm", f"E-00000{i}", {"amount_fen": i * 100},
                ts=f"2026-04-1{i}T09:00:00+08:00",
            )

    def test_正常链校验通过(self):
        r = self.chain.verify()
        self.assertTrue(r["ok"])
        self.assertEqual(r["length"], 5)

    def test_改一条内容被发现(self):
        self.chain.entries[2]["payload_digest"] = "0" * 64
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], 2)

    def test_改金额但重算该条哈希仍被发现(self):
        entry = self.chain.entries[2]
        entry["payload_digest"] = "f" * 64
        # 模拟攻击者重算了这一条的 hash，但没有重算后续条目
        body = "|".join([
            entry["prev_hash"], str(entry["seq"]), entry["ts"], entry["actor"],
            entry["action"], entry["object_ref"], entry["payload_digest"],
        ])
        entry["hash"] = sm3_hex(body.encode())
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], 3, "断点应出现在下一条，因为它的 prev_hash 对不上")

    def test_删除一条被发现(self):
        del self.chain.entries[2]
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertIn("seq", r["reason"])

    def test_改时间戳被发现(self):
        self.chain.entries[1]["ts"] = "2026-01-01T00:00:00+08:00"
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], 1)

    def test_链尾hash稳定(self):
        head1 = self.chain.head()
        self.chain.append("村委-张三", "report.export", "S-1", {"tpl": "T1"}, ts="2026-05-01T10:00:00+08:00")
        self.assertNotEqual(head1, self.chain.head())

    def test_可序列化后重新校验(self):
        dumped = json.dumps({"village_id": "4401xx-某村", "entries": self.chain.entries},
                            ensure_ascii=False)
        loaded = json.loads(dumped)
        restored = AuditChain(loaded["village_id"], loaded["entries"])
        self.assertTrue(restored.verify()["ok"])

    def test_相同输入相同哈希(self):
        a = AuditChain("村A")
        b = AuditChain("村A")
        for c in (a, b):
            c.append("张三", "event.confirm", "E-1", {"x": 1}, ts="2026-01-01T00:00:00+08:00")
        self.assertEqual(a.head(), b.head())

    def test_村ID不同则创世哈希不同(self):
        a, b = AuditChain("村A"), AuditChain("村B")
        a.append("张三", "x", "y", ts="")
        b.append("张三", "x", "y", ts="")
        self.assertNotEqual(a.head(), b.head())


if __name__ == "__main__":
    unittest.main()
