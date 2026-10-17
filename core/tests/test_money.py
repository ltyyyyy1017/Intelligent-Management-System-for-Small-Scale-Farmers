import unittest
from decimal import Decimal

from core import money


class MoneyTest(unittest.TestCase):
    def test_fen_from_yuan(self):
        self.assertEqual(money.fen(200), 20000)
        self.assertEqual(money.fen("2.6"), 260)
        self.assertEqual(money.fen(Decimal("0.005")), 1)  # ROUND_HALF_UP，不是银行家舍入

    def test_float_is_rejected(self):
        with self.assertRaises(TypeError):
            money.fen(2.6)
        with self.assertRaises(TypeError):
            money.mul(100, 0.7)

    def test_mul_rounds_half_up(self):
        self.assertEqual(money.mul(1000, Decimal("0.7")), 700)
        self.assertEqual(money.mul(1, Decimal("0.5")), 1)
        self.assertEqual(money.mul(1, Decimal("0.49")), 0)

    def test_div_guard(self):
        self.assertEqual(money.div(1000, Decimal("3")), 333)
        with self.assertRaises(ZeroDivisionError):
            money.div(1000, 0)

    def test_fmt_yuan(self):
        self.assertEqual(money.fmt_yuan(123456), "1,234.56")
        self.assertEqual(money.fmt_yuan(-123456), "-1,234.56")
        self.assertEqual(money.fmt_yuan(5), "0.05")
        self.assertEqual(money.fmt_yuan(100000000), "1,000,000.00")

    def test_yuan_roundtrip(self):
        for fen_value in (0, 1, 99, 100, 123456789):
            self.assertEqual(money.fen(money.yuan(fen_value)), fen_value)


if __name__ == "__main__":
    unittest.main()
