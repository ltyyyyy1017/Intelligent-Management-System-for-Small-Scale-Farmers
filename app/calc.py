"""数据库 → 核算内核的适配层。

`core.costing.compute` 是纯函数，不认识 SQLite；这一层只做三件事：
取数、转成内核的数据类、把结果连同"用了哪版规则/参数"一起返回。
每次计算都重新读库，不做缓存——本系统的账本规模下，重算比"缓存失效"便宜得多。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.costing import compute, sensitivity
from core.models import Event, MachineryUse, Params, Plot, RulePack

from . import repo
from .clock import parse_date
from .errors import NotFound, ValidationError
from .packs import DEFAULT_PARAM_PACK, DEFAULT_RULE_PACK

__all__ = ["CalcContext", "load_context", "calculate", "seed_packs", "param_cli_override"]

_ZERO = Decimal("0")


@dataclass
class CalcContext:
    farmer: dict
    events: list[Event]
    plots: list[Plot]
    params: Params
    rule: RulePack
    machinery_use: list[MachineryUse]
    params_raw: dict
    rule_raw: dict

    @property
    def farmer_token(self) -> str:
        return str(self.farmer["token"])


def pack_to_params(raw: dict) -> Params:
    return Params.from_dict(raw)


def pack_to_rule(raw: dict) -> RulePack:
    return RulePack(
        version=str(raw.get("version", "R1-2026.1")),
        revenue_categories=tuple(raw.get("revenue_categories", ())),
        subsidy_categories=tuple(raw.get("subsidy_categories", ("补贴",))),
        selling_expense_categories=tuple(raw.get("selling_expense_categories", ())),
        volume_cost_categories=tuple(raw.get("volume_cost_categories", ())),
        include_family_labor=bool(raw.get("include_family_labor", True)),
        include_capital_cost=bool(raw.get("include_capital_cost", False)),
        depreciation_method=str(raw.get("depreciation_method", "workload")),
        ignore_draft_events=bool(raw.get("ignore_draft_events", True)),
    )


def _event_from_row(row: dict, farmer_token: str) -> Event:
    qty = row.get("qty")
    return Event(
        id=str(row["id"]),
        farmer_token=farmer_token,
        date=parse_date(str(row["date"])),
        type=str(row["type"]),
        category=str(row["category"]),
        amount_fen=int(row["amount_fen"]),
        plot_id=None if row.get("plot_id") is None else str(row["plot_id"]),
        qty=None if qty in (None, "") else Decimal(str(qty)),
        unit=row.get("unit"),
        counterparty_token=row.get("counterparty_token"),
        confidence=float(row.get("confidence") or 1.0),
        evidence_id=row.get("evidence_id"),
        source=str(row.get("source") or "manual"),
        state=str(row.get("state") or "confirmed"),
        estimated=bool(row.get("estimated")),
    )


def load_context(
    conn,
    farmer_id: int,
    period: str,
    params_version: str | None = None,
    rule_version: str | None = None,
) -> CalcContext:
    farmer = repo.get_farmer(conn, farmer_id)
    if not farmer:
        raise NotFound("找不到这位农户", detail=f"farmer_id={farmer_id}")
    village = repo.get_village(conn)

    region = (village or {}).get("region_code") or DEFAULT_PARAM_PACK["region_code"]
    pver = params_version or (village or {}).get("param_version") or DEFAULT_PARAM_PACK["version"]
    rver = rule_version or (village or {}).get("rule_version") or DEFAULT_RULE_PACK["version"]

    param_row = repo.get_param_pack(conn, region, pver)
    rule_row = repo.get_rule_pack(conn, rver)
    params_raw = param_row.get("data") if param_row else dict(DEFAULT_PARAM_PACK)
    rule_raw = rule_row.get("data") if rule_row else dict(DEFAULT_RULE_PACK)
    if not params_raw:
        raise ValidationError(f"找不到参数包 {region}/{pver}，请先在管理页导入")
    if not rule_raw:
        raise ValidationError(f"找不到规则包 {rver}，请先在管理页导入")

    rows = repo.list_events(
        conn, farmer_id=farmer_id, date_from=f"{period}-01-01", date_to=f"{period}-12-31"
    )
    # 作废事件不入账；草稿不参与出表（规则包里也可显式关闭该行为）
    active_rows = [r for r in rows if r["state"] != "voided"]
    events = [_event_from_row(r, farmer["token"]) for r in active_rows]

    plots = [
        Plot(
            id=str(p["id"]),
            farmer_token=farmer["token"],
            area_mu=Decimal(str(p["area_mu"])),
            tenure=str(p["tenure"]),
            crop=str(p["crop"]),
            rent_fen_per_mu=int(p["rent_fen_per_mu"] or 0),
        )
        for p in repo.list_plots(conn, farmer_id)
    ]

    use = [
        MachineryUse(
            name=str(u["name"]),
            mu=Decimal(str(u["mu"])),
            prior_depreciation_fen=int(u["prior_depreciation_fen"] or 0),
            year_index=int(u["year_index"] or 1),
        )
        for u in repo.list_machinery_use(conn, farmer_id, period)
    ]

    return CalcContext(
        farmer=farmer,
        events=events,
        plots=plots,
        params=pack_to_params(params_raw),
        rule=pack_to_rule(rule_raw),
        machinery_use=use,
        params_raw=params_raw,
        rule_raw=rule_raw,
    )


def calculate(ctx: CalcContext, **kwargs) -> dict:
    """跑一遍核算。参数（α、售价系数等）可临时覆盖，用于情景分析。"""
    return compute(ctx.events, ctx.plots, ctx.params, ctx.rule, ctx.machinery_use, **kwargs)


def calculate_sensitivity(ctx: CalcContext) -> dict:
    return sensitivity(ctx.events, ctx.plots, ctx.params, ctx.rule, ctx.machinery_use)


def seed_packs(conn, created_at: str) -> None:
    """首次建库时写入内置参数包与规则包（幂等）。"""
    repo.save_param_pack(
        conn,
        DEFAULT_PARAM_PACK["version"],
        DEFAULT_PARAM_PACK["region_code"],
        DEFAULT_PARAM_PACK,
        created_at,
        note="内置默认参数包",
    )
    repo.save_rule_pack(
        conn,
        DEFAULT_RULE_PACK["version"],
        DEFAULT_RULE_PACK,
        DEFAULT_RULE_PACK["effective_from"],
        created_at,
        note="内置默认规则包",
    )


def param_cli_override(params: Params, **kwargs) -> Params:
    """给 CLI 场景覆盖少量参数用（不可变 dataclass → 造新实例）。"""
    from dataclasses import replace

    return replace(params, **kwargs)
