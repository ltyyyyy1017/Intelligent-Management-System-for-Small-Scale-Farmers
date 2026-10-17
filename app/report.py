"""报表：T1 信贷尽调 / T2 财政补贴 / T3 经营主体备案，以及农户一页摘要。

对应 docs/技术方案V2.md §9。四条必须守住的东西：

1. **显性 / 隐性成本分列，不合并。** 银行看现金流，农户看真实成本，合并会同时误导两边。
2. **每个数字可溯源。** 行里带"来源"，脚注带规则包/参数包版本、α、估算项占比、快照哈希。
3. **模板版本化。** 改版不覆盖历史报表；这里用 `T1-v1` 这样的 id。
4. **L1 字段进不来。** 报表只从核算结果与 token 渲染，姓名/身份证/银行卡/电话在
   数据模型里就没有通往模板的路径（§8.1）。

本模块只产出"报表数据"，渲染 HTML / XLSX 由 `render_html` / `render_xlsx` 完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from core import money

from . import xlsx

__all__ = [
    "Row",
    "Section",
    "Report",
    "build_report",
    "render_html",
    "render_xlsx",
    "farmer_summary",
    "TEMPLATE_INFO",
    "TYPE_LABEL",
]

TYPE_LABEL = {
    "income": "收入",
    "variable_cost": "变动成本",
    "fixed_cost": "固定成本",
    "asset": "资产",
    "liability": "负债",
}

TEMPLATE_INFO = {
    "T1-v1": {
        "name": "T1 信贷尽调版",
        "audience": "银行 / 担保 / 农担",
        "purpose": "经营真实性与还款能力",
    },
    "T2-v1": {
        "name": "T2 财政补贴版",
        "audience": "农业农村局 / 财政",
        "purpose": "补贴资格与投入佐证",
    },
    "T3-v1": {
        "name": "T3 经营主体备案版",
        "audience": "农经站 / 市场监管",
        "purpose": "年报口径的经营情况",
    },
}


@dataclass
class Row:
    label: str
    value: str
    source: str = ""
    numeric: float | int | None = None
    fmt: str = "text"  # text | money | number | percent
    emphasis: bool = False


@dataclass
class Section:
    title: str
    rows: list[Row] = field(default_factory=list)
    note: str = ""
    kind: str = "table"  # table | note


@dataclass
class Report:
    template_id: str
    title: str
    subtitle: str
    period: str
    farmer_token: str
    sections: list[Section]
    events: list[dict]
    footnotes: list[str]
    snapshot: dict
    result: dict
    snapshot_id: str = ""

    @property
    def template_name(self) -> str:
        return TEMPLATE_INFO.get(self.template_id, {}).get("name", self.template_id)


# ---------- 小工具 ----------

def _yuan(fen: int | None) -> str:
    return money.fmt_yuan(int(fen or 0))


def _cat(structure: dict, *names: str) -> int:
    total = 0
    for name in names:
        item = structure.get(name)
        if item:
            total += int(item["amount_fen"])
    return total


def _cat_rows(structure: dict, skip: tuple[str, ...] = ()) -> list[Row]:
    rows = []
    for name, item in structure.items():
        if name in skip:
            continue
        rows.append(
            Row(
                label=name,
                value=_yuan(item["amount_fen"]),
                source=f"占总成本 {item['share'] * 100:.1f}%",
                numeric=item["amount_fen"] / 100,
                fmt="money",
            )
        )
    return rows


# ---------- 构建 ----------

def build_report(
    template_id: str,
    result: dict,
    snapshot: dict,
    *,
    period: str,
    farmer_token: str,
    farmer_group: str = "",
    plots: list[dict] | None = None,
    events: list[dict] | None = None,
    village_name: str = "",
    snapshot_id: str = "",
) -> Report:
    if template_id not in TEMPLATE_INFO:
        raise ValueError(f"未知报表模板 {template_id!r}")
    plots = plots or []
    events = events or []
    meta = result["meta"]
    footnotes = _footnotes(result, snapshot, meta)

    if template_id.startswith("T1"):
        sections = _t1_sections(result, plots, period)
    elif template_id.startswith("T2"):
        sections = _t2_sections(result, plots, events)
    else:
        sections = _t3_sections(result, plots, meta)

    return Report(
        template_id=template_id,
        title=TEMPLATE_INFO[template_id]["name"],
        subtitle=(
            f"{TEMPLATE_INFO[template_id]['audience']} · "
            f"{TEMPLATE_INFO[template_id]['purpose']}"
        ),
        period=period,
        farmer_token=farmer_token,
        sections=sections,
        events=events,
        footnotes=footnotes,
        snapshot=snapshot,
        result=result,
        snapshot_id=snapshot_id,
    )


def _t1_sections(result: dict, plots: list[dict], period: str) -> list[Section]:
    area, rev, cost, res, be = result["area"], result["revenue"], result["cost"], result["result"], result["breakeven"]
    structure = cost["structure"]
    crops = "、".join(sorted({p.get("crop", "") for p in plots if p.get("crop")})) or "—"

    material = _cat(structure, "种子", "化肥", "农药", "农膜", "灌溉电费", "燃料")
    machine = _cat(structure, "机械作业费") + int(cost["depreciation_fen"])
    labor_paid = _cat(structure, "雇工")
    land_paid = _cat(structure, "土地租金")

    sections = [
        Section("一、经营基本情况", [
            Row("经营耕地面积（亩）", area["total_mu"], "地块台账合计", float(area["total_mu"]), "number"),
            Row("　其中：自有（亩）", area["owned_mu"], "地块 tenure=owned", float(area["owned_mu"]), "number"),
            Row("　其中：租入（亩）", area["rented_mu"], "地块 tenure=rented", float(area["rented_mu"]), "number"),
            Row("种植作物", crops, "地块台账"),
            Row("记账期", f"{period} 年度", "快照期"),
        ]),
        Section("二、收入", [
            Row("年度经营收入（元）", _yuan(rev["operating_fen"]), "收入事件；不含补贴",
                rev["operating_fen"] / 100, "money", emphasis=True),
            Row("补贴收入（元）", _yuan(rev["subsidy_fen"]), "补贴事件；单列", rev["subsidy_fen"] / 100, "money"),
            Row("销售数量", f"{float(rev['sold_qty']):,.0f} 公斤", "销售事件数量合计"),
        ]),
        Section("三、成本（显性 / 隐性分列）", [
            Row("物化成本（元）", _yuan(material), "种子+化肥+农药+农膜+灌溉电费+燃料", material / 100, "money"),
            Row("机械作业成本（元）", _yuan(machine), "外包作业费 + 自有农机折旧", machine / 100, "money"),
            Row("雇工成本（元）", _yuan(labor_paid), "雇工事件（现金支出）", labor_paid / 100, "money"),
            Row("土地租金（元）", _yuan(land_paid), "租金事件（现金支出）", land_paid / 100, "money"),
            Row("**现金成本合计（元）**", _yuan(cost["cash_fen"]), "变动 + 实付固定",
                cost["cash_fen"] / 100, "money", emphasis=True),
            Row("自有土地机会成本（元）", _yuan(cost["land_opportunity_fen"]),
                "面积 × 当地流转租金", cost["land_opportunity_fen"] / 100, "money"),
            Row("农机折旧（元）", _yuan(cost["depreciation_fen"]),
                "工作量法 / 年数总和法，含累计封顶", cost["depreciation_fen"] / 100, "money"),
            Row("自用工折算（元）", _yuan(cost["family_labor_fen"]),
                f"按 α={result['meta']['alpha']} 折算，非现金支出", cost["family_labor_fen"] / 100, "money"),
            Row("**总成本（元）**", _yuan(cost["total_fen"]), "现金 + 推算",
                cost["total_fen"] / 100, "money", emphasis=True),
        ]),
        Section("四、经营成果", [
            Row("净收益（元）", _yuan(res["net_income_fen"]), "收入 − 总成本",
                res["net_income_fen"] / 100, "money", emphasis=True),
            Row("亩均净收益（元/亩）", _yuan(res["per_mu_net_fen"]), "净收益 ÷ 面积",
                res["per_mu_net_fen"] / 100, "money"),
        ]),
    ]
    if be.get("farm_gate") is not None:
        sections.append(Section("五、保本价", [
            Row("地头保本价（元/公斤）", _yuan(be["farm_gate"]), "不含运输与销售费用；粮贩上门时用",
                be["farm_gate"] / 100, "money"),
            Row("到场保本价（元/公斤）", _yuan(be["delivered"]), "含运输/装卸/烘干/销售费用",
                be["delivered"] / 100, "money"),
        ]))
    sections.append(Section("六、成本结构明细", _cat_rows(structure)))
    return sections


def _t2_sections(result: dict, plots: list[dict], events: list[dict]) -> list[Section]:
    area, cost, rev = result["area"], result["cost"], result["revenue"]
    structure = cost["structure"]
    sections = [
        Section("一、申报面积", [
            Row("申报面积（亩）", area["total_mu"], "地块台账合计", float(area["total_mu"]), "number"),
            Row("自有面积（亩）", area["owned_mu"], "地块 tenure=owned", float(area["owned_mu"]), "number"),
            Row("租入面积（亩）", area["rented_mu"], "地块 tenure=rented", float(area["rented_mu"]), "number"),
        ]),
        Section("二、地块清单", [
            Row(
                f"{p.get('name') or '地块'}（{p.get('crop', '')}）",
                f"{p.get('area_mu')} 亩 · {'自有' if p.get('tenure') == 'owned' else '租入'}",
                "地块台账",
                float(p.get("area_mu") or 0),
                "number",
            )
            for p in plots
        ] or [Row("—", "未登记地块", "请先在农户档案里登记地块")]),
        Section("三、投入明细（按科目）", _cat_rows(structure)),
        Section("四、产出与凭证", [
            Row("经营收入（元）", _yuan(rev["operating_fen"]), "收入事件", rev["operating_fen"] / 100, "money"),
            Row("补贴收入（元）", _yuan(rev["subsidy_fen"]), "补贴事件", rev["subsidy_fen"] / 100, "money"),
            Row("凭证张数", str(len({e.get("evidence_id") for e in events if e.get("evidence_id")})),
                "已归档原始凭证数"),
            Row("记账笔数", str(len(events)), "本期已确认事件数"),
        ]),
    ]
    return sections


def _t3_sections(result: dict, plots: list[dict], meta: dict) -> list[Section]:
    area, cost, rev, res = result["area"], result["cost"], result["revenue"], result["result"]
    structure = cost["structure"]
    machinery = sum(
        int(v["amount_fen"]) for k, v in structure.items() if k == "农机折旧"
    )
    return [
        Section("一、主体经营信息", [
            Row("经营面积（亩）", area["total_mu"], "地块台账", float(area["total_mu"]), "number"),
            Row("作物", "、".join(sorted({p.get("crop", "") for p in plots if p.get("crop")})) or "—", "地块台账"),
            Row("全年记账笔数", str(meta["events_total"]), "事件表"),
        ]),
        Section("二、经营收支", [
            Row("经营收入（元）", _yuan(rev["operating_fen"]), "收入事件", rev["operating_fen"] / 100, "money"),
            Row("补贴收入（元）", _yuan(rev["subsidy_fen"]), "补贴事件", rev["subsidy_fen"] / 100, "money"),
            Row("总成本（元）", _yuan(cost["total_fen"]), "现金 + 推算", cost["total_fen"] / 100, "money"),
            Row("盈余（元）", _yuan(res["net_income_fen"]), "收入 − 总成本",
                res["net_income_fen"] / 100, "money", emphasis=True),
        ]),
        Section("三、成本构成", _cat_rows(structure)),
        Section("四、资产（农机折旧口径）", [
            Row("农机折旧计入额（元）", _yuan(machinery), "工作量法/年数总和法", machinery / 100, "money"),
        ]),
    ]


def _footnotes(result: dict, snapshot: dict, meta: dict) -> list[str]:
    notes = [
        f"规则包版本：{meta['rule_version']}；参数包版本：{meta['param_version']}"
        f"（区域 {meta['region_code']}）",
        f"自用工折算系数 α = {meta['alpha']}（区间 0.6–0.8；系数越高成本越高）",
        f"估算项成本占比：{meta['estimated_cost_share'] * 100:.1f}%"
        f"（{meta['events_estimated']} / {meta['events_total']} 条事件为推算）",
        f"事件总数：{meta['events_total']}（收入 {meta['events_income']} / 成本 {meta['events_cost']}）",
        f"事件集哈希（SM3）：{snapshot.get('event_set_hash', '')[:32]}",
        f"Merkle 根（SM3）：{snapshot.get('merkle_root', '')[:32]}",
        f"签章人：{snapshot.get('signed_by', '')}；设备：{snapshot.get('device_id', '')}",
        "本报表不含身份证号、银行卡号、手机号、真实姓名等 L1 直接标识；"
        "农户以代币标识，必要时由村委在本地核对。",
        "金额以元为单位、分位四舍五入显示；核算过程为整数分运算，不产生浮点误差。",
    ]
    return notes


# ---------- 农户一页摘要 ----------

def farmer_summary(result: dict, sensitivity: dict | None = None) -> dict:
    """给农户看的三个数，全部用大白话，不出现"成本""净收益"这类术语。"""
    res, cost, be = result["result"], result["cost"], result["breakeven"]
    net = int(res["net_income_fen"])
    summary = {
        "net_income_fen": net,
        "headline": "今年赚了" if net >= 0 else "今年亏了",
        "net_text": money.fmt_yuan(abs(net)),
        "per_mu_text": money.fmt_yuan(abs(int(res["per_mu_net_fen"]))),
        "cash_cost_text": money.fmt_yuan(int(cost["cash_fen"])),
        "real_cost_text": money.fmt_yuan(int(cost["total_fen"])),
        "breakeven_farm_gate": None if be.get("farm_gate") is None else money.fmt_yuan(int(be["farm_gate"])),
        "breakeven_delivered": None if be.get("delivered") is None else money.fmt_yuan(int(be["delivered"])),
        "cost_structure": [
            {"name": k, "amount_text": money.fmt_yuan(int(v["amount_fen"])), "share": v["share"]}
            for k, v in list(cost["structure"].items())[:8]
        ],
    }
    if sensitivity:
        summary["range_text"] = (
            f"{money.fmt_yuan(int(sensitivity['worst_net_income_fen']))} ~ "
            f"{money.fmt_yuan(int(sensitivity['best_net_income_fen']))} 元"
        )
        summary["most_sensitive"] = {
            "alpha": "自己出工的折算系数",
            "price": "卖价",
            "yield": "产量",
        }.get(sensitivity["most_sensitive_factor"], sensitivity["most_sensitive_factor"])
    return summary


# ---------- 渲染：HTML（A4 打印） ----------

_CSS = """
:root { --ink:#1a1a1a; --muted:#666; --line:#cfcfcf; --soft:#f4f4f4; }
* { box-sizing: border-box; }
body { margin:0; padding:24px; color:var(--ink); background:#fff;
  font-family: "Microsoft YaHei","PingFang SC","Noto Sans CJK SC",sans-serif; font-size:14px; line-height:1.6; }
.page { max-width: 820px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--muted); margin-bottom: 18px; }
.meta { display:flex; flex-wrap:wrap; gap: 8px 28px; border-top:2px solid var(--ink);
  border-bottom:1px solid var(--line); padding:10px 0; margin-bottom:18px; font-size:13px; }
.meta b { font-weight:600; }
h2 { font-size:16px; margin: 22px 0 8px; padding-left:8px; border-left:4px solid var(--ink); }
table { width:100%; border-collapse: collapse; margin-bottom:6px; }
th, td { border:1px solid var(--line); padding:7px 9px; vertical-align:top; }
th { background: var(--soft); text-align:left; font-weight:600; }
td.num { text-align:right; font-variant-numeric: tabular-nums; white-space:nowrap; }
tr.em td { font-weight:700; background:#fafafa; }
td.src { color: var(--muted); font-size:12px; }
.note { color: var(--muted); font-size:12px; margin: 4px 0 0; }
.foot { margin-top: 26px; border-top:1px solid var(--line); padding-top:10px; font-size:11.5px; color:#444; }
.foot li { margin: 2px 0; }
.hash { font-family: ui-monospace, Consolas, monospace; word-break: break-all; }
.sign { margin-top: 26px; display:flex; justify-content:space-between; font-size:13px; }
.sign div { width: 45%; border-top:1px solid var(--ink); padding-top:6px; }
.toolbar { position: fixed; top: 12px; right: 12px; }
.toolbar button { font-size: 14px; padding: 8px 14px; cursor:pointer; }
@media print {
  body { padding: 0; font-size: 12px; }
  .toolbar { display:none; }
  .page { max-width: none; }
  h2 { page-break-after: avoid; }
  table { page-break-inside: auto; }
  tr { page-break-inside: avoid; }
}
"""


def render_html(report: Report, *, village_name: str = "", generated_at: str = "",
                auto_print: bool = False) -> str:
    rows_html: list[str] = []
    for section in report.sections:
        rows_html.append(f"<h2>{escape(section.title)}</h2>")
        rows_html.append("<table><thead><tr><th style='width:34%'>项目</th>"
                         "<th style='width:22%'>数值</th><th>来源 / 说明</th></tr></thead><tbody>")
        for row in section.rows:
            cls = " class='em'" if row.emphasis else ""
            num = " class='num'" if row.fmt in ("money", "number", "percent") else ""
            rows_html.append(
                f"<tr{cls}><td>{escape(row.label)}</td><td{num}>{escape(row.value)}</td>"
                f"<td class='src'>{escape(row.source)}</td></tr>"
            )
        rows_html.append("</tbody></table>")
        if section.note:
            rows_html.append(f"<p class='note'>{escape(section.note)}</p>")

    detail_html = ""
    if report.events:
        detail_html = ["<h2>附：记账明细</h2>",
                       "<table><thead><tr><th>日期</th><th>类型</th><th>科目</th>"
                       "<th>金额（元）</th><th>数量</th><th>凭证</th></tr></thead><tbody>"]
        for e in report.events:
            qty = "" if e.get("qty") in (None, "") else f"{e['qty']} {e.get('unit') or ''}"
            detail_html.append(
                f"<tr><td>{escape(str(e.get('date', '')))}</td>"
                f"<td>{escape(TYPE_LABEL.get(e.get('type', ''), e.get('type', '')))}</td>"
                f"<td>{escape(str(e.get('category', '')))}</td>"
                f"<td class='num'>{_yuan(int(e.get('amount_fen') or 0))}</td>"
                f"<td>{escape(qty)}</td><td>{escape(str(e.get('evidence_id') or '—'))}</td></tr>"
            )
        detail_html.append("</tbody></table>")
        detail_html = "".join(detail_html)

    foot = "".join(f"<li>{escape(n)}</li>" for n in report.footnotes)
    snap = report.snapshot
    title = escape(report.title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{escape(village_name)} {title} {escape(report.period)}</title>
<style>{_CSS}</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">打印 / 另存为 PDF</button></div>
<div class="page">
  <h1>{title}</h1>
  <div class="sub">{escape(report.subtitle)}</div>
  <div class="meta">
    <span><b>经营主体：</b>{escape(report.farmer_token)}</span>
    <span><b>村：</b>{escape(village_name or '—')}</span>
    <span><b>记账期：</b>{escape(report.period)} 年度</span>
    <span><b>出表时间：</b>{escape(str(snap.get('created_at') or generated_at))}</span>
    <span><b>快照号：</b>{escape(report.snapshot_id or '—')}</span>
  </div>
  {''.join(rows_html)}
  {detail_html}
  <div class="foot">
    <b>报表脚注（固定打印）</b>
    <ul>{foot}</ul>
    <p class="hash">公钥：{escape(str(snap.get('public_key', ''))[:64])}…</p>
    <p class="hash">签名：{escape(str(snap.get('signature', ''))[:64])}…</p>
  </div>
  <div class="sign">
    <div>村委经办人（签章）：{escape(str(snap.get('signed_by', '')))}</div>
    <div>设备标识：{escape(str(snap.get('device_id', '')))}</div>
  </div>
</div>
{'<script>window.addEventListener("load",function(){setTimeout(function(){window.print();},300);});</script>' if auto_print else ''}
</body></html>"""


# ---------- 渲染：XLSX ----------

def render_xlsx(report: Report, *, village_name: str = "") -> bytes:
    wb = xlsx.Workbook()
    ws = wb.add_sheet("报表")
    ws.width(30, 18, 42)
    ws.append([f"{village_name} · {report.title}"], style=4)
    ws.append([report.subtitle], style=5)
    ws.append([f"经营主体：{report.farmer_token}", f"记账期：{report.period} 年度",
               f"快照：{report.snapshot.get('merkle_root', '')[:16]}"], style=5)
    ws.append([])
    for section in report.sections:
        ws.append([section.title], style=1)
        ws.append(["项目", "数值", "来源 / 说明"], style=3)
        for row in section.rows:
            if row.fmt == "money" and row.numeric is not None:
                ws.append([row.label, row.numeric, row.source], style=2)
            elif row.fmt == "number" and row.numeric is not None:
                ws.append([row.label, row.numeric, row.source])
            else:
                ws.append([row.label, row.value, row.source])
        ws.append([])
    ws.append(["报表脚注"], style=1)
    for note in report.footnotes:
        ws.append([note], style=5)

    if report.events:
        ws2 = wb.add_sheet("记账明细")
        ws2.width(12, 12, 16, 14, 12, 12)
        ws2.freeze_header = True
        ws2.append(["日期", "类型", "科目", "金额（元）", "数量", "凭证"], style=3)
        for e in report.events:
            qty = "" if e.get("qty") in (None, "") else f"{e['qty']} {e.get('unit') or ''}"
            ws2.append([
                str(e.get("date", "")),
                TYPE_LABEL.get(e.get("type", ""), e.get("type", "")),
                str(e.get("category", "")),
                int(e.get("amount_fen") or 0) / 100,
                qty,
                str(e.get("evidence_id") or ""),
            ])
    return wb.to_bytes()
