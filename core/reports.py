"""报表渲染：把核算结果变成人可读、可打印的文档。

这里输出 Markdown（便于在仓库里直接阅读与 diff）；生产实现走 HTML→PDF，
字段与本节一一对应，见 docs/技术方案V2.md §9。
"""
from __future__ import annotations

from . import money

__all__ = [
    "render_summary_md",
    "render_cost_structure_md",
    "render_sensitivity_md",
    "to_t1_rows",
    "render_t1_md",
]


def render_summary_md(result: dict, snapshot: dict | None = None) -> str:
    meta = result["meta"]
    rev = result["revenue"]
    cost = result["cost"]
    res = result["result"]
    be = result["breakeven"]
    area = result["area"]

    lines = [
        "## 一页式经营摘要",
        "",
        f"**净收益：{money.fmt_yuan(res['net_income_fen'])} 元**"
        f"（亩均 {money.fmt_yuan(res['per_mu_net_fen'])} 元／亩）",
        "",
        "| 项目 | 金额（元） | 说明 |",
        "|---|---:|---|",
        f"| 经营收入 | {money.fmt_yuan(rev['operating_fen'])} | 不含补贴 |",
        f"| 补贴收入 | {money.fmt_yuan(rev['subsidy_fen'])} | 单列，不计入经营收入 |",
        f"| 现金成本 | {money.fmt_yuan(cost['cash_fen'])} | 变动 + 实付固定 |",
        f"| 　其中：变动成本 | {money.fmt_yuan(cost['variable_fen'])} | 种子/化肥/农药/雇工等 |",
        f"| 　其中：实付固定成本 | {money.fmt_yuan(cost['fixed_paid_fen'])} | 租金/保险/管理费 |",
        f"| 推算成本 | {money.fmt_yuan(cost['imputed_fen'])} | 非现金支出，见下 |",
        f"| 　其中：自有土地机会成本 | {money.fmt_yuan(cost['land_opportunity_fen'])} | 按当地流转租金 |",
        f"| 　其中：农机折旧 | {money.fmt_yuan(cost['depreciation_fen'])} | 工作量法/年数总和法 |",
        f"| 　其中：自用工折算 | {money.fmt_yuan(cost['family_labor_fen'])} | α = {meta['alpha']} |",
        f"| **总成本** | **{money.fmt_yuan(cost['total_fen'])}** | 现金 + 推算 |",
        "",
        f"- 经营面积：{area['total_mu']} 亩（自有 {area['owned_mu']} 亩，租入 {area['rented_mu']} 亩）",
        f"- 销量：{rev['sold_qty']} kg",
        "",
    ]

    if be["farm_gate"] is not None:
        lines += [
            "### 卖多少钱不亏",
            "",
            "| 口径 | 保本价（元/单位） | 用途 |",
            "|---|---:|---|",
            f"| 地头保本价 | {money.fmt_yuan(be['farm_gate'])} | 粮贩上门收购时的谈判底线 |",
            f"| 到场保本价 | {money.fmt_yuan(be['delivered'])} | 自己拉到粮库/合作社时 |",
            "",
        ]
    if result.get("breakeven_qty"):
        lines += [
            f"- 盈亏平衡产量：{result['breakeven_qty']:.1f} kg"
            "（按当前售价与单位变动成本；高于此产量才不亏固定成本）",
            "",
        ]

    lines += [
        "### 报表脚注（固定打印）",
        "",
        f"- 规则包版本：{meta['rule_version']}；参数包版本：{meta['param_version']}"
        f"（{meta['region_code']}）",
        f"- 自用工折算系数 α = {meta['alpha']}（取值区间 0.6–0.8，系数越高成本越高）",
        f"- 估算项成本占比：{meta['estimated_cost_share'] * 100:.1f}%"
        f"（{meta['events_estimated']} / {meta['events_total']} 条事件为推算）",
        f"- 事件总数：{meta['events_total']}（收入 {meta['events_income']} / 成本 {meta['events_cost']}）",
    ]
    if snapshot:
        lines += [
            f"- 事件集哈希：`{snapshot['event_set_hash'][:16]}…`",
            f"- Merkle 根：`{snapshot['merkle_root'][:16]}…`",
            f"- 签章人：{snapshot['signed_by']}；签章载荷：`{snapshot['signature_payload_sha256'][:16]}…`",
        ]
    lines.append("")
    return "\n".join(lines)


def render_cost_structure_md(result: dict) -> str:
    structure = result["cost"]["structure"]
    total = result["cost"]["total_fen"]
    lines = [
        "## 成本结构",
        "",
        "| 科目 | 金额（元） | 占总成本 | 条形 |",
        "|---|---:|---:|---|",
    ]
    for cat, item in structure.items():
        bar = "█" * max(0, int(item["share"] * 40))
        lines.append(
            f"| {cat} | {money.fmt_yuan(item['amount_fen'])} | {item['share'] * 100:.1f}% | {bar} |"
        )
    lines.append(f"| **合计** | **{money.fmt_yuan(total)}** | 100.0% | |")
    lines.append("")
    return "\n".join(lines)


def render_sensitivity_md(sens: dict) -> str:
    lines = [
        "## 敏感性分析（三因素）",
        "",
        f"- 基准净收益：**{money.fmt_yuan(sens['base_net_income_fen'])} 元**",
        f"- 悲观情形：{money.fmt_yuan(sens['worst_net_income_fen'])} 元"
        f"　｜　乐观情形：{money.fmt_yuan(sens['best_net_income_fen'])} 元",
        f"- 影响最大的因素：**{ {'alpha': '自用工折算系数 α', 'price': '售价', 'yield': '单产'}[sens['most_sensitive_factor']] }**",
        "",
        "| 因素 | 取值 | 净收益（元） | 波动幅度（元） |",
        "|---|---:|---:|---:|",
    ]
    label = {"alpha": "α", "price": "售价系数", "yield": "单产系数"}
    for factor, scan in sens["scans"].items():
        for i, sample in enumerate(scan["samples"]):
            lines.append(
                f"| {label[factor] if i == 0 else ''} | {sample['factor']} | "
                f"{money.fmt_yuan(sample['net_income_fen'])} | "
                f"{money.fmt_yuan(scan['swing_fen']) if i == 0 else ''} |"
            )
    lines.append("")
    lines.append("> 说明：只做单因素扫描与联合极值，不输出概率分布。区间是给农户看的，不是给银行做决策的。")
    lines.append("")
    return "\n".join(lines)


def to_t1_rows(result: dict) -> list[tuple[str, str, str]]:
    """T1 信贷尽调版字段映射（节选）。返回 (字段, 值, 来源)。"""
    cost = result["cost"]
    rev = result["revenue"]
    return [
        ("经营耕地面积（亩）", result["area"]["total_mu"], "地块台账合计"),
        ("年度经营收入（元）", money.fmt_yuan(rev["operating_fen"]), "收入事件；不含补贴"),
        ("补贴收入（元）", money.fmt_yuan(rev["subsidy_fen"]), "补贴事件；单列"),
        ("物化成本（元）", money.fmt_yuan(cost["variable_fen"]), "变动成本事件"),
        ("机械作业成本（元）", money.fmt_yuan(cost["depreciation_fen"]), "自有农机折旧"),
        ("雇工与自用工（元）", money.fmt_yuan(cost["family_labor_fen"]), "自用工模型值，α 见脚注"),
        ("现金成本合计（元）", money.fmt_yuan(cost["cash_fen"]), "变动 + 实付固定"),
        ("总成本（元）", money.fmt_yuan(cost["total_fen"]), "现金 + 推算"),
        ("净收益（元）", money.fmt_yuan(result["result"]["net_income_fen"]), "引擎派生"),
        ("亩均净收益（元）", money.fmt_yuan(result["result"]["per_mu_net_fen"]), "引擎派生"),
    ]


def render_t1_md(result: dict, snapshot: dict | None = None) -> str:
    lines = [
        "## T1 信贷尽调版（字段映射节选）",
        "",
        "| 报表字段 | 值 | 来源 |",
        "|---|---:|---|",
    ]
    for name, value, source in to_t1_rows(result):
        lines.append(f"| {name} | {value} | {source} |")
    lines.append("")
    lines.append(
        "> 显性 / 隐性成本必须分列：银行看现金流（现金成本），农户看真实成本（总成本）。"
    )
    lines.append("")
    return "\n".join(lines)
