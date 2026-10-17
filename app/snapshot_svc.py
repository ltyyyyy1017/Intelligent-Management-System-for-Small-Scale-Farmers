"""快照服务：出表 → 锁定输入 → 计算 → 摘要 → SM2 签章 → 落库。

对应 docs/技术方案V2.md §5.4、§8.5。

一个容易被忽略但决定方案成立与否的细节：**快照必须自带输入**。
只存"事件 id 列表"，一年后农户作废了其中一笔、或改了地块面积，重放就会
对不上——那时你无法区分"有人改了账"和"账本来就是这样，后来正常调整过"。
所以这里把 events / plots / machinery_use / params / rule 全部封进快照，
重放只依赖快照自身，不依赖数据库当前状态。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from core import snapshot as core_snapshot
from core.costing import compute
from core.models import Event, MachineryUse, Params, Plot

from . import repo
from .calc import load_context, pack_to_params, pack_to_rule
from .clock import now_iso
from .config import Config
from .db import next_counter, transaction
from .errors import DataIntegrityError, NotFound
from .ids import snapshot_id
from .signing import load_signing_key, public_key_hex, sign_digest_bytes, verify_signature

__all__ = ["SnapshotService", "serialize_inputs", "deserialize_inputs"]


# ---------- 输入序列化（快照自带输入，重放不依赖当前库） ----------

def _dump_event(e: Event) -> dict:
    return {
        "id": e.id,
        "farmer_token": e.farmer_token,
        "date": e.date.isoformat(),
        "type": e.type,
        "category": e.category,
        "amount_fen": e.amount_fen,
        "plot_id": e.plot_id,
        "qty": None if e.qty is None else f"{e.qty:f}",
        "unit": e.unit,
        "counterparty_token": e.counterparty_token,
        "confidence": e.confidence,
        "evidence_id": e.evidence_id,
        "source": e.source,
        "state": e.state,
        "estimated": bool(e.estimated),
    }


def _load_event(d: dict) -> Event:
    from datetime import date

    return Event(
        id=d["id"],
        farmer_token=d["farmer_token"],
        date=date.fromisoformat(d["date"]),
        type=d["type"],
        category=d["category"],
        amount_fen=int(d["amount_fen"]),
        plot_id=d.get("plot_id"),
        qty=None if d.get("qty") in (None, "") else Decimal(str(d["qty"])),
        unit=d.get("unit"),
        counterparty_token=d.get("counterparty_token"),
        confidence=float(d.get("confidence", 1.0)),
        evidence_id=d.get("evidence_id"),
        source=d.get("source", "manual"),
        state=d.get("state", "confirmed"),
        estimated=bool(d.get("estimated")),
    )


def _dump_plot(p: Plot) -> dict:
    return {
        "id": p.id,
        "farmer_token": p.farmer_token,
        "area_mu": f"{p.area_mu:f}",
        "tenure": p.tenure,
        "crop": p.crop,
        "rent_fen_per_mu": p.rent_fen_per_mu,
    }


def _load_plot(d: dict) -> Plot:
    return Plot(
        id=d["id"],
        farmer_token=d["farmer_token"],
        area_mu=Decimal(str(d["area_mu"])),
        tenure=d["tenure"],
        crop=d["crop"],
        rent_fen_per_mu=int(d.get("rent_fen_per_mu", 0)),
    )


def _dump_use(u: MachineryUse) -> dict:
    return {
        "name": u.name,
        "mu": f"{u.mu:f}",
        "prior_depreciation_fen": u.prior_depreciation_fen,
        "year_index": u.year_index,
    }


def _load_use(d: dict) -> MachineryUse:
    return MachineryUse(
        name=d["name"],
        mu=Decimal(str(d["mu"])),
        prior_depreciation_fen=int(d.get("prior_depreciation_fen", 0)),
        year_index=int(d.get("year_index", 1)),
    )


def serialize_inputs(ctx, events: list[Event]) -> dict:
    return {
        "events": [_dump_event(e) for e in events],
        "plots": [_dump_plot(p) for p in ctx.plots],
        "machinery_use": [_dump_use(u) for u in ctx.machinery_use],
        "params": ctx.params_raw,
        "rule": ctx.rule_raw,
    }


def deserialize_inputs(data: dict) -> dict:
    return {
        "events": [_load_event(d) for d in data.get("events", [])],
        "plots": [_load_plot(d) for d in data.get("plots", [])],
        "machinery_use": [_load_use(d) for d in data.get("machinery_use", [])],
        "params": pack_to_params(data.get("params", {})),
        "rule": pack_to_rule(data.get("rule", {})),
    }


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=lambda o: str(o))


@dataclass
class SnapshotService:
    conn: object
    cfg: Config
    keys: object
    actor: str

    # ---------- 生成 ----------

    def create(
        self, farmer_id: int, period: str, signed_by: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        ctx = load_context(self.conn, farmer_id, period)
        events = [e for e in ctx.events if e.state == "confirmed"]
        if not events:
            from .errors import ValidationError

            raise ValidationError(f"{period} 年没有已确认的账，先记账再出报表")

        result = compute(events, ctx.plots, ctx.params, ctx.rule, ctx.machinery_use)
        ts = created_at or now_iso()
        snap = core_snapshot.make_snapshot(
            result,
            events,
            signed_by=signed_by or self.actor,
            period=period,
            created_at=ts,
            device_id=self.keys.device_id,
        )
        priv = load_signing_key(self.cfg, self.keys)
        signature = sign_digest_bytes(priv, core_snapshot.signing_bytes(snap))
        signed = core_snapshot.attach_signature(snap, signature, public_key_hex(priv))

        with transaction(self.conn):
            seq = next_counter(self.conn, f"snapshot:{period}")
            sid = snapshot_id(period, seq)
            repo.insert_snapshot(
                self.conn,
                {
                    "id": sid,
                    "period": period,
                    "farmer_id": farmer_id,
                    "event_ids_json": _json_dumps([e.id for e in events]),
                    "inputs_json": _json_dumps(serialize_inputs(ctx, events)),
                    "event_set_hash": snap["event_set_hash"],
                    "merkle_root": snap["merkle_root"],
                    "rule_version": snap["rule_version"],
                    "param_version": snap["param_version"],
                    "output_json": _json_dumps(result),
                    "snapshot_json": _json_dumps(signed),
                    "signature": signature,
                    "public_key": signed["public_key"],
                    "signed_by": snap["signed_by"],
                    "device_id": self.keys.device_id,
                    "created_at": ts,
                },
            )
            self._audit().append(
                self.actor,
                "snapshot.create",
                sid,
                {
                    "farmer": ctx.farmer_token,
                    "period": period,
                    "merkle_root": snap["merkle_root"],
                    "event_count": len(events),
                },
                ts=ts,
            )
        return self.get(sid)

    # ---------- 查询 ----------

    def get(self, sid: str) -> dict:
        row = repo.get_snapshot(self.conn, sid)
        if not row:
            raise NotFound(f"找不到快照 {sid}")
        row["snapshot"] = json.loads(row["snapshot_json"])
        row["output"] = json.loads(row["output_json"])
        return row

    def list(self, farmer_id: int | None = None) -> list[dict]:
        return repo.list_snapshots(self.conn, farmer_id)

    def _audit(self):
        from .audit_svc import AuditService

        return AuditService(self.conn)

    # ---------- 重放与验签 ----------

    def replay(self, sid: str) -> dict:
        """只用快照自带的输入重算，与快照里的 output 比对。"""
        row = self.get(sid)
        inputs = deserialize_inputs(json.loads(row["inputs_json"]))
        recomputed = compute(
            inputs["events"], inputs["plots"], inputs["params"], inputs["rule"],
            inputs["machinery_use"],
        )
        stored = row["output"]
        checks = {
            "event_set_hash": core_snapshot.event_set_hash(inputs["events"]) == row["event_set_hash"],
            "merkle_root": core_snapshot.merkle_root(
                core_snapshot.merkle_leaves(inputs["events"])
            ) == row["merkle_root"],
            "output": recomputed == stored,
        }
        diff = []
        if not checks["output"]:
            for key in sorted(set(stored) | set(recomputed)):
                if stored.get(key) != recomputed.get(key):
                    diff.append(key)
        return {"ok": all(checks.values()), "checks": checks, "diff": diff}

    def verify(self, sid: str) -> dict:
        """银行侧三步验证的第 1、2 步（离线可跑）。"""
        row = self.get(sid)
        snap = row["snapshot"]
        payload = core_snapshot.signing_bytes(snap)
        sig_ok = verify_signature(
            str(snap.get("public_key") or row.get("public_key") or ""),
            payload,
            str(snap.get("signature") or row.get("signature") or ""),
        )
        replay = self.replay(sid)
        audit = self._audit().verify()
        return {
            "ok": bool(sig_ok and replay["ok"]),
            "snapshot_id": sid,
            "signature": {
                "ok": sig_ok,
                "signed_by": row["signed_by"],
                "device_id": row.get("device_id"),
                "public_key": snap.get("public_key"),
                "algorithm": "SM2-with-SM3",
            },
            "replay": replay,
            "audit_chain": audit,
            "merkle_root": row["merkle_root"],
            "event_count": len(json.loads(row["event_ids_json"])),
            "created_at": row["created_at"],
        }

    def export(self, sid: str) -> dict:
        """导出给银行的最小充分材料：快照 + 报表摘要，不含任何 L1 字段。"""
        row = self.get(sid)
        snap = row["snapshot"]
        return {
            "format": "nfm-snapshot-v1",
            "snapshot": snap,
            "verification_hint": {
                "algorithm": "SM2-with-SM3",
                "steps": ["用 public_key 验证 signature 覆盖 snapshot（签名时不含 signature/public_key 两个字段）",
                          "对事件集重算 event_set_hash 与 merkle_root",
                          "必要时经农户授权抽验单张原始凭证"],
            },
        }


def load_snapshot_file(path) -> dict:
    """读取快照文件（验证工具用）。"""
    try:
        return json.loads(open(path, "r", encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataIntegrityError("快照文件损坏或不是 JSON") from exc
