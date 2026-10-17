"""确定性核算引擎。

设计约束（docs/技术方案V2.md §3 P1、§6.6）：
1. compute() 是纯函数：不读文件、不读时钟、不联网、不随机；
2. 入参只有事件集 + 地块 + 参数包 + 规则包，出参是完整可序列化的结果；
3. 全链路整数分，禁止 float；
4. 同一组入参任意次调用结果逐分相同，可被 snapshot.replay 校验。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Sequence

from . import money
from .models import (
    Event,
    Machinery,
    MachineryUse,
    Params,
    Plot,
    RulePack,
    ValidationError,
)

__all__ = ["compute", "sensitivity"]

_ZERO = Decimal("0")


def _active_events(events: Iterable[Event], rule: RulePack) -> list[Event]:
    out = []
    for ev in events:
        if ev.state == "voided":
            continue
        if rule.ignore_draft_events and ev.state == "draft":
            continue
        out.append(ev)
    return sorted(out, key=lambda e: (e.date, e.id))


def _source_area(plots: Sequence[Plot]) -> tuple[Decimal, Decimal, Decimal]:
    """返回 (总面积, 自有面积, 租入面积)。"""
    total = sum((p.area_mu for p in plots), _ZERO)
    owned = sum((p.area_mu for p in plots if p.tenure == "owned"), _ZERO)
    rented = total - owned
    return total, owned, rented


def _family_labor_cost(area: Decimal, params: Params, alpha: Decimal) -> tuple[int, dict[str, int]]:
    """自用工成本 = Σ(亩均工时 h_i × 面积 × 该环节日工价/8) × α。

    工时与工价全部来自参数包，报表须声明 α 取值（见 §6.3）。
    """
    detail: dict[str, int] = {}
    total = _ZERO
    for stage, hours in params.hours_per_mu.items():
        wage = params.day_wage.get(stage)
        if wage is None:
            raise ValidationError(f"参数包缺少环节 {stage!r} 的日工价，不能静默按 0 计")
        yuan_amount = area * hours * (Decimal(wage) / Decimal(8))
        fen_amount = money.fen(yuan_amount)
        detail[stage] = money.mul(fen_amount, alpha)
        total += Decimal(detail[stage])
    return int(total), detail


def _depreciation(
    machine: Machinery,
    use: MachineryUse,
    method: str,
) -> int:
    """单机折旧。工作量法按作业量摊，年数总和法按剩余年限摊，均受累计折旧封顶约束。"""
    depreciable = money.mul(machine.price_fen, (Decimal(1) - machine.residual_rate))
    remaining = depreciable - use.prior_depreciation_fen
    if remaining <= 0:
        return 0

    if method == "workload":
        if use.mu <= 0:
            raise ValidationError(
                f"农机 {machine.name}: 工作量法需要作业量记录（mu>0），"
                "缺失时应改用年数总和法或先补记录，不允许静默按 0 计提"
            )
        raw = money.mul(depreciable, use.mu / machine.design_total_mu)
    elif method == "sum_of_years":
        years = max(1, machine.life_years)
        year_index = max(1, int(getattr(use, "year_index", 1)))
        year_index = min(year_index, years)
        denom = Decimal(years * (years + 1) // 2)
        weight = Decimal(years - year_index + 1) / denom
        raw = money.mul(depreciable, weight)
    else:  # pragma: no cover - RulePack 已校验
        raise ValidationError(f"未知折旧方法 {method!r}")

    return max(0, min(raw, remaining))


def compute(
    events: Iterable[Event],
    plots: Sequence[Plot],
    params: Params,
    rule: RulePack | None = None,
    machinery_use: Sequence[MachineryUse] = (),
    price_factor: Decimal = Decimal("1"),
    yield_factor: Decimal = Decimal("1"),
    alpha: Decimal | None = None,
) -> dict:
    """核算一期经营结果。返回可直接序列化、可直接重放进比对的 dict。"""
    rule = rule or RulePack()
    alpha_used = params.shadow_wage_alpha if alpha is None else alpha
    if not (Decimal("0.5") <= alpha_used <= Decimal("1.0")):
        raise ValidationError("α 折算系数应在 0.5–1.0 之间，超出范围必有错，拒绝计算")

    active = _active_events(events, rule)
    total_area, owned_area, rented_area = _source_area(plots)

    revenue = 0
    subsidy = 0
    variable_cost = 0
    fixed_cost_paid = 0
    selling_expense = 0
    estimated_cost = 0
    sold_qty = _ZERO
    by_category: dict[str, int] = {}
    unclassified_income: dict[str, int] = {}
    counts = {"total": len(active), "income": 0, "cost": 0, "estimated": 0}
    vol_scale = Decimal(str(yield_factor))

    for ev in active:
        if ev.estimated:
            counts["estimated"] += 1
        if ev.type == "income":
            counts["income"] += 1
            if ev.category in rule.subsidy_categories:
                subsidy += ev.amount_fen
            elif ev.category in rule.revenue_categories:
                revenue += ev.amount_fen
                if ev.qty is not None:
                    sold_qty += ev.qty
            else:
                # 未归类的收入不静默丢弃：计入收入并单列，提示财务复核规则包
                revenue += ev.amount_fen
                unclassified_income[ev.category] = (
                    unclassified_income.get(ev.category, 0) + ev.amount_fen
                )
        elif ev.type in ("variable_cost", "fixed_cost"):
            counts["cost"] += 1
            amount = ev.amount_fen
            if ev.category in rule.volume_cost_categories:
                # 随产量变动的成本（机械作业、运输等）在单产情景下同步缩放
                amount = money.mul(amount, vol_scale)
            by_category[ev.category] = by_category.get(ev.category, 0) + amount
            if ev.category in rule.selling_expense_categories:
                selling_expense += amount
            if ev.type == "variable_cost":
                variable_cost += amount
            else:
                fixed_cost_paid += amount
            if ev.estimated:
                estimated_cost += amount
        # asset / liability 只影响资产负债表，不进入本期损益

    # 收入按情景系数缩放（仅用于敏感性分析，基准情形为 1）
    scale = Decimal(str(price_factor)) * Decimal(str(yield_factor))
    revenue_scaled = money.mul(revenue, scale)

    # 自有土地机会成本（租入地的实付租金应作为事件入账，避免重复计量）
    land_opportunity = 0
    for plot in plots:
        if plot.tenure != "owned":
            continue
        per_mu = params.land_rent_per_mu.get(plot.crop)
        if per_mu is None:
            raise ValidationError(f"参数包缺少作物 {plot.crop!r} 的流转租金基准")
        land_opportunity += money.fen(Decimal(per_mu) * plot.area_mu)

    dep_detail: dict[str, int] = {}
    depreciation = 0
    machines = {m.name: m for m in params.machinery}
    for use in machinery_use:
        machine = machines.get(use.name)
        if machine is None:
            raise ValidationError(f"折旧引用了参数包中不存在的农机 {use.name!r}")
        method = rule.depreciation_method if rule.depreciation_method else machine.method
        # 单机显式声明年数总和法时优先；否则按规则包的全局口径
        if machine.method == "sum_of_years":
            method = "sum_of_years"
        amount = _depreciation(machine, use, method)
        dep_detail[machine.name] = amount
        depreciation += amount

    family_labor = 0
    labor_detail: dict[str, int] = {}
    if rule.include_family_labor:
        if total_area <= 0:
            raise ValidationError("计算自用工成本需要地块面积（当前为 0）")
        family_labor, labor_detail = _family_labor_cost(total_area, params, alpha_used)

    cash_cost = variable_cost + fixed_cost_paid
    capital_cost = 0
    if rule.include_capital_cost:
        capital_cost = money.mul(
            money.mul(cash_cost, params.short_loan_rate),
            Decimal(params.capital_months) / Decimal(12),
        )

    fixed_derived = land_opportunity + depreciation
    imputed_cost = fixed_derived + family_labor + capital_cost
    total_cost = cash_cost + imputed_cost

    total_revenue = revenue_scaled + subsidy
    net_income = total_revenue - total_cost
    net_excl_subsidy = revenue_scaled - total_cost
    per_mu = money.div(net_income, total_area) if total_area > 0 else 0

    # 保本价：两个口径（地头 / 到场）
    breakeven = {"farm_gate": None, "delivered": None, "qty": float(sold_qty) if sold_qty else 0.0}
    if sold_qty > 0:
        delivered_cost = total_cost - subsidy
        breakeven["delivered"] = money.div(delivered_cost, sold_qty)
        breakeven["farm_gate"] = money.div(delivered_cost - selling_expense, sold_qty)

    # 盈亏平衡产量：q* = 固定成本 / (单价 - 单位变动成本)
    breakeven_qty = None
    if sold_qty > 0:
        unit_price = Decimal(revenue_scaled) / sold_qty
        unit_var = Decimal(variable_cost) / sold_qty
        margin = unit_price - unit_var
        if margin > 0:
            fixed_total = fixed_cost_paid + fixed_derived + family_labor + capital_cost
            breakeven_qty = float(Decimal(fixed_total) / margin)

    cost_structure: dict[str, dict] = {}
    for cat, amt in dict(by_category).items():
        cost_structure[cat] = {"amount_fen": amt, "share": float(money.ratio(amt, total_cost))}
    for cat, amt in (
        ("自有土地机会成本", land_opportunity),
        ("农机折旧", depreciation),
        ("自用工折算", family_labor),
        ("资金占用成本", capital_cost),
    ):
        if amt:
            cost_structure[cat] = {"amount_fen": amt, "share": float(money.ratio(amt, total_cost))}
    cost_structure = dict(
        sorted(cost_structure.items(), key=lambda kv: -kv[1]["amount_fen"])
    )

    return {
        "meta": {
            "rule_version": rule.version,
            "param_version": params.version,
            "region_code": params.region_code,
            "alpha": str(alpha_used),
            "price_factor": str(price_factor),
            "yield_factor": str(yield_factor),
            "include_family_labor": rule.include_family_labor,
            "include_capital_cost": rule.include_capital_cost,
            "depreciation_method": rule.depreciation_method,
            "events_total": counts["total"],
            "events_income": counts["income"],
            "events_cost": counts["cost"],
            "events_estimated": counts["estimated"],
            "estimated_cost_share": float(money.ratio(estimated_cost, cash_cost)) if cash_cost else 0.0,
        },
        "area": {
            "total_mu": str(total_area),
            "owned_mu": str(owned_area),
            "rented_mu": str(rented_area),
        },
        "revenue": {
            "operating_fen": revenue_scaled,
            "subsidy_fen": subsidy,
            "total_fen": total_revenue,
            "sold_qty": str(sold_qty),
            "unclassified": unclassified_income,
        },
        "cost": {
            "variable_fen": variable_cost,
            "fixed_paid_fen": fixed_cost_paid,
            "land_opportunity_fen": land_opportunity,
            "depreciation_fen": depreciation,
            "depreciation_detail": dep_detail,
            "family_labor_fen": family_labor,
            "family_labor_detail": labor_detail,
            "capital_fen": capital_cost,
            "selling_expense_fen": selling_expense,
            "cash_fen": cash_cost,
            "imputed_fen": imputed_cost,
            "total_fen": total_cost,
            "structure": cost_structure,
        },
        "result": {
            "net_income_fen": net_income,
            "net_income_excl_subsidy_fen": net_excl_subsidy,
            "per_mu_net_fen": per_mu,
        },
        "breakeven": breakeven,
        "breakeven_qty": breakeven_qty,
    }


def sensitivity(
    events: Iterable[Event],
    plots: Sequence[Plot],
    params: Params,
    rule: RulePack | None = None,
    machinery_use: Sequence[MachineryUse] = (),
    alpha_grid: Sequence[Decimal] = (Decimal("0.6"), Decimal("0.7"), Decimal("0.8")),
    price_grid: Sequence[Decimal] = (Decimal("0.8"), Decimal("1.0"), Decimal("1.2")),
    yield_grid: Sequence[Decimal] = (Decimal("0.8"), Decimal("1.0"), Decimal("1.2")),
) -> dict:
    """三因素情景分析，输出净收益区间与最敏感因素（见 §6.5）。

    只做单因素扫描 + 联合极值，不假装能算出概率分布。
    """
    base = compute(events, plots, params, rule, machinery_use)
    base_net = base["result"]["net_income_fen"]

    def _runs(grid: Sequence[Decimal], key: str) -> dict:
        values = []
        for v in grid:
            kwargs = {key: v}
            out = compute(events, plots, params, rule, machinery_use, **kwargs)
            values.append({"factor": str(v), "net_income_fen": out["result"]["net_income_fen"]})
        return {"samples": values, "swing_fen": max(v["net_income_fen"] for v in values) - min(v["net_income_fen"] for v in values)}

    scans = {
        "alpha": _runs(alpha_grid, "alpha"),
        "price": _runs(price_grid, "price_factor"),
        "yield": _runs(yield_grid, "yield_factor"),
    }
    most_sensitive = max(scans.items(), key=lambda kv: kv[1]["swing_fen"])[0]

    nets = []
    for a in alpha_grid:
        for p in price_grid:
            for y in yield_grid:
                out = compute(events, plots, params, rule, machinery_use, price_factor=p, yield_factor=y, alpha=a)
                nets.append(out["result"]["net_income_fen"])

    return {
        "base_net_income_fen": base_net,
        "worst_net_income_fen": min(nets),
        "best_net_income_fen": max(nets),
        "scans": scans,
        "most_sensitive_factor": most_sensitive,
        "grid": {"alpha": [str(a) for a in alpha_grid], "price": [str(p) for p in price_grid], "yield": [str(y) for y in yield_grid]},
    }
