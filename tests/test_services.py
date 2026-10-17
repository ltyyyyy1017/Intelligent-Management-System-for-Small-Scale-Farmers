"""领域服务测试：记账状态机、快照不可变性、凭证、外发控制、备份。"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import repo
from app.backup import create_backup, restore_backup
from app.clock import today
from app.errors import Conflict, DataIntegrityError, NotFound, UserError, ValidationError
from app.evidence import EvidenceService
from app.ledger import LedgerService, month_summary, parse_amount_fen, parse_qty
from app.privacy_svc import OutboundService
from app.report_svc import ReportService
from app.selfcheck import run_selfcheck
from app.snapshot_svc import SnapshotService
from app.verify import verify_exported
from tests.helpers import build_data


class AmountTest(unittest.TestCase):
    def test_解析元(self):
        self.assertEqual(parse_amount_fen("200"), 20000)
        self.assertEqual(parse_amount_fen("200.5"), 20050)
        self.assertEqual(parse_amount_fen("1,200.50"), 120050)
        self.assertEqual(parse_amount_fen("￥88"), 8800)
        self.assertEqual(parse_amount_fen("200元"), 20000)

    def test_拒绝非法金额(self):
        for bad in ("", "abc", "-5", 0, "0"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    parse_amount_fen(bad)
        with self.assertRaises(ValidationError):
            parse_amount_fen(True)

    def test_数量必须有单位(self):
        self.assertIsNone(parse_qty(""))
        with self.assertRaises(ValidationError):
            parse_qty("50")


class LedgerServiceTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data(events=False)
        self.fid = ids[0]
        self.svc = LedgerService(self.conn, "测试员")

    def tearDown(self):
        self.conn.close()

    def _record(self, **kw):
        base = dict(
            farmer_id=self.fid, date_text="2026-05-06", type="variable_cost",
            category="化肥", amount_fen=20000,
        )
        base.update(kw)
        return self.svc.record(**base)

    def test_记账后可读回(self):
        eid = self._record()
        ev = self.svc.get(eid)
        self.assertEqual(ev["amount_fen"], 20000)
        self.assertEqual(ev["state"], "confirmed")

    def test_重复记账被拦(self):
        self._record()
        with self.assertRaises(Conflict):
            self._record()
        # 显式强制可以再记一笔
        self._record(force=True)

    def test_已确认的账不能改(self):
        eid = self._record()
        with self.assertRaises(Conflict):
            self.svc.update_draft(eid, amount_fen=30000)

    def test_草稿可以改(self):
        eid = self._record(state="draft")
        updated = self.svc.update_draft(eid, amount_fen=30000, note="改了")
        self.assertEqual(updated["amount_fen"], 30000)

    def test_作废后不计入月结(self):
        eid = self._record(amount_fen=50000)
        self.svc.void(eid, "记错了")
        summary = month_summary(self.conn, self.fid, "2026-05")
        self.assertEqual(summary["expense_fen"], 0)

    def test_日期不能太超前(self):
        with self.assertRaises(ValidationError):
            self._record(date_text="2099-01-01")

    def test_科目归一(self):
        eid = self._record(category="复合肥")
        self.assertEqual(self.svc.get(eid)["category"], "化肥")

    def test_地块必须属于该农户(self):
        with self.assertRaises(ValidationError):
            self._record(plot_id=9999)

    def test_每次操作都进审计链(self):
        eid = self._record(state="draft")
        self.svc.confirm(eid)
        self.svc.void(eid, "x")
        from app.audit_svc import AuditService

        self.assertTrue(AuditService(self.conn).verify()["ok"])
        self.assertGreaterEqual(len(repo.audit_rows(self.conn)), 3)


class SnapshotServiceTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data()
        self.fid = ids[0]
        self.year = str(today().year)
        self.svc = SnapshotService(self.conn, self.cfg, self.keys, "村委-李四")

    def tearDown(self):
        self.conn.close()

    def test_快照自带输入_后续补记不影响旧快照(self):
        snap = self.svc.create(self.fid, self.year)
        before = self.svc.replay(snap["id"])
        self.assertTrue(before["ok"])
        # 事后补记一笔：快照必须仍然可重放（因为它自带输入）
        LedgerService(self.conn, "测试员").record(
            farmer_id=self.fid, date_text=f"{self.year}-06-01", type="variable_cost",
            category="农药", amount_fen=12345, force=True,
        )
        after = self.svc.replay(snap["id"])
        self.assertTrue(after["ok"], msg=after)

    def test_验签与重放通过(self):
        snap = self.svc.create(self.fid, self.year)
        result = self.svc.verify(snap["id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["signature"]["ok"])
        self.assertTrue(result["replay"]["ok"])

    def test_篡改输出被重放发现(self):
        snap = self.svc.create(self.fid, self.year)
        from app.db import transaction

        with transaction(self.conn):
            self.conn.execute(
                "UPDATE snapshot SET output_json = ? WHERE id = ?",
                (json.dumps({"result": {"net_income_fen": 1}}), snap["id"]),
            )
        result = self.svc.verify(snap["id"])
        self.assertFalse(result["replay"]["ok"])

    def test_篡改快照字段签名失效(self):
        snap = self.svc.create(self.fid, self.year)
        row = repo.get_snapshot(self.conn, snap["id"])
        data = json.loads(row["snapshot_json"])
        data["merkle_root"] = "0" * 64
        result = verify_exported(data)
        self.assertFalse(result["signature_ok"])

    def test_没有已确认事件时拒绝出表(self):
        cfg, keys, conn, ids = build_data(events=False)
        try:
            with self.assertRaises(ValidationError):
                SnapshotService(conn, cfg, keys, "x").create(ids[0], "2026")
        finally:
            conn.close()


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data(events=False)
        self.svc = EvidenceService(self.conn, self.cfg, self.keys, "测试员")

    def tearDown(self):
        self.conn.close()

    def test_存取与哈希校验(self):
        eid = self.svc.store(b"fake-image-bytes", "image", "票据.jpg")
        self.assertEqual(self.svc.read(eid), b"fake-image-bytes")
        self.assertTrue(self.svc.verify(eid)["ok"])

    def test_文件被替换会被发现(self):
        eid = self.svc.store(b"original", "image", "a.jpg")
        row = self.svc.get(eid)
        path = self.cfg.evidence_dir / row["rel_path"]
        # 用合法密钥重新封装了不同内容：哈希对不上
        path.write_bytes(self.keys.seal(b"tampered", aad=eid.encode()))
        self.assertFalse(self.svc.verify(eid)["ok"])

    def test_空文件被拒(self):
        with self.assertRaises(UserError):
            self.svc.store(b"", "image")


class OutboundTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data()
        self.fid = ids[0]
        year = str(today().year)
        self.snap = SnapshotService(self.conn, self.cfg, self.keys, "村委").create(self.fid, year)
        self.svc = OutboundService(self.conn, self.cfg, self.keys, "村委")

    def tearDown(self):
        self.conn.close()

    def test_预览只含L2L3(self):
        preview = self.svc.preview(self.snap["id"])
        self.assertTrue(preview.ok)
        self.assertTrue(all(f["level"] in ("L2", "L3") for f in preview.fields))
        self.assertFalse(any(f["key"] == "real_name" for f in preview.fields))

    def test_未勾选同意被拒(self):
        with self.assertRaises(UserError):
            self.svc.build(self.snap["id"], "某银行", "贷款", consent=False)

    def test_缺少接收方被拒(self):
        with self.assertRaises(UserError):
            self.svc.build(self.snap["id"], "", "贷款", consent=True)

    def test_正常外发并留痕(self):
        result = self.svc.build(self.snap["id"], "某农商行", "春耕贷款", consent=True)
        self.assertTrue(Path(result["path"]).exists())
        history = self.svc.history()
        self.assertEqual(history[0]["decision"], "approved")
        self.assertEqual(history[0]["recipient"], "某农商行")

    def test_含L1时阻断(self):
        # 把农户姓名写进报表标题，模拟"模板被改坏"导致 L1 泄露
        from app import report_svc

        original = report_svc.ReportService.build

        def leaky(self, sid, template):
            report = original(self, sid, template)
            report.title = "农户1的经营报表"
            return report

        report_svc.ReportService.build = leaky
        try:
            with self.assertRaises(DataIntegrityError):
                self.svc.build(self.snap["id"], "某农商行", "贷款", consent=True)
        finally:
            report_svc.ReportService.build = original
        self.assertEqual(self.svc.history()[0]["decision"], "blocked")


class BackupTest(unittest.TestCase):
    def test_备份与恢复(self):
        cfg, keys, conn, ids = build_data()
        farmer_count = len(repo.list_farmers(conn))
        conn.close()
        path = create_backup(cfg)
        self.assertTrue(path.exists())

        # 恢复到全新目录
        with tempfile.TemporaryDirectory(prefix="nfm-restore-") as tmp:
            from app.config import Config

            target = Config(data_dir=Path(tmp))
            restore_backup(target, path, overwrite=True)
            from app.db import connect

            restored = connect(target)
            try:
                self.assertEqual(len(repo.list_farmers(restored)), farmer_count)
                self.assertTrue(repo.get_village(restored))
            finally:
                restored.close()

    def test_加密备份需要口令(self):
        cfg, keys, conn, ids = build_data(events=False)
        conn.close()
        path = create_backup(cfg, "s3cret-pass")
        with tempfile.TemporaryDirectory(prefix="nfm-restore2-") as tmp:
            from app.config import Config

            target = Config(data_dir=Path(tmp))
            with self.assertRaises(UserError):
                restore_backup(target, path, overwrite=True)
            restore_backup(target, path, passphrase="s3cret-pass", overwrite=True)
            self.assertTrue(target.db_path.exists())


class SelfcheckTest(unittest.TestCase):
    def test_自检通过(self):
        cfg, keys, conn, ids = build_data(events=False)
        try:
            result = run_selfcheck(conn, cfg, keys)
            self.assertTrue(result["ok"], msg=[i for i in result["items"] if not i["ok"]])
        finally:
            conn.close()


class ReportServiceTest(unittest.TestCase):
    def setUp(self):
        self.cfg, self.keys, self.conn, ids = build_data()
        self.svc = ReportService(self.conn, self.cfg, self.keys, "村委")
        self.snap = SnapshotService(self.conn, self.cfg, self.keys, "村委").create(
            ids[0], str(today().year)
        )

    def tearDown(self):
        self.conn.close()

    def test_三种模板都能导出(self):
        for tpl in ("T1-v1", "T2-v1", "T3-v1"):
            result = self.svc.export(self.snap["id"], tpl, "html")
            text = result.path.read_text(encoding="utf-8")
            self.assertIn("报表脚注", text)
            self.assertNotIn("身份证", text.split("本报表不含")[0])

    def test_导出内容不含L1(self):
        result = self.svc.export(self.snap["id"], "T1-v1", "html")
        text = result.path.read_text(encoding="utf-8")
        self.assertNotIn("13800138000", text)
        self.assertNotIn("农户1", text)  # 姓名也不应出现
        self.assertIn("F-", text)  # 只出现代币

    def test_Excel导出是合法zip(self):
        result = self.svc.export(self.snap["id"], "T1-v1", "xlsx")
        with zipfile.ZipFile(result.path) as z:
            names = z.namelist()
            self.assertIn("xl/workbook.xml", names)
            self.assertTrue(any(n.startswith("xl/worksheets/sheet") for n in names))
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("净收益", sheet)

    def test_银行材料包含快照与说明(self):
        result = self.svc.export(self.snap["id"], "T1-v1", "zip")
        with zipfile.ZipFile(result.path) as z:
            names = z.namelist()
            self.assertTrue(any("快照.json" in n for n in names))
            self.assertTrue(any("验证说明" in n for n in names))
            self.assertTrue(any("报表" in n for n in names))

    def test_未知模板被拒(self):
        with self.assertRaises(ValidationError):
            self.svc.export(self.snap["id"], "T9", "html")


if __name__ == "__main__":
    unittest.main()
