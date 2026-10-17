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
        root = snapshot.merkle_root(snapshot._event_leaves(self.events))
        self.assertNotEqual(root, snapshot.merkle_root(snapshot._event_leaves(self.events[:-1])))
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
        self.assertIn("signature_payload_sha256", self.snap)
        self.assertEqual(self.snap["signed_by"], "村委-张三")


if __name__ == "__main__":
    unittest.main()
