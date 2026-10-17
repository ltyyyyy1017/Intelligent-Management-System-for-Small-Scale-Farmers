import json
import unittest

from core.audit import AuditChain


class AuditChainTest(unittest.TestCase):
    def setUp(self):
        self.chain = AuditChain("4401xx-某村")
        for i in range(5):
            self.chain.append("村委-张三", "event.confirm", f"E-00000{i}", {"amount_fen": i * 100})

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
        import hashlib
        body = "|".join([entry["prev_hash"], str(entry["seq"]), entry["actor"],
                         entry["action"], entry["object_ref"], entry["payload_digest"]])
        entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], 3, "断点应出现在下一条，因为它的 prev_hash 对不上")

    def test_删除一条被发现(self):
        del self.chain.entries[2]
        r = self.chain.verify()
        self.assertFalse(r["ok"])
        self.assertIn("seq", r["reason"])

    def test_链尾hash稳定(self):
        head1 = self.chain.head()
        self.chain.append("村委-张三", "report.export", "S-1", {"tpl": "T1"})
        self.assertNotEqual(head1, self.chain.head())

    def test_可序列化后重新校验(self):
        dumped = json.dumps({"village_id": "4401xx-某村", "entries": self.chain.entries},
                            ensure_ascii=False)
        loaded = json.loads(dumped)
        restored = AuditChain(loaded["village_id"], loaded["entries"])
        self.assertTrue(restored.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
