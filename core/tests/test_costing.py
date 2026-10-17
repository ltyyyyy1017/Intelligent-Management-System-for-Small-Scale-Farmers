import unittest
from datetime import date
from decimal import Decimal

from core import money
from core.costing import compute, sensitivity
from core.models import Event, MachineryUse, Params, ValidationError
from core.tests import fixtures


def _run(**kwargs):
    return compute(
        fixtures.events(),
        fixtures.plots(),
        fixtures.params(),
        fixtures.rule(),
        fixtures.machinery_use(),
        **kwargs,
    )


class CostingInvariantTest(unittest.TestCase):
    def setUp(self):
        self.r = _run()

    def test_总成本等于现金加推算(self):
        cost = self.r["cost"]
        self.assertEqual(cost["total_fen"], cost["cash_fen"] + cost["imputed_fen"])
        self.assertEqual(
            cost["imputed_fen"],
            cost["land_opportunity_fen"]
            + cost["depreciation_fen"]
            + cost["family_labor_fen"]
            + cost["capital_fen"],
        )

    def test_净收益等于收入减总成本(self):
        self.assertEqual(
            self.r["result"]["net_income_fen"],
            self.r["revenue"]["total_fen"] - self.r["cost"]["total_fen"],
        )

    def test_亩均净收益(self):
        area = Decimal(self.r["area"]["total_mu"])
        self.assertEqual(self.r["result"]["per_mu_net_fen"],
                         money.div(self.r["result"]["net_income_fen"], area))

    def test_补贴不计入经营收入(self):
        self.assertEqual(self.r["revenue"]["operating_fen"], 1_352_000)
        self.assertEqual(self.r["revenue"]["subsidy_fen"], 30_000)

    def test_草稿事件默认不参与核算(self):
        # E-000009（灌溉电费 250 元）为 draft，默认被排除
        self.assertEqual(self.r["cost"]["variable_fen"],
                         200_000 + 80_000 + 60_000 + 150_000 + 40_000)
        self.assertEqual(self.r["meta"]["events_total"], 8)

    def test_作废事件永不参与(self):
        events = fixtures.events() + [
            Event(id="E-000099", farmer_token="F-ABCDEF2345",
                  date=date(2026, 6, 1),
                  type="variable_cost", category="化肥", amount_fen=999_999,
                  state="voided")
        ]
        r = compute(events, fixtures.plots(), fixtures.params(), fixtures.rule(),
                    fixtures.machinery_use())
        self.assertEqual(r["meta"]["events_total"], 8)

    def test_确定性_两次结果完全一致(self):
        self.assertEqual(_run(), _run())

    def test_折旧按工作量法(self):
        # (1,200,000 × 0.95) × 12 / 8000 = 1710 分
        self.assertEqual(self.r["cost"]["depreciation_fen"], 1710)
        self.assertEqual(self.r["cost"]["depreciation_detail"]["旋耕机"], 1710)

    def test_折旧封顶(self):
        use = [MachineryUse(name="旋耕机", mu=Decimal("99999"), prior_depreciation_fen=0)]
        r = compute(fixtures.events(), fixtures.plots(), fixtures.params(),
                    fixtures.rule(), use)
        self.assertEqual(r["cost"]["depreciation_fen"], money.mul(1_200_000, Decimal("0.95")))

    def test_工作量法缺作业量必须报错(self):
        use = [MachineryUse(name="旋耕机", mu=Decimal("0"))]
        with self.assertRaises(ValidationError):
            compute(fixtures.events(), fixtures.plots(), fixtures.params(),
                    fixtures.rule(), use)

    def test_自用工按α折算(self):
        low = _run(alpha=Decimal("0.6"))
        high = _run(alpha=Decimal("0.8"))
        self.assertLess(low["cost"]["family_labor_fen"], high["cost"]["family_labor_fen"])
        self.assertGreater(low["result"]["net_income_fen"], high["result"]["net_income_fen"])

    def test_α超出区间拒绝计算(self):
        with self.assertRaises(ValidationError):
            _run(alpha=Decimal("1.2"))

    def test_保本价两口径_地头低于到场(self):
        be = self.r["breakeven"]
        self.assertIsNotNone(be["farm_gate"])
        self.assertLess(be["farm_gate"], be["delivered"])

    def test_保本价口径正确(self):
        be = self.r["breakeven"]
        expected = money.div(
            self.r["cost"]["total_fen"] - self.r["revenue"]["subsidy_fen"], Decimal("5200")
        )
        self.assertEqual(be["delivered"], expected)
        expected_farm = money.div(
            self.r["cost"]["total_fen"] - self.r["revenue"]["subsidy_fen"]
            - self.r["cost"]["selling_expense_fen"], Decimal("5200")
        )
        self.assertEqual(be["farm_gate"], expected_farm)

    def test_售价系数缩放收入(self):
        r = _run(price_factor=Decimal("1.2"))
        self.assertEqual(r["revenue"]["operating_fen"], money.mul(1_352_000, Decimal("1.2")))

    def test_自有土地机会成本只算自有地(self):
        # 8 亩 × 800 元/亩 = 6,400 元
        self.assertEqual(self.r["cost"]["land_opportunity_fen"], 640_000)

    def test_参数缺工价必须报错(self):
        p = fixtures.params()
        broken = Params(
            region_code=p.region_code, version=p.version,
            day_wage={k: v for k, v in p.day_wage.items() if k != "收割"},
            hours_per_mu=p.hours_per_mu, shadow_wage_alpha=p.shadow_wage_alpha,
            land_rent_per_mu=p.land_rent_per_mu, machinery=p.machinery,
        )
        with self.assertRaises(ValidationError):
            compute(fixtures.events(), fixtures.plots(), broken, fixtures.rule(),
                    fixtures.machinery_use())


class SensitivityTest(unittest.TestCase):
    def test_区间包含基准且能识别最敏感因素(self):
        s = sensitivity(fixtures.events(), fixtures.plots(), fixtures.params(),
                        fixtures.rule(), fixtures.machinery_use())
        self.assertLessEqual(s["worst_net_income_fen"], s["base_net_income_fen"])
        self.assertGreaterEqual(s["best_net_income_fen"], s["base_net_income_fen"])
        self.assertIn(s["most_sensitive_factor"], ("alpha", "price", "yield"))
        self.assertEqual(len(s["scans"]), 3)


if __name__ == "__main__":
    unittest.main()
