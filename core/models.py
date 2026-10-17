"""数据模型：事件、地块、区域参数包、规则包。

对应 docs/技术方案V2.md §5.2 的表结构，这里只实现核算链路需要的最小字段集，
数据库持久化层不在此文件内（参考实现聚焦「可复算」）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

__all__ = [
    "ValidationError",
    "EVENT_TYPES",
    "Event",
    "Plot",
    "Machinery",
    "MachineryUse",
    "Params",
    "RulePack",
]

EVENT_TYPES = ("income", "variable_cost", "fixed_cost", "asset", "liability")


class ValidationError(ValueError):
    """数据校验失败。宁可当场报错，也不要静默算出错账。"""


@dataclass(frozen=True)
class Event:
    """一笔经营事件（系统的最小事实单元）。"""

    id: str
    farmer_token: str
    date: date
    type: str
    category: str
    amount_fen: int
    plot_id: str | None = None
    qty: Decimal | None = None
    unit: str | None = None
    counterparty_token: str | None = None
    confidence: float = 1.0
    evidence_id: str | None = None
    source: str = "manual"
    state: str = "confirmed"
    estimated: bool = False

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValidationError(f"事件 {self.id}: 未知类型 {self.type!r}")
        if not isinstance(self.amount_fen, int):
            raise ValidationError(f"事件 {self.id}: amount_fen 必须是整数分")
        if self.amount_fen < 0:
            raise ValidationError(f"事件 {self.id}: 金额不能为负（红冲用 type/state 表达）")
        if self.qty is not None and not isinstance(self.qty, Decimal):
            raise ValidationError(f"事件 {self.id}: qty 必须是 Decimal（定点）")
        if self.state not in ("draft", "confirmed", "voided"):
            raise ValidationError(f"事件 {self.id}: 未知状态 {self.state!r}")
        if self.source not in ("ocr", "asr", "manual", "import"):
            raise ValidationError(f"事件 {self.id}: 未知来源 {self.source!r}")

    def canonical(self) -> str:
        """规范化序列化，用于事件集哈希。字段顺序固定、无空格、金额为整数。"""
        qty = "" if self.qty is None else f"{self.qty:f}"
        return "|".join(
            [
                self.id,
                self.farmer_token,
                self.date.isoformat(),
                self.type,
                self.category,
                str(self.amount_fen),
                self.plot_id or "",
                qty,
                self.unit or "",
                self.counterparty_token or "",
                self.source,
                "1" if self.estimated else "0",
            ]
        )


@dataclass(frozen=True)
class Plot:
    """地块。自有地按当地流转租金计机会成本，租入地按实付租金。"""

    id: str
    farmer_token: str
    area_mu: Decimal
    tenure: str = "owned"  # owned | rented
    crop: str = "水稻"
    rent_fen_per_mu: int = 0

    def __post_init__(self) -> None:
        if self.tenure not in ("owned", "rented"):
            raise ValidationError(f"地块 {self.id}: tenure 必须是 owned/rented")
        if self.area_mu <= 0:
            raise ValidationError(f"地块 {self.id}: 面积必须为正")
        if not isinstance(self.area_mu, Decimal):
            raise ValidationError(f"地块 {self.id}: area_mu 必须是 Decimal")


@dataclass(frozen=True)
class Machinery:
    """自有农机，用于工作量法/年数总和法折旧。"""

    name: str
    price_fen: int
    residual_rate: Decimal = Decimal("0.05")
    design_total_mu: Decimal = Decimal("8000")
    life_years: int = 10
    method: str = "workload"  # workload | sum_of_years

    def __post_init__(self) -> None:
        if not (0 <= self.residual_rate < 1):
            raise ValidationError(f"农机 {self.name}: 残值率必须在 [0,1)")
        if self.method not in ("workload", "sum_of_years"):
            raise ValidationError(f"农机 {self.name}: 未知折旧方法 {self.method}")


@dataclass(frozen=True)
class MachineryUse:
    """某台农机在本核算期的使用量。

    prior_depreciation_fen 用于累计折旧封顶；year_index 供年数总和法定位年份。
    """

    name: str
    mu: Decimal = Decimal("0")
    prior_depreciation_fen: int = 0
    year_index: int = 1


@dataclass(frozen=True)
class Params:
    """区域参数包（对应 param_pack.yaml）。数值均为可被覆盖的假设，报表须声明取值。"""

    region_code: str = "4401xx"
    version: str = "2026.1"
    day_wage: dict[str, int] = field(default_factory=dict)          # 环节 -> 元/工日
    hours_per_mu: dict[str, Decimal] = field(default_factory=dict)  # 环节 -> 工时/亩
    shadow_wage_alpha: Decimal = Decimal("0.70")
    land_rent_per_mu: dict[str, int] = field(default_factory=dict)  # 作物/地类 -> 元/亩·年
    machinery: tuple[Machinery, ...] = ()
    short_loan_rate: Decimal = Decimal("0.0435")
    capital_months: int = 6
    yield_baseline: dict[str, dict[str, Decimal]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Params":
        mach = tuple(
            Machinery(
                name=m["name"],
                price_fen=m["price_fen"],
                residual_rate=Decimal(str(m.get("residual_rate", "0.05"))),
                design_total_mu=Decimal(str(m.get("design_total_mu", "8000"))),
                life_years=int(m.get("life_years", 10)),
                method=m.get("method", "workload"),
            )
            for m in data.get("machinery", [])
        )
        return cls(
            region_code=data.get("region_code", "4401xx"),
            version=str(data.get("version", "2026.1")),
            day_wage={k: int(v) for k, v in data.get("day_wage", {}).items()},
            hours_per_mu={k: Decimal(str(v)) for k, v in data.get("hours_per_mu", {}).items()},
            shadow_wage_alpha=Decimal(str(data.get("shadow_wage_alpha", "0.70"))),
            land_rent_per_mu={k: int(v) for k, v in data.get("land_rent_per_mu", {}).items()},
            machinery=mach,
            short_loan_rate=Decimal(str(data.get("short_loan_rate", "0.0435"))),
            capital_months=int(data.get("capital_months", 6)),
            yield_baseline=data.get("yield_baseline", {}),
        )


@dataclass(frozen=True)
class RulePack:
    """核算规则包：决定「哪些科目归入收入/成本」「是否计隐性成本」。

    规则与模型分离、与代码分离的目的：规则可被财务专业成员独立审阅与版本化，
    模型升级不影响历史报表口径。
    """

    version: str = "R1-2026.1"
    revenue_categories: tuple[str, ...] = ("主粮销售", "经济作物销售", "副业收入")
    subsidy_categories: tuple[str, ...] = ("补贴",)
    selling_expense_categories: tuple[str, ...] = ("运输", "装卸", "烘干", "销售费用")
    # 随产量变动的成本科目：单产情景分析时同步缩放，否则"单产"与"售价"会得到
    # 完全相同的敏感性结果，那是在骗自己。
    volume_cost_categories: tuple[str, ...] = ("机械作业费", "运输", "装卸", "烘干", "销售费用")
    include_family_labor: bool = True
    include_capital_cost: bool = False
    depreciation_method: str = "workload"  # 覆盖单机设置
    ignore_draft_events: bool = True

    def __post_init__(self) -> None:
        if self.depreciation_method not in ("workload", "sum_of_years"):
            raise ValidationError(f"规则包 {self.version}: 未知折旧方法")
