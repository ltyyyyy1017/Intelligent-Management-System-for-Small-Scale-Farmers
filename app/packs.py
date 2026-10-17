"""规则包、参数包的内置默认值与科目映射。

两条口径（docs/技术方案V2.md §6.1、§6.3）：

1. **科目体系是农户简易账**（收付实现 + 单季作物归集），不是企业会计科目。
   所以这里只有"收入 / 变动成本 / 固定成本 / 资产 / 负债"五类，二级科目用
   农户自己的说法（化肥、雇工、收割费），不要求他理解"制造费用"。
2. **参数全部可覆盖、可版本化**。默认值只是"能算出结果"的起点，报表会把
   实际用的版本与取值印在脚注里；农经站给的定额可以直接替换。
"""
from __future__ import annotations

from typing import Iterable

__all__ = [
    "DEFAULT_PARAM_PACK",
    "DEFAULT_RULE_PACK",
    "CATEGORY_CATALOG",
    "CATEGORY_ALIASES",
    "TYPE_LABELS",
    "categories_for",
    "classify_category",
    "normalize_category",
    "guess_type",
    "format_param_pack",
]

# 一级类型 → 中文标签（界面与报表统一用中文，代码里用英文枚举）
TYPE_LABELS = {
    "income": "收入",
    "variable_cost": "变动成本",
    "fixed_cost": "固定成本",
    "asset": "资产",
    "liability": "负债",
}

# 二级科目目录：顺序即界面上的推荐顺序
CATEGORY_CATALOG: dict[str, tuple[str, ...]] = {
    "income": ("主粮销售", "经济作物销售", "副业收入", "补贴", "其他收入"),
    "variable_cost": (
        "种子", "化肥", "农药", "农膜", "灌溉电费", "燃料", "机械作业费", "雇工",
        "运输", "装卸", "烘干", "销售费用", "其他变动成本",
    ),
    "fixed_cost": ("土地租金", "保险", "管理费", "设施折旧", "其他固定成本"),
    "asset": ("农机购置", "设施建设", "其他资产"),
    "liability": ("借款", "还款"),
}

# 口语 → 标准科目。农户说"尿素""复合肥"，系统都记到"化肥"下，
# 明细写在备注里——既让银行看到规范科目，又不丢农户的原话。
CATEGORY_ALIASES: dict[str, str] = {
    "复合肥": "化肥", "尿素": "化肥", "磷肥": "化肥", "钾肥": "化肥", "有机肥": "化肥",
    "碳铵": "化肥", "二铵": "化肥", "叶面肥": "化肥", "肥料": "化肥",
    "稻种": "种子", "谷种": "种子", "秧苗": "种子", "种苗": "种子", "种子种苗": "种子",
    "除草剂": "农药", "杀虫剂": "农药", "杀菌剂": "农药", "打药": "农药",
    "薄膜": "农膜", "地膜": "农膜",
    "电费": "灌溉电费", "抽水": "灌溉电费", "灌溉": "灌溉电费", "水费": "灌溉电费",
    "柴油": "燃料", "汽油": "燃料", "机油": "燃料",
    "收割费": "机械作业费", "耕地费": "机械作业费", "旋耕": "机械作业费",
    "插秧费": "机械作业费", "农机作业": "机械作业费", "机耕": "机械作业费",
    "人工": "雇工", "工资": "雇工", "请人": "雇工", "帮工": "雇工", "工钱": "雇工",
    "运费": "运输", "拉粮": "运输",
    "装卸费": "装卸", "搬运": "装卸",
    "烘干费": "烘干",
    "租金": "土地租金", "地租": "土地租金", "承包费": "土地租金",
    "保险": "保险", "农业保险": "保险",
    "补贴": "补贴", "补助": "补贴", "种粮补贴": "补贴", "农机补贴": "补贴",
    "卖粮": "主粮销售", "售粮": "主粮销售", "卖稻谷": "主粮销售", "卖谷": "主粮销售",
    "卖菜": "经济作物销售", "卖果": "经济作物销售", "卖水果": "经济作物销售",
    "打工": "副业收入", "副业": "副业收入",
    "买农机": "农机购置", "购机": "农机购置",
    "大棚": "设施建设", "建棚": "设施建设",
    "贷款": "借款", "借钱": "借款", "还贷": "还款",
}


def categories_for(event_type: str) -> tuple[str, ...]:
    return CATEGORY_CATALOG.get(event_type, ())


def classify_category(text: str, event_type: str | None = None) -> tuple[str, str]:
    """把农户的说法归到 (标准科目, 一级类型)。

    优先级：一级类型内精确匹配 → 全局别名 → 一级类型内包含匹配 → 兜底。
    不做"智能猜测"，宁可按"其他"归集并在界面提示复核，也不悄悄记错科目。
    """
    raw = (text or "").strip()
    if not raw:
        return ("其他收入" if event_type == "income" else "其他变动成本", event_type or "variable_cost")

    pools: Iterable[str] = categories_for(event_type) if event_type else (
        c for cs in CATEGORY_CATALOG.values() for c in cs
    )
    for name in pools:
        if raw == name:
            return name, _type_of(name)

    if raw in CATEGORY_ALIASES:
        name = CATEGORY_ALIASES[raw]
        return name, _type_of(name)

    for name in pools:
        if name in raw:
            return name, _type_of(name)
    for alias, name in CATEGORY_ALIASES.items():
        if alias in raw:
            return name, _type_of(name)

    fallback_type = event_type or "variable_cost"
    fallback = "其他收入" if fallback_type == "income" else (
        "其他固定成本" if fallback_type == "fixed_cost" else "其他变动成本"
    )
    return fallback, fallback_type


def _type_of(category: str) -> str:
    for etype, names in CATEGORY_CATALOG.items():
        if category in names:
            return etype
    return "variable_cost"


_ALL_CATEGORIES = frozenset(c for names in CATEGORY_CATALOG.values() for c in names)


def normalize_category(text: str, event_type: str | None = None) -> str:
    """归一科目：能对上目录就用标准科目，对不上原样返回（不硬塞"其他"）。

    返回原样很重要：农户写"给老王帮忙的工钱"这种条目，硬归成"其他变动成本"
    会丢掉他自己看得懂的信息。报表里它会计入"其他"，但账本上仍显示原话。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw in _ALL_CATEGORIES:
        return raw
    if raw in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[raw]
    for alias, name in CATEGORY_ALIASES.items():
        if alias and alias in raw:
            return name
    for name in _ALL_CATEGORIES:
        if name in raw:
            return name
    return raw


def guess_type(text: str) -> str:
    """按关键词猜一级类型，仅用于"一句话记账"的预填，界面必须可改。"""
    raw = text or ""
    for word in ("卖", "收入", "收到", "补贴", "补助", "售"):
        if word in raw:
            return "income"
    for word in ("买", "购", "付", "花", "支出", "交", "请人", "雇"):
        if word in raw:
            return "variable_cost"
    return "variable_cost"


# ---------- 默认参数包（附录 B 的完整版） ----------

DEFAULT_PARAM_PACK: dict = {
    "region_code": "4401xx",
    "version": "2026.1",
    "note": "内置默认值，仅用于演示与首次部署；进村前须由农经站/调研数据替换。",
    "day_wage": {
        "播种": 150, "插秧": 180, "施肥": 140, "打药": 160,
        "灌溉": 130, "收割": 170, "晾晒": 120,
    },
    "hours_per_mu": {
        "播种": "0.8", "施肥": "0.5", "打药": "0.6", "灌溉": "0.4", "收割": "1.2",
    },
    "shadow_wage_alpha": "0.70",
    "land_rent_per_mu": {
        "水稻": 800, "玉米": 600, "蔬菜": 1500, "果园": 1200, "旱地": 500, "水田": 800,
    },
    "machinery": [
        {
            "name": "旋耕机", "price_fen": 1200000, "residual_rate": "0.05",
            "design_total_mu": "8000", "life_years": 8, "method": "workload",
        },
        {
            "name": "联合收割机", "price_fen": 12000000, "residual_rate": "0.05",
            "design_total_mu": "3000", "life_years": 10, "method": "workload",
        },
        {
            "name": "植保无人机", "price_fen": 4000000, "residual_rate": "0.05",
            "design_total_mu": "15000", "life_years": 5, "method": "workload",
        },
    ],
    "short_loan_rate": "0.0435",
    "capital_months": 6,
    "yield_baseline": {
        "水稻": {"low": "450", "mid": "520", "high": "600"},
        "玉米": {"low": "400", "mid": "460", "high": "520"},
        "蔬菜": {"low": "2000", "mid": "2500", "high": "3000"},
    },
    "price_baseline": {
        "水稻": {"low": "2.2", "mid": "2.6", "high": "3.0"},
        "玉米": {"low": "1.8", "mid": "2.2", "high": "2.5"},
        "蔬菜": {"low": "3.0", "mid": "4.0", "high": "5.0"},
    },
}

# ---------- 默认规则包 ----------

DEFAULT_RULE_PACK: dict = {
    "version": "R1-2026.1",
    "effective_from": "2026-01-01",
    "note": "农户简易账口径：显性/隐性分列，补贴单列，自用工按 α 折算。",
    "revenue_categories": ["主粮销售", "经济作物销售", "副业收入", "其他收入"],
    "subsidy_categories": ["补贴"],
    "selling_expense_categories": ["运输", "装卸", "烘干", "销售费用"],
    "volume_cost_categories": ["机械作业费", "运输", "装卸", "烘干", "销售费用"],
    "include_family_labor": True,
    "include_capital_cost": False,
    "depreciation_method": "workload",
    "ignore_draft_events": True,
    # 界面与校验用：必填字段缺失即阻断（F7）
    "required_report_fields": [
        "area_total_mu", "operating_revenue_fen", "cash_cost_fen", "total_cost_fen",
        "net_income_fen",
    ],
}


def format_param_pack(pack: dict) -> str:
    """给界面看的一行摘要。"""
    return (
        f"区域 {pack.get('region_code', '?')} · 版本 {pack.get('version', '?')} · "
        f"α={pack.get('shadow_wage_alpha', '?')} · 农机 {len(pack.get('machinery', []))} 台"
    )
