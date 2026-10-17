"""报表服务：基于快照生成 CSV/HTML/XLSX，并记录"谁在何时导出了什么"。

关键点：**报表只能由快照渲染**，不能直接用当前库重算。理由：
一张已经盖章交给银行的报表，半年后农户又补记了几笔，如果报表是"实时重算"的，
那它与当初交给银行的那张就对不上了——凭证的效力就没了。快照是那张纸的数字化锚点。
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import repo
from .clock import now_iso
from .config import Config
from .db import next_counter, transaction
from .errors import NotFound, ValidationError
from .ids import report_id
from .report import TEMPLATE_INFO, build_report, render_html, render_xlsx
from .snapshot_svc import SnapshotService

__all__ = ["ReportService", "ExportResult"]


@dataclass
class ExportResult:
    report_id: str
    snapshot_id: str
    template: str
    format: str
    path: Path
    url: str


class ReportService:
    def __init__(self, conn, cfg: Config, keys, actor: str) -> None:
        self.conn = conn
        self.cfg = cfg
        self.keys = keys
        self.actor = actor

    # ---------- 构建（不落盘） ----------

    def build(self, sid: str, template: str):
        snap = SnapshotService(self.conn, self.cfg, self.keys, self.actor).get(sid)
        farmer = repo.get_farmer(self.conn, int(snap["farmer_id"]))
        if not farmer:
            raise NotFound("快照对应的农户不存在")
        event_ids = json.loads(snap["event_ids_json"])
        events = repo.get_events_by_ids(self.conn, event_ids)
        events.sort(key=lambda e: (e["date"], e["id"]))
        plots = repo.list_plots(self.conn, int(snap["farmer_id"]))
        village = repo.get_village(self.conn)
        return build_report(
            template,
            snap["output"],
            snap["snapshot"],
            period=snap["period"],
            farmer_token=farmer["token"],
            plots=plots,
            events=[_safe_event(e) for e in events],
            village_name=village.get("name", ""),
            snapshot_id=snap["id"],
        )

    # ---------- 导出 ----------

    def export(self, sid: str, template: str, fmt: str = "html") -> ExportResult:
        if template not in TEMPLATE_INFO:
            raise ValidationError(f"未知报表模板：{template}")
        if fmt not in ("html", "xlsx", "zip"):
            raise ValidationError(f"不支持的导出格式：{fmt}")

        snap = SnapshotService(self.conn, self.cfg, self.keys, self.actor).get(sid)
        report = self.build(sid, template)
        village = repo.get_village(self.conn)
        ts = now_iso()
        stamp = ts.replace(":", "").replace("-", "")[:15]
        base = f"{sid}-{template}-{stamp}"

        self.cfg.export_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "html":
            data = render_html(
                report, village_name=village.get("name", ""), generated_at=ts
            ).encode("utf-8")
            name = f"{base}.html"
        elif fmt == "xlsx":
            data = render_xlsx(report, village_name=village.get("name", ""))
            name = f"{base}.xlsx"
        else:
            data = self._zip_package(sid, snap, report, village.get("name", ""), ts)
            name = f"{base}.zip"

        path = self.cfg.export_dir / name
        path.write_bytes(data)

        with transaction(self.conn):
            seq = next_counter(self.conn, "report")
            rid = report_id(seq)
            repo.insert_report(
                self.conn,
                {
                    "id": rid,
                    "snapshot_id": sid,
                    "template": template,
                    "format": fmt,
                    "rel_path": name,
                    "created_at": ts,
                    "created_by": self.actor,
                },
            )
            from .audit_svc import AuditService

            AuditService(self.conn).append(
                self.actor,
                "report.export",
                rid,
                {"snapshot": sid, "template": template, "format": fmt},
                ts=ts,
            )
        return ExportResult(
            report_id=rid, snapshot_id=sid, template=template, format=fmt,
            path=path, url=f"/files/exports/{name}",
        )

    def _zip_package(self, sid: str, snap: dict, report, village_name: str, ts: str) -> bytes:
        """给银行的材料包：报表 + 快照 + 验证说明。不含任何 L1 字段。"""
        buf = io.BytesIO()
        html = render_html(report, village_name=village_name, generated_at=ts)
        snap_export = SnapshotService(self.conn, self.cfg, self.keys, self.actor).export(sid)
        readme = _PACKAGE_README.format(
            snapshot_id=sid,
            period=snap["period"],
            signed_by=snap["signed_by"],
            merkle=snap["merkle_root"],
            public_key=(snap["snapshot"].get("public_key") or "")[:32],
            created_at=snap["created_at"],
        )
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(f"{sid}/报表-{report.template_name}.html", html.encode("utf-8"))
            z.writestr(
                f"{sid}/快照.json",
                json.dumps(snap_export, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            z.writestr(f"{sid}/验证说明.txt", readme.encode("utf-8"))
        return buf.getvalue()

    def list(self, sid: str | None = None) -> list[dict]:
        return repo.list_reports(self.conn, sid)


def _safe_event(e: dict) -> dict:
    """报表明细只保留 L2/L3 字段，备注与交易对手一律不出现在报表里。"""
    return {
        "id": e["id"],
        "date": e["date"],
        "type": e["type"],
        "category": e["category"],
        "amount_fen": e["amount_fen"],
        "qty": e.get("qty"),
        "unit": e.get("unit"),
        "evidence_id": e.get("evidence_id"),
        "estimated": bool(e.get("estimated")),
    }


_PACKAGE_README = """中小农户智能财务管理系统 · 银行核验材料包
============================================================

快照号：{snapshot_id}
记账期：{period} 年度
签章人：{signed_by}
出表时间：{created_at}
Merkle 根（SM3）：{merkle}
公钥（前 32 位）：{public_key}…

本包包含三样东西：
1. 报表.html            —— 可直接打印或另存为 PDF 的经营报表
2. 快照.json            —— 报表的"数字指纹"：事件集哈希、Merkle 根、签名、公钥
3. 本说明

核验三步（全部离线，不需要联网、不需要本系统在场）
------------------------------------------------------------
第 1 步 验签：
  用快照里的 public_key，对"去掉 signature 与 public_key 两个字段后的快照 JSON"
  做 SM2-with-SM3 验签。验签通过 = 这份快照确实由该村委的密钥签发、且未被改动。

第 2 步 验根：
  若需要复核数字，可由村委在本地重算事件集哈希与 Merkle 根，与本快照比对。
  本系统提供 tools/verify_snapshot.py，输入快照 JSON 即可输出两项结果。

第 3 步 抽验（可选，需农户单次授权）：
  指定某一笔（如"3 月化肥支出"），由村委在本地调出该笔原始凭证。
  只披露这一笔，其余账本仍不出村。

边界说明（请一并告知审核人员）
------------------------------------------------------------
本协议证明三件事：账本未被事后修改、报表忠实于账本、有人对报表负责。
它**不证明**农户没有造假账——防造假靠抽验原始票据与交叉核对，属于业务风控，
不是密码学能解决的。任何把"可验证"说成"防造假"的宣传都是错的。
"""
