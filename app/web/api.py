"""JSON 接口（给未来的移动端 / 桌面壳 / 自动化测试用）。

页面本身不依赖这些接口——服务端渲染保证"没有 JS 也能记账"。接口存在的意义是
把同一套领域服务暴露给程序调用，避免以后为了做个手机端再写一遍业务逻辑。
"""
from __future__ import annotations

from .. import repo
from ..calc import calculate, load_context
from ..clock import today
from ..extract import extract
from ..ledger import LedgerService, parse_amount_fen
from ..snapshot_svc import SnapshotService
from .server import Request, Response

__all__ = ["register"]


def register(app) -> None:
    @app.get("/api/health", auth=False)
    def health(req: Request):
        village = repo.get_village(req.conn)
        return {
            "ok": True,
            "version": "2.0.0",
            "village": village.get("name", ""),
            "region": village.get("region_code", ""),
            "device": req.app.keys.device_id,
        }

    @app.get("/api/farmers")
    def farmers(req: Request):
        return [
            {"id": f["id"], "token": f["token"], "group": f.get("group_name") or ""}
            for f in repo.list_farmers(req.conn)
        ]

    @app.post("/api/extract")
    def api_extract(req: Request):
        data = req.json()
        text = str(data.get("text") or "").strip()
        if not text:
            return Response.json({"error": "text 不能为空"}, status=422)
        return extract(text, today()).as_dict()

    @app.post("/api/events")
    def api_create_event(req: Request):
        data = req.json()
        svc = LedgerService(req.conn, req.user["display_name"])
        eid = svc.record(
            farmer_id=int(data["farmer_id"]),
            date_text=str(data.get("date") or today().isoformat()),
            type=str(data.get("type") or "variable_cost"),
            category=str(data.get("category") or ""),
            amount_fen=parse_amount_fen(data.get("amount", data.get("amount_fen", 0))),
            qty=data.get("qty"),
            unit=data.get("unit"),
            plot_id=data.get("plot_id"),
            note=str(data.get("note") or ""),
            source=str(data.get("source") or "manual"),
            state=str(data.get("state") or "confirmed"),
            force=bool(data.get("force")),
        )
        return {"id": eid}

    @app.post("/api/events/{eid}/confirm")
    def api_confirm(req: Request, eid: str):
        return {"event": LedgerService(req.conn, req.user["display_name"]).confirm(eid)}

    @app.post("/api/events/{eid}/void")
    def api_void(req: Request, eid: str):
        return {"event": LedgerService(req.conn, req.user["display_name"]).void(eid, "API 作废")}

    @app.get("/api/summary")
    def api_summary(req: Request):
        farmer_id = int(req.q("farmer") or 0)
        period = req.q("period") or str(today().year)
        ctx = load_context(req.conn, farmer_id, period)
        result = calculate(ctx)
        return {
            "period": period,
            "result": result["result"],
            "revenue": result["revenue"],
            "cost": result["cost"],
            "breakeven": result["breakeven"],
            "meta": result["meta"],
        }

    @app.post("/api/snapshots")
    def api_snapshot(req: Request):
        data = req.json()
        snap = SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).create(
            int(data["farmer_id"]), str(data.get("period") or today().year)
        )
        return {"id": snap["id"], "merkle_root": snap["merkle_root"], "signature": snap["signature"]}

    @app.get("/api/snapshots/{sid}/verify")
    def api_verify(req: Request, sid: str):
        return SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).verify(sid)

    @app.get("/api/audit/verify")
    def api_audit(req: Request):
        from ..audit_svc import AuditService

        return AuditService(req.conn).verify()
