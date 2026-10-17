"""规则抽取器测试。

验收标准（对应 docs/技术方案V2.md §7.3）：
- 有明确"元/块"后缀的金额必须抽对；
- 数量、日期里的数字**不能**被误当金额；
- 拿不准的字段必须进 needs_confirm，而不是给个看起来像真的的数。
"""
from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.extract import extract, parse_chinese_number

REF = date(2026, 5, 20)


class ChineseNumberTest(unittest.TestCase):
    def test_常见口语数字(self):
        cases = {
            "两百": 200, "二百": 200, "十五": 15, "二十三": 23, "一百零五": 105,
            "一千二": 1200, "三百五": 350, "两千零五": 2005, "一万二千": 12000,
            "十": 10, "九百九十九": 999,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_chinese_number(text), Decimal(expected))

    def test_阿拉伯数字与小数(self):
        self.assertEqual(parse_chinese_number("200"), Decimal("200"))
        self.assertEqual(parse_chinese_number("1200.50"), Decimal("1200.50"))

    def test_解析不了返回None(self):
        self.assertIsNone(parse_chinese_number("化肥"))


class ExtractTest(unittest.TestCase):
    def test_完整句子(self):
        r = extract("今天买化肥花了200元", REF)
        self.assertEqual(r.date.value, "2026-05-20")
        self.assertEqual(r.amount_fen.value, 20000)
        self.assertEqual(r.category.value, "化肥")
        self.assertEqual(r.event_type.value, "variable_cost")
        self.assertEqual(r.needs_confirm, [])

    def test_卖粮带千分位(self):
        r = extract("昨天卖稻谷收入1,200.50元", REF)
        self.assertEqual(r.amount_fen.value, 120050)
        self.assertEqual(r.category.value, "主粮销售")
        self.assertEqual(r.event_type.value, "income")
        self.assertEqual(r.date.value, "2026-05-19")

    def test_中文数字与数量(self):
        r = extract("5月3日买尿素两袋花了三百五十块", REF)
        self.assertEqual(r.amount_fen.value, 35000)
        self.assertEqual(r.category.value, "化肥")
        self.assertEqual(r.qty.value, "2")
        self.assertEqual(r.unit, "袋")
        self.assertEqual(r.date.value, "2026-05-03")

    def test_数量里的数字不能当金额(self):
        r = extract("买了500公斤化肥", REF)
        self.assertEqual(r.amount_fen.value, 0)
        self.assertIn("amount", r.needs_confirm)

    def test_年份里的数字不能当金额(self):
        r = extract("2026年4月12日买种子", REF)
        self.assertEqual(r.amount_fen.value, 0)
        self.assertIn("amount", r.needs_confirm)

    def test_无单位金额置信度低(self):
        r = extract("买化肥 200", REF)
        self.assertEqual(r.amount_fen.value, 20000)
        self.assertIn("amount", r.needs_confirm)

    def test_雇工识别(self):
        r = extract("请人插秧花了一千二百元", REF)
        self.assertEqual(r.amount_fen.value, 120000)
        self.assertEqual(r.category.value, "雇工")

    def test_补贴单列(self):
        r = extract("收到种粮补贴300元", REF)
        self.assertEqual(r.category.value, "补贴")
        self.assertEqual(r.event_type.value, "income")

    def test_日期缺省用参考日且需确认(self):
        r = extract("买农药60元", REF)
        self.assertEqual(r.date.value, "2026-05-20")
        self.assertIn("date", r.needs_confirm)

    def test_类别无法识别需确认(self):
        r = extract("今天花了200元", REF)
        self.assertIn("category", r.needs_confirm)
        self.assertEqual(r.amount_fen.value, 20000)

    def test_输出契约字段齐全(self):
        d = extract("今天买化肥200元", REF).as_dict()
        for key in ("date", "amount", "category", "type", "needs_confirm", "note"):
            self.assertIn(key, d)
        for key in ("date", "amount", "category"):
            self.assertIn("conf", d[key])
            self.assertIn("value", d[key])

    def test_同一输入可复现(self):
        a = extract("5月3日买尿素两袋花了三百五十块", REF).as_dict()
        b = extract("5月3日买尿素两袋花了三百五十块", REF).as_dict()
        self.assertEqual(a, b)

    def test_跨年日期回退到上一年(self):
        r = extract("12月20日买化肥200元", date(2026, 1, 5))
        self.assertEqual(r.date.value, "2025-12-20")

    def test_空文本不崩(self):
        r = extract("", REF)
        self.assertEqual(r.amount_fen.value, 0)
        self.assertTrue(r.needs_confirm)


if __name__ == "__main__":
    unittest.main()
