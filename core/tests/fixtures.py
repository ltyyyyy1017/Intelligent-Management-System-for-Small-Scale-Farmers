"""测试用的固定账本：10 亩水稻（自有 8 亩 + 租入 2 亩），一个完整年度。

数值刻意贴近日标区县的真实量级，便于对照"这套账算出来是否说得通"。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.models import (
    Event,
    Machinery,
    MachineryUse,
    Params,
    Plot,
    RulePack,
)

FARMER = "F-ABCDEF2345"


def plots() -> list[Plot]:
    return [
        Plot(id="P1", farmer_token=FARMER, area_mu=Decimal("8"), tenure="owned", crop="水稻"),
        Plot(id="P2", farmer_token=FARMER, area_mu=Decimal("2"), tenure="rented", crop="水稻",
             rent_fen_per_mu=0),  # 租金以事件入账，避免与实付租金重复计量
    ]


def params() -> Params:
    return Params(
        region_code="4401xx",
        version="2026.1",
        day_wage={"播种": 150, "施肥": 140, "打药": 160, "灌溉": 130, "收割": 170},
        hours_per_mu={
            "播种": Decimal("0.8"),
            "施肥": Decimal("0.5"),
            "打药": Decimal("0.6"),
            "灌溉": Decimal("0.4"),
            "收割": Decimal("1.2"),
        },
        shadow_wage_alpha=Decimal("0.70"),
        land_rent_per_mu={"水稻": 800},
        machinery=(
            Machinery(name="旋耕机", price_fen=1_200_000, residual_rate=Decimal("0.05"),
                      design_total_mu=Decimal("8000"), life_years=8),
        ),
        short_loan_rate=Decimal("0.0435"),
        capital_months=6,
    )


def events() -> list[Event]:
    d = date(2026, 4, 12)
    return [
        Event(id="E-000001", farmer_token=FARMER, date=d, type="income",
              category="主粮销售", amount_fen=1_352_000, qty=Decimal("5200"), unit="kg",
              source="manual"),
        Event(id="E-000002", farmer_token=FARMER, date=date(2026, 9, 20), type="income",
              category="补贴", amount_fen=30_000, source="manual"),
        Event(id="E-000003", farmer_token=FARMER, date=d, type="variable_cost",
              category="化肥", amount_fen=200_000, qty=Decimal("500"), unit="kg", source="ocr",
              confidence=0.91, evidence_id="EV-1"),
        Event(id="E-000004", farmer_token=FARMER, date=d, type="variable_cost",
              category="种子", amount_fen=80_000, source="ocr", confidence=0.88),
        Event(id="E-000005", farmer_token=FARMER, date=date(2026, 5, 3), type="variable_cost",
              category="农药", amount_fen=60_000, source="manual"),
        Event(id="E-000006", farmer_token=FARMER, date=date(2026, 7, 18), type="variable_cost",
              category="机械作业费", amount_fen=150_000, source="manual"),
        Event(id="E-000007", farmer_token=FARMER, date=date(2026, 8, 2), type="variable_cost",
              category="运输", amount_fen=40_000, source="manual"),
        Event(id="E-000008", farmer_token=FARMER, date=date(2026, 1, 10), type="fixed_cost",
              category="土地租金", amount_fen=160_000, source="manual"),
        Event(id="E-000009", farmer_token=FARMER, date=date(2026, 6, 1), type="variable_cost",
              category="灌溉电费", amount_fen=25_000, source="manual", state="draft"),
    ]


def machinery_use() -> list[MachineryUse]:
    return [MachineryUse(name="旋耕机", mu=Decimal("12"), prior_depreciation_fen=0)]


def rule() -> RulePack:
    return RulePack()
