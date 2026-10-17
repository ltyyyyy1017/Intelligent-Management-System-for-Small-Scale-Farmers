"""记账领域服务：校验、去重、状态机与审计。

三条硬规矩（来自 docs/技术方案V2.md §3、§5）：

1. **金额只能是整数分**，从界面到数据库到核算引擎全链路不出现 float；
2. **事件只增不改**：已确认的账不能原地改，只能"作废 + 重记"，否则审计链
   与快照重放都会失去意义；
3. **重复记账要拦**：同一农户同一天同一科目同一金额，大概率是手滑点了两次，
   默认拦下并告知已存在的那笔，允许显式强制。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.models import EVENT_TYPES

from . import repo
from .audit_svc import AuditService
from .clock import now_iso, today
from .db import transaction
from .errors import Conflict, NotFound, ValidationError
from .packs import classify_category, normalize_category

__all__ = ["LedgerService", "parse_amount_fen", "parse_qty", "month_summary"]

_MAX_FUTURE_DAYS = 31


def parse_amount_fen(text: Any) -> int:
    """把用户输入的元转成分。

    接受 "200"、"200.5"、"1,200.50"、"￥200"、"200元"；拒绝负数与空值。
    不做中文数字解析——那是"一句话记账"抽取器的事，记账落库只认数字。
    """
    if isinstance(text, bool):
        raise ValidationError("金额格式不正确")
    if isinstance(text, int):
        if text <= 0:
            raise ValidationError("金额不能为 0")
        return text * 100
    raw = str(text if text is not None else "").strip()
    for token in ("￥", "¥", "元", ",", "，", " "):
        raw = raw.replace(token, "")
    if not raw:
        raise ValidationError("金额不能为空")
    if raw.startswith("-"):
        raise ValidationError("金额不能为负（退款请用补贴/其他收入单列）")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValidationError(f"看不懂这个金额：{text!r}") from exc
    if value < 0:
        raise ValidationError("金额不能为负")
    if value > Decimal("100000000"):
        raise ValidationError("金额超过 1 亿元，请确认是否多打了一位")
    fen = int((value * 100).to_integral_value())
    if fen == 0:
        raise ValidationError("金额不能为 0")
    return fen


def parse_qty(text: Any, unit: str | None = None) -> Decimal | None:
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValidationError(f"数量格式不正确：{text!r}") from exc
    if value < 0:
        raise ValidationError("数量不能为负")
    if value > Decimal("10000000"):
        raise ValidationError("数量过大，请确认单位是否填错")
    if unit is None:
        # 有数量必须有单位：否则"50"到底是 50 公斤还是 50 袋，没人说得清
        raise ValidationError("填了数量就要选单位（公斤/亩/袋/小时）")
    return value


def _validate_date(text: str) -> str:
    try:
        d = date.fromisoformat(str(text))
    except (ValueError, TypeError) as exc:
        raise ValidationError("日期格式应为 2026-04-12") from exc
    if d.year < 2000:
        raise ValidationError("日期早于 2000 年，请检查")
    if d > today() + timedelta(days=_MAX_FUTURE_DAYS):
        raise ValidationError("日期不能晚于今天（补录历史账请选过去的日期）")
    return d.isoformat()


@dataclass
class LedgerService:
    conn: Any
    actor: str

    @property
    def audit(self) -> AuditService:
        return AuditService(self.conn)

    # ---------- 写入 ----------

    def record(
        self,
        *,
        farmer_id: int,
        date_text: str,
        type: str,
        category: str,
        amount_fen: int,
        qty: Any = None,
        unit: str | None = None,
        plot_id: int | None = None,
        note: str = "",
        source: str = "manual",
        confidence: float = 1.0,
        evidence_id: str | None = None,
        estimated: bool = False,
        state: str = "confirmed",
        force: bool = False,
    ) -> str:
        farmer = repo.get_farmer(self.conn, farmer_id)
        if not farmer:
            raise NotFound("找不到这位农户")
        if type not in EVENT_TYPES:
            raise ValidationError(f"未知的事件类型 {type!r}")
        if state not in ("draft", "confirmed"):
            raise ValidationError("新记的账只能是草稿或已确认")
        if source not in ("ocr", "asr", "manual", "import"):
            raise ValidationError(f"未知来源 {source!r}")

        date_iso = _validate_date(date_text)
        if not isinstance(amount_fen, int) or amount_fen <= 0:
            raise ValidationError("金额必须是大于 0 的整数分")
        if plot_id is not None:
            owned = {p["id"] for p in repo.list_plots(self.conn, farmer_id)}
            if plot_id not in owned:
                raise ValidationError("地块不属于这位农户")
        qty_value = parse_qty(qty, unit) if qty not in (None, "") else None
        conf = float(confidence)
        if not (0.0 <= conf <= 1.0):
            raise ValidationError("置信度必须在 0–1 之间")

        if (category or "").strip():
            category = normalize_category(category, type)
        else:
            # 没填科目时，从备注里尽力归一个（永远会得到一个目录内的科目）
            category = classify_category(note, type)[0]

        if not force:
            dup = self._find_duplicate(farmer_id, date_iso, type, category, amount_fen)
            if dup:
                raise Conflict(
                    f"这笔账好像已经记过了（{dup['id']}，{date_iso}，"
                    f"{category} {amount_fen / 100:.2f} 元）",
                    detail=f"duplicate:{dup['id']}",
                )

        ts = now_iso()
        with transaction(self.conn):
            eid = repo.insert_event(
                self.conn,
                {
                    "farmer_id": farmer_id,
                    "plot_id": plot_id,
                    "date": date_iso,
                    "type": type,
                    "category": category,
                    "amount_fen": amount_fen,
                    "qty": None if qty_value is None else f"{qty_value:f}",
                    "unit": unit,
                    "confidence": conf,
                    "evidence_id": evidence_id,
                    "source": source,
                    "state": state,
                    "estimated": estimated,
                    "note": note or None,
                    "created_by": self.actor,
                    "created_at": ts,
                },
            )
            self.audit.append(
                self.actor,
                "event.create",
                eid,
                {
                    "farmer": farmer["token"],
                    "date": date_iso,
                    "type": type,
                    "category": category,
                    "amount_fen": amount_fen,
                    "state": state,
                    "source": source,
                },
                ts=ts,
            )
        return eid

    def confirm(self, eid: str) -> dict:
        ev = self._get(eid)
        if ev["state"] == "confirmed":
            return ev
        if ev["state"] == "voided":
            raise Conflict("已作废的账不能再确认，请重新记一笔")
        ts = now_iso()
        with transaction(self.conn):
            repo.set_event_state(self.conn, eid, "confirmed", ts)
            self.audit.append(self.actor, "event.confirm", eid, {"amount_fen": ev["amount_fen"]}, ts=ts)
        return self._get(eid)

    def void(self, eid: str, reason: str = "") -> dict:
        ev = self._get(eid)
        if ev["state"] == "voided":
            return ev
        ts = now_iso()
        with transaction(self.conn):
            repo.set_event_state(self.conn, eid, "voided", ts)
            repo.update_event(self.conn, eid, {"note": (ev.get("note") or "") + f"｜作废：{reason}"[:200]}, ts)
            self.audit.append(
                self.actor, "event.void", eid,
                {"amount_fen": ev["amount_fen"], "reason": reason}, ts=ts,
            )
        return self._get(eid)

    def update_draft(self, eid: str, **fields: Any) -> dict:
        ev = self._get(eid)
        if ev["state"] != "draft":
            raise Conflict("只有草稿能修改；已确认的账请先作废再重记")
        clean: dict[str, Any] = {}
        if "date" in fields:
            clean["date"] = _validate_date(fields["date"])
        if "type" in fields:
            if fields["type"] not in EVENT_TYPES:
                raise ValidationError("未知的事件类型")
            clean["type"] = fields["type"]
        if "category" in fields:
            clean["category"] = (fields["category"] or "").strip()
        if "amount_fen" in fields:
            if not isinstance(fields["amount_fen"], int) or fields["amount_fen"] <= 0:
                raise ValidationError("金额必须是大于 0 的整数分")
            clean["amount_fen"] = fields["amount_fen"]
        if "qty" in fields or "unit" in fields:
            unit = fields.get("unit", ev.get("unit"))
            qty = fields.get("qty", ev.get("qty"))
            value = parse_qty(qty, unit) if qty not in (None, "") else None
            clean["qty"] = None if value is None else f"{value:f}"
            clean["unit"] = unit
        if "note" in fields:
            clean["note"] = fields["note"]
        if "plot_id" in fields:
            clean["plot_id"] = fields["plot_id"]
        ts = now_iso()
        with transaction(self.conn):
            repo.update_event(self.conn, eid, clean, ts)
            self.audit.append(self.actor, "event.update_draft", eid, {"fields": sorted(clean)}, ts=ts)
        return self._get(eid)

    # ---------- 查询 ----------

    def _get(self, eid: str) -> dict:
        ev = repo.get_event(self.conn, eid)
        if not ev:
            raise NotFound(f"找不到这笔账：{eid}")
        return ev

    def get(self, eid: str) -> dict:
        return self._get(eid)

    def list(
        self,
        farmer_id: int | None = None,
        *,
        month: str | None = None,
        state: str | None = None,
        limit: int | None = 200,
        include_voided: bool = False,
    ) -> list[dict]:
        date_from = date_to = None
        if month:
            year, mon = month.split("-")
            date_from = f"{year}-{mon}-01"
            nxt = (date(int(year), int(mon), 1) + timedelta(days=32)).replace(day=1)
            date_to = (nxt - timedelta(days=1)).isoformat()
        return repo.list_events(
            self.conn,
            farmer_id=farmer_id,
            state=state,
            date_from=date_from,
            date_to=date_to,
            include_voided=include_voided,
            limit=limit,
        )

    def drafts(self, farmer_id: int | None = None) -> list[dict]:
        return repo.list_events(self.conn, farmer_id=farmer_id, state="draft", limit=100)

    def _find_duplicate(
        self, farmer_id: int, date_iso: str, type: str, category: str, amount_fen: int
    ) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM event WHERE farmer_id = ? AND date = ? AND type = ? AND category = ? "
            "AND amount_fen = ? AND state != 'voided' LIMIT 1",
            (farmer_id, date_iso, type, category, amount_fen),
        ).fetchone()
        return dict(row) if row else None


def month_summary(conn, farmer_id: int, month: str) -> dict:
    """首页三行大字：这个月收了多少、花了多少、还剩多少。"""
    year, mon = month.split("-")
    start = f"{year}-{mon}-01"
    nxt = (date(int(year), int(mon), 1) + timedelta(days=32)).replace(day=1)
    end = (nxt - timedelta(days=1)).isoformat()
    rows = repo.list_events(conn, farmer_id=farmer_id, date_from=start, date_to=end, limit=None)
    income = sum(r["amount_fen"] for r in rows if r["type"] == "income")
    expense = sum(r["amount_fen"] for r in rows if r["type"] in ("variable_cost", "fixed_cost"))
    return {
        "month": month,
        "income_fen": income,
        "expense_fen": expense,
        "balance_fen": income - expense,
        "count": len(rows),
    }
