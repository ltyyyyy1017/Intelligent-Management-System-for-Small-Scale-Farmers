"""隐私分级与脱敏测试。

重点不是"能扫出来"，而是：
- 校验位不对的号码**不能**误报（否则每笔都弹窗，用户会直接忽略提示）；
- L1 掩码后不能还原出完整号码；
- 外发复检必须在组合泄露时阻断。
"""
from __future__ import annotations

import unittest

from app.privacy import (
    L1,
    L2,
    Finding,
    ensure_no_l1,
    has_l1,
    l1_findings,
    mask,
    mask_text,
    scan,
    tokenize_name,
    validate_bank_card,
    validate_id_card,
)
from app.errors import DataIntegrityError


class ValidatorTest(unittest.TestCase):
    def test_身份证校验位(self):
        self.assertTrue(validate_id_card("11010519491231002X"))
        self.assertFalse(validate_id_card("110105194912310021"))  # 校验位错
        self.assertFalse(validate_id_card("11010519491231002"))   # 17 位
        self.assertFalse(validate_id_card("11010519491231002Y"))  # 非法字符

    def test_银行卡Luhn(self):
        self.assertTrue(validate_bank_card("4111111111111111"))
        self.assertTrue(validate_bank_card("4111 1111 1111 1111"))
        self.assertFalse(validate_bank_card("4111111111111112"))
        self.assertFalse(validate_bank_card("1234"))


class ScanTest(unittest.TestCase):
    def test_扫描身份证_手机号_银行卡(self):
        text = "户主 11010519491231002X，电话 138-0013-8000，卡号 4111111111111111"
        found = scan(text)
        kinds = {f.kind for f in found}
        self.assertIn("id_card", kinds)
        self.assertIn("phone", kinds)
        self.assertIn("bank_card", kinds)
        self.assertTrue(all(f.level == L1 for f in found))

    def test_校验位不对的号码不误报(self):
        text = "订单号 110105194912310021，编号 4111111111111112"
        self.assertEqual(scan(text), [])

    def test_已知姓名命中(self):
        found = scan("张三今天买了化肥", known_names=["张三"])
        self.assertEqual(found[0].kind, "real_name")
        self.assertEqual(found[0].text, "张三")

    def test_地址与村组分级(self):
        addr = {f.kind: f.level for f in scan("广东省清远市某县三坑镇新围村")}
        self.assertEqual(addr.get("address"), L1)
        group = {f.kind: f.level for f in scan("新围村 3 组")}
        self.assertEqual(group.get("group"), L2)

    def test_普通文本无命中(self):
        self.assertEqual(scan("买化肥 200 元，5 月 3 日追肥"), [])

    def test_重叠实体只报一次(self):
        found = scan("13800138000")
        self.assertEqual(len(found), 1)


class MaskTest(unittest.TestCase):
    def test_手机号掩码(self):
        self.assertEqual(mask("13800138000", "phone"), "138****8000")

    def test_身份证掩码(self):
        self.assertEqual(mask("11010519491231002X", "id_card"), "110105********002X")

    def test_姓名掩码(self):
        self.assertEqual(mask("张三", "real_name"), "张*")
        self.assertEqual(mask("欧阳修", "real_name"), "欧**")

    def test_掩码后不含原文(self):
        raw = "联系 13800138000 处理"
        masked = mask_text(raw)
        self.assertNotIn("13800138000", masked)
        self.assertIn("处理", masked)

    def test_掩码保持顺序与多次命中(self):
        raw = "13800138000 和 13900139000"
        masked = mask_text(raw)
        self.assertEqual(masked, "138****8000 和 139****9000")


class OutboundTest(unittest.TestCase):
    def test_L1命中阻止外发(self):
        with self.assertRaises(DataIntegrityError) as ctx:
            ensure_no_l1("身份证 11010519491231002X")
        self.assertEqual(ctx.exception.detail, "l1_leak")

    def test_无L1放行(self):
        ensure_no_l1("2026 年经营收入 13,520.00 元，净收益 28.65 元")

    def test_has_l1(self):
        self.assertTrue(has_l1("电话 13800138000"))
        self.assertFalse(has_l1("净收益 28.65 元"))

    def test_token稳定且不含明文(self):
        token = tokenize_name("张三", b"device-secret")
        self.assertTrue(token.startswith("T-"))
        self.assertEqual(token, tokenize_name("张三", b"device-secret"))
        self.assertNotEqual(token, tokenize_name("张三", b"other-secret"))
        self.assertNotIn("张三", token)


if __name__ == "__main__":
    unittest.main()
