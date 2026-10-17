"""批量导入测试：xlsx 读取、表头映射、坏行报告、重复跳过。"""
from __future__ import annotations

import unittest

from app import xlsx
from app.errors import UserError
from app.importer import import_rows, read_csv, read_table, read_xlsx
from tests.helpers import build_data


def make_xlsx(rows: list[list]) -> bytes:
    wb = xlsx.Workbook()
    ws = wb.add_sheet("导入")
    for row in rows:
        ws.append(row)
    return wb.to_bytes()


class XlsxReadTest(unittest.TestCase):
    def test_读回自己写的工作簿(self):
        data = make_xlsx([["日期", "科目", "金额"], ["2026-05-06", "化肥", 200]])
        rows = read_xlsx(data)
        self.assertEqual(rows[0], ["日期", "科目", "金额"])
        self.assertEqual(rows[1][0], "2026-05-06")
        self.assertEqual(rows[1][1], "化肥")
        self.assertAlmostEqual(float(rows[1][2]), 200.0)

    def test_非zip文件报错友好(self):
        with self.assertRaises(UserError):
            read_xlsx(b"not a zip")

    def test_csv读取(self):
        data = "日期,科目,金额\n2026-05-06,化肥,200\n".encode("utf-8-sig")
        rows = read_csv(data)
        self.assertEqual(rows[1][1], "化肥")

    def test_按扩展名或内容选择解析器(self):
        data = make_xlsx([["日期", "科目", "金额"]])
        self.assertEqual(read_table(data, "表格.xlsx")[0][0], "日期")
        self.assertEqual(read_table(data, "")[0][0], "日期")  # 以 PK 开头 → xlsx


class ImportRowsTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data(events=False)
        self.fid = ids[0]

    def tearDown(self):
        self.conn.close()

    def _rows(self, *data):
        return [
            ["日期", "类型", "科目", "金额", "数量", "单位", "备注"],
            *data,
        ]

    def test_正常导入为草稿(self):
        rows = self._rows(
            ["2026-05-06", "支出", "化肥", 200, 50, "kg", "复合肥"],
            ["2026-09-20", "收入", "主粮销售", 13520, 5200, "kg", "卖稻谷"],
        )
        summary = import_rows(self.conn, "测试员", self.fid, rows, state="draft")
        self.assertEqual(summary.created, 2)
        self.assertEqual(summary.errors, [])
        states = {r["state"] for r in self.conn.execute("SELECT state FROM event").fetchall()}
        self.assertEqual(states, {"draft"})

    def test_重复行跳过(self):
        rows = self._rows(
            ["2026-05-06", "支出", "化肥", 200, "", "", ""],
            ["2026-05-06", "支出", "化肥", 200, "", "", ""],
        )
        summary = import_rows(self.conn, "测试员", self.fid, rows)
        self.assertEqual(summary.created, 1)
        self.assertEqual(summary.duplicates, 1)

    def test_坏行带行号报告(self):
        rows = self._rows(
            ["不是日期", "支出", "化肥", 200, "", "", ""],
            ["2026-05-06", "支出", "化肥", "abc", "", "", ""],
            ["2026-05-07", "支出", "化肥", 300, "", "", ""],
        )
        summary = import_rows(self.conn, "测试员", self.fid, rows)
        self.assertEqual(summary.created, 1)
        self.assertEqual([e["row"] for e in summary.errors], [2, 3])
        self.assertIn("日期", summary.errors[0]["error"])

    def test_缺少表头被拒(self):
        with self.assertRaises(UserError) as ctx:
            import_rows(self.conn, "测试员", self.fid, [["日期", "金额"], ["2026-01-01", 1]])
        self.assertIn("科目", ctx.exception.message)

    def test_月日格式补年份(self):
        rows = self._rows(["5月6日", "支出", "化肥", 200, "", "", ""])
        summary = import_rows(self.conn, "测试员", self.fid, rows, year_hint="2026")
        self.assertEqual(summary.created, 1)
        row = self.conn.execute("SELECT date FROM event").fetchone()
        self.assertEqual(row["date"], "2026-05-06")

    def test_Excel日期序列号(self):
        # 2026-05-06 距 1899-12-30 的天数
        from datetime import date

        serial = (date(2026, 5, 6) - date(1899, 12, 30)).days
        rows = self._rows([serial, "支出", "化肥", 200, "", "", ""])
        summary = import_rows(self.conn, "测试员", self.fid, rows)
        self.assertEqual(summary.created, 1)
        row = self.conn.execute("SELECT date FROM event").fetchone()
        self.assertEqual(row["date"], "2026-05-06")


if __name__ == "__main__":
    unittest.main()
