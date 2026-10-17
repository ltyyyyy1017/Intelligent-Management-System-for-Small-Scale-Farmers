"""Web 集成测试：走真实 HTTP，覆盖登录、记账、出报表、验证、权限与 CSRF。"""
from __future__ import annotations

import json
import unittest

from tests.helpers import ADMIN_PASSWORD, ADMIN_USER, Client, build_env, login


class WebTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = build_env()
        cls.cfg = cls.env.cfg
        cls.keys = cls.env.keys

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def client(self, login_first: bool = True) -> Client:
        c = Client(self.env.base)
        if login_first:
            login(c)
        return c


class AuthTest(WebTestBase):
    def test_未登录访问首页跳转登录页(self):
        c = Client(self.env.base)
        c.get("/", follow=False)
        self.assertEqual(c.last_status, 302)
        self.assertIn("/login", c.last_location)

    def test_登录成功进入首页(self):
        c = self.client()
        self.assertIn("记一笔", c.last_body)
        self.assertIn("乡村财务智管家", c.last_body)

    def test_错误口令不通过(self):
        c = Client(self.env.base)
        c.get("/login")
        c.post("/login", {"username": ADMIN_USER, "password": "wrong"})
        self.assertIn("账号或口令不对", c.last_body)

    def test_退出后回到登录页(self):
        c = self.client()
        c.get("/")
        csrf = c.csrf
        c.post("/logout", {"_csrf": csrf})
        c.get("/", follow=False)
        self.assertEqual(c.last_status, 302)

    def test_未登录访问接口返回401(self):
        c = Client(self.env.base)
        c.get("/api/farmers", follow=False)
        self.assertEqual(c.last_status, 401)

    def test_会话过期时间与系统时区一致(self):
        # 时区不一致会让字符串比较得出"立刻过期"，登录后下一次请求就掉线
        c = self.client()
        conn = self.env.conn()
        try:
            row = conn.execute(
                "SELECT expires_at FROM session ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertTrue(row["expires_at"].endswith("+08:00"), row["expires_at"])


class EntryTest(WebTestBase):
    def test_手动记账成功(self):
        c = self.client()
        c.get("/entry")
        csrf = c.csrf
        self.assertTrue(csrf)
        c.post("/entry", {
            "_csrf": csrf, "farmer": "1", "type": "variable_cost", "amount": "88.50",
            "category": "化肥", "date": "2026-05-06", "qty": "10", "unit": "kg", "note": "测试",
        })
        self.assertIn("记好了", c.last_body)
        conn = self.env.conn()
        row = conn.execute("SELECT * FROM event WHERE amount_fen = 8850").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["category"], "化肥")

    def test_重复记账被拦(self):
        c = self.client()
        c.get("/entry")
        csrf = c.csrf
        payload = {
            "_csrf": csrf, "farmer": "1", "type": "variable_cost", "amount": "66.00",
            "category": "农药", "date": "2026-05-07", "qty": "", "unit": "", "note": "",
        }
        c.post("/entry", payload)
        self.assertIn("记好了", c.last_body)
        c.get("/entry")
        c.post("/entry", {**payload, "_csrf": c.csrf})
        self.assertIn("已经记过", c.last_body)

    def test_CSRF缺失被拒(self):
        c = self.client()
        c.get("/entry")
        c.post("/entry", {"farmer": "1", "amount": "10", "category": "化肥", "date": "2026-05-06"},
               follow=False)
        self.assertEqual(c.last_status, 403)

    def test_金额格式错误有中文提示(self):
        c = self.client()
        c.get("/entry")
        c.post("/entry", {
            "_csrf": c.csrf, "farmer": "1", "type": "variable_cost", "amount": "abc",
            "category": "化肥", "date": "2026-05-06",
        })
        self.assertIn("看不懂这个金额", c.last_body)

    def test_一句话记账全流程(self):
        c = self.client()
        c.get("/quick")
        c.post("/quick", {"_csrf": c.csrf, "farmer": "1", "text": "昨天买复合肥花了三百五十块"})
        self.assertIn("请核对这一笔", c.last_body)
        self.assertIn("化肥", c.last_body)
        # 核对页里带着抽取结果，直接保存
        import re

        form = re.search(r'<form method="post" action="/quick/save".*?</form>', c.last_body, re.S)
        self.assertIsNotNone(form)
        csrf = re.search(r'name="_csrf" value="([^"]+)"', c.last_body).group(1)
        c.post("/quick/save", {
            "_csrf": csrf, "farmer": "1", "amount": "350", "category": "化肥",
            "date": "2026-05-19", "type": "variable_cost", "note": "昨天买复合肥花了三百五十块",
        })
        self.assertIn("记好了", c.last_body)


class ReportTest(WebTestBase):
    def test_生成快照并验证通过(self):
        c = self.client()
        c.get("/reports")
        c.post("/reports/create", {"_csrf": c.csrf, "farmer": "1", "period": "2026"})
        self.assertIn("已生成快照", c.last_body)
        conn = self.env.conn()
        row = conn.execute("SELECT id FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        c.get(f"/verify?sid={row['id']}")
        self.assertIn("验证结果：通过", c.last_body)
        self.assertIn("SM2 签名", c.last_body)

    def test_导出HTML报表(self):
        c = self.client()
        conn = self.env.conn()
        row = conn.execute("SELECT id FROM snapshot LIMIT 1").fetchone()
        conn.close()
        if not row:
            c.get("/reports")
            c.post("/reports/create", {"_csrf": c.csrf, "farmer": "1", "period": "2026"})
            conn = self.env.conn()
            row = conn.execute("SELECT id FROM snapshot LIMIT 1").fetchone()
            conn.close()
        c.get("/reports")
        c.post("/reports/export", {
            "_csrf": c.csrf, "sid": row["id"], "template": "T1-v1", "format": "html",
        })
        self.assertIn("T1 信贷尽调版", c.last_body)
        self.assertIn("净收益", c.last_body)
        self.assertIn("Merkle 根", c.last_body)

    def test_未生成快照时导出被拒(self):
        c = self.client()
        c.get("/reports")
        c.post("/reports/export", {"_csrf": c.csrf, "sid": "S-1999-0001", "template": "T1-v1",
                                   "format": "html"})
        self.assertIn("找不到快照", c.last_body)


class PermissionTest(WebTestBase):
    def test_经办人不能进管理页(self):
        c = Client(self.env.base)
        login(c, "clerk", "clerk-2026")
        c.get("/admin", follow=False)
        self.assertEqual(c.last_status, 403)

    def test_管理员能进管理页(self):
        c = self.client()
        c.get("/admin")
        self.assertIn("农户档案", c.last_body)
        self.assertIn("设备标识", c.last_body)

    def test_自检页面可用(self):
        c = self.client()
        c.get("/admin/selfcheck")
        self.assertIn("自检结果", c.last_body)

    def test_审计页显示链状态(self):
        c = self.client()
        c.get("/admin/audit")
        self.assertIn("审计链", c.last_body)

    def test_隐私页可用(self):
        c = self.client()
        c.get("/privacy")
        self.assertIn("数据分级", c.last_body)
        self.assertIn("外发记录", c.last_body)

    def test_登记农机作业量(self):
        c = self.client()
        c.get("/admin")
        self.assertIn("农机作业量", c.last_body)
        c.post("/admin/machinery", {
            "_csrf": c.csrf, "farmer_id": "1", "period": "2026", "name": "旋耕机",
            "mu": "12", "prior": "0", "year_index": "1",
        })
        self.assertIn("已保存", c.last_body)
        conn = self.env.conn()
        try:
            row = conn.execute(
                "SELECT * FROM machinery_use WHERE farmer_id = 1 AND period = '2026'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["mu"], "12")


    def test_批量导入模板与导入(self):
        c = self.client()
        c.get("/admin/import")
        self.assertIn("下载导入模板", c.last_body)
        csrf = c.csrf
        c.get("/admin/import/template.xlsx")
        self.assertEqual(c.last_status, 200)
        self.assertIn("spreadsheetml", c.last_headers.get("content-type", ""))
        self.assertTrue(c.last_bytes.startswith(b"PK"))
        template = c.last_bytes
        c.post_multipart(
            "/admin/import",
            {"_csrf": csrf, "farmer_id": "1", "year": "2026", "state": "draft"},
            {"file": ("导入模板.xlsx", template)},
        )
        self.assertIn("导入结果", c.last_body)
        self.assertIn("读到", c.last_body)
        conn = self.env.conn()
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM event WHERE source = 'import'").fetchone()["n"]
        finally:
            conn.close()
        self.assertGreaterEqual(count, 3)


class ApiTest(WebTestBase):
    def test_health无需登录(self):
        c = Client(self.env.base)
        c.get("/api/health")
        data = json.loads(c.last_body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["village"], "测试村")

    def test_extract接口(self):
        c = self.client()
        c.get("/")
        c.post_json("/api/extract", {"text": "今天买化肥花了200元"}, csrf=c.csrf)
        data = json.loads(c.last_body)
        self.assertEqual(data["amount"]["value"], 20000)
        self.assertEqual(data["category"]["value"], "化肥")


class HelpAndEvidenceTest(WebTestBase):
    def test_帮助页说明能力边界(self):
        c = self.client()
        c.get("/help")
        self.assertIn("三步记一笔", c.last_body)
        self.assertIn("未安装", c.last_body)
        self.assertIn("票据拍照归档", c.last_body)

    def test_经办人导航里没有管理入口(self):
        c = Client(self.env.base)
        login(c, "clerk", "clerk-2026")
        c.get("/")
        self.assertNotIn('href="/admin"', c.last_body)
        self.assertIn('href="/ledger"', c.last_body)

    def test_带票据照片记账并归档(self):
        c = self.client()
        c.get("/entry")
        csrf = c.csrf
        photo = b"\x89PNG\r\n\x1a\n" + b"fake-image-content" * 10
        c.post_multipart(
            "/entry",
            {
                "_csrf": csrf, "farmer": "1", "type": "variable_cost", "amount": "123.45",
                "category": "农药", "date": "2026-05-08", "qty": "", "unit": "", "note": "带票据",
            },
            {"evidence": ("ticket.png", photo)},
        )
        self.assertIn("记好了", c.last_body)
        conn = self.env.conn()
        ev_row = conn.execute("SELECT * FROM evidence ORDER BY captured_at DESC LIMIT 1").fetchone()
        event_row = conn.execute(
            "SELECT * FROM event WHERE amount_fen = 12345 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(ev_row)
        self.assertEqual(event_row["evidence_id"], ev_row["id"])

        from app.evidence import EvidenceService

        conn = self.env.conn()
        try:
            svc = EvidenceService(conn, self.cfg, self.keys, "test")
            self.assertEqual(svc.read(ev_row["id"]), photo)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
