"""受控外发：脱敏 → 复检 → 逐次确认 → 打包 → 审计（§4.2c、§8.2）。

这是"隐私不出村"与"信用可验证"之间唯一的出口，所以每一步都要留下证据：

- 打包内容只从白名单字段渲染（L1 没有通往模板的路径）；
- 打包后把 zip 里每个成员**解压后再扫一遍**——压缩字节扫描是自欺欺人；
- 用户必须显式勾选"我同意"，且同意记录进审计链；
- 外发记录保存收件方、用途、字段清单，事后可追。
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

from . import repo
from .clock import now_iso
from .config import Config
from .db import transaction
from .errors import DataIntegrityError, UserError
from .privacy import L1, L2, L3, FIELD_LEVELS, l1_findings, mask_text
from .report_svc import ReportService
from .snapshot_svc import SnapshotService

__all__ = ["OutboundService", "OutboundPreview"]

#: 外发字段白名单：只允许 L2/L3。新增字段必须先在这里登记级别。
ALLOWED_FIELDS = {
    "farmer_token": L2,
    "period": L3,
    "area_total_mu": L3,
    "operating_revenue": L3,
    "subsidy_revenue": L3,
    "cash_cost": L3,
    "total_cost": L3,
    "net_income": L3,
    "per_mu_net": L3,
    "breakeven_farm_gate": L3,
    "breakeven_delivered": L3,
    "cost_structure": L3,
    "merkle_root": L3,
    "signature": L3,
    "public_key": L3,
    "rule_version": L3,
    "param_version": L3,
}


@dataclass
class OutboundPreview:
    snapshot_id: str
    fields: list[dict]
    l1_hits: list[str]
    ok: bool
    note: str


class OutboundService:
    def __init__(self, conn, cfg: Config, keys, actor: str) -> None:
        self.conn = conn
        self.cfg = cfg
        self.keys = keys
        self.actor = actor

    def _known_names(self) -> list[str]:
        names = []
        for farmer in repo.list_farmers(self.conn):
            try:
                names.append(self.keys.unseal_text(farmer["name_enc"], aad=b"farmer-name"))
            except Exception:  # noqa: BLE001
                continue
        return names

    def preview(self, sid: str) -> OutboundPreview:
        snap = SnapshotService(self.conn, self.cfg, self.keys, self.actor).get(sid)
        farmer = repo.get_farmer(self.conn, int(snap["farmer_id"]))
        out = snap["output"]
        fields = [
            {"name": "经营主体代币", "key": "farmer_token", "level": L2, "value": farmer["token"]},
            {"name": "记账期", "key": "period", "level": L3, "value": snap["period"]},
            {"name": "经营面积（亩）", "key": "area_total_mu", "level": L3, "value": out["area"]["total_mu"]},
            {"name": "经营收入（元）", "key": "operating_revenue", "level": L3,
             "value": f"{out['revenue']['operating_fen'] / 100:.2f}"},
            {"name": "补贴收入（元）", "key": "subsidy_revenue", "level": L3,
             "value": f"{out['revenue']['subsidy_fen'] / 100:.2f}"},
            {"name": "现金成本（元）", "key": "cash_cost", "level": L3,
             "value": f"{out['cost']['cash_fen'] / 100:.2f}"},
            {"name": "总成本（元）", "key": "total_cost", "level": L3,
             "value": f"{out['cost']['total_fen'] / 100:.2f}"},
            {"name": "净收益（元）", "key": "net_income", "level": L3,
             "value": f"{out['result']['net_income_fen'] / 100:.2f}"},
            {"name": "Merkle 根", "key": "merkle_root", "level": L3, "value": snap["merkle_root"]},
        ]
        # 校验：任何不在白名单或级别不是 L2/L3 的字段都不允许出现
        for f in fields:
            if f["key"] not in ALLOWED_FIELDS or ALLOWED_FIELDS[f["key"]] == L1:
                raise DataIntegrityError(f"字段 {f['key']} 不在外发白名单内")
        return OutboundPreview(
            snapshot_id=sid,
            fields=fields,
            l1_hits=[],
            ok=True,
            note="以上字段之外的数据不会进入材料包；L1 直接标识在数据模型里就没有外发路径。",
        )

    def build(
        self, sid: str, recipient: str, purpose: str, consent: bool, dry_run: bool = False
    ) -> dict:
        if not consent:
            raise UserError("必须勾选同意才能生成外发材料")
        if not recipient.strip():
            raise UserError("请填写接收方（例如：XX 农商行 XX 支行）")
        if not purpose.strip():
            raise UserError("请填写用途（例如：春耕贷款申请）")

        preview = self.preview(sid)
        result = ReportService(self.conn, self.cfg, self.keys, self.actor).export(sid, "T1-v1", "zip")
        raw = result.path.read_bytes()

        # 复检：解压后逐成员扫描 L1，压缩字节扫描没有意义
        hits: list[str] = []
        names = self._known_names()
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for member in z.namelist():
                if member.endswith("/"):
                    continue
                content = z.read(member).decode("utf-8", "replace")
                for finding in l1_findings(content, names):
                    hits.append(f"{member}: {finding.kind}")

        decision = "blocked" if hits else ("cancelled" if dry_run else "approved")
        with transaction(self.conn):
            repo.insert_outbound(
                self.conn,
                {
                    "created_at": now_iso(),
                    "actor": self.actor,
                    "purpose": purpose,
                    "recipient": recipient,
                    "snapshot_id": sid,
                    "fields": [f["key"] for f in preview.fields],
                    "rel_path": result.path.name,
                    "decision": decision,
                    "note": "; ".join(hits)[:300] if hits else "",
                },
            )
            from .audit_svc import AuditService

            entry = AuditService(self.conn).append(
                self.actor,
                "outbound.build" if not hits else "outbound.blocked",
                sid,
                {
                    "recipient": recipient,
                    "purpose": purpose,
                    "fields": [f["key"] for f in preview.fields],
                    "l1_hits": hits[:5],
                    "allowed": not hits,
                },
            )
            self.conn.execute(
                "UPDATE outbound SET audit_seq = ? WHERE id = (SELECT MAX(id) FROM outbound)",
                (entry["seq"],),
            )

        if hits:
            raise DataIntegrityError(
                "外发内容里仍有 L1 直接标识，已阻止发送", detail="; ".join(hits[:3])
            )
        return {"ok": True, "path": str(result.path), "url": result.url,
                "fields": preview.fields, "audit_seq": entry["seq"]}

    def history(self, limit: int = 100) -> list[dict]:
        rows = repo.list_outbound(self.conn, limit)
        for row in rows:
            row["fields"] = json.loads(row.get("fields_json") or "[]")
        return rows

    def masked_farmer(self, farmer: dict) -> dict:
        """界面上展示"我们本地存了什么"，但默认脱敏。"""
        try:
            name = self.keys.unseal_text(farmer["name_enc"], aad=b"farmer-name")
        except Exception:  # noqa: BLE001
            name = "（无法解密）"
        phone = ""
        if farmer.get("phone_enc"):
            try:
                phone = self.keys.unseal_text(farmer["phone_enc"], aad=b"farmer-phone")
            except Exception:  # noqa: BLE001
                phone = ""
        return {
            "token": farmer["token"],
            "name_masked": mask_text(name),
            "phone_masked": mask_text(phone) if phone else "",
            "group": farmer.get("group_name") or "",
        }
