"""端到端演示：一份账本 → 核算 → 报表 → 快照 → 重放校验 → 审计链验伪。

运行：在仓库根目录执行 `python demo.py`
产出：samples/示例账本.jsonl、samples/示例快照.json、samples/示例报表.md

这个脚本对应答辩演示脚本（docs/对外表述口径与答辩要点.md §五）的前 3 分钟：
记账落库 → 出报表 → 复算一致 → 篡改被发现。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import money, reports, snapshot  # noqa: E402
from core.audit import AuditChain  # noqa: E402
from core.costing import compute, sensitivity  # noqa: E402
from core.models import Event  # noqa: E402
from core.tests import fixtures  # noqa: E402

SAMPLE_DIR = ROOT / "samples"


def event_to_dict(ev: Event) -> dict:
    return {
        "id": ev.id,
        "farmer_token": ev.farmer_token,
        "date": ev.date.isoformat(),
        "type": ev.type,
        "category": ev.category,
        "amount_fen": ev.amount_fen,
        "qty": None if ev.qty is None else f"{ev.qty:f}",
        "unit": ev.unit,
        "source": ev.source,
        "state": ev.state,
        "estimated": ev.estimated,
    }


def main() -> int:
    SAMPLE_DIR.mkdir(exist_ok=True)

    events = fixtures.events()
    plots = fixtures.plots()
    params = fixtures.params()
    rule = fixtures.rule()
    use = fixtures.machinery_use()

    result = compute(events, plots, params, rule, use)
    snap = snapshot.make_snapshot(result, events, signed_by="村委-张三", period="2026")
    sens = sensitivity(events, plots, params, rule, use)
    replay = snapshot.replay(snap, result, events, plots, params, rule, use)

    # 审计链：把每笔确认动作串起来，并演示一次篡改
    chain = AuditChain(params.region_code)
    for ev in events:
        if ev.state == "confirmed":
            chain.append("村委-张三", "event.confirm", ev.id, {"amount_fen": ev.amount_fen})
    chain.append("村委-张三", "report.export", "S-2026-001", {"template": "T1"})
    audit_ok = chain.verify()

    # 篡改演示：改一笔金额后重放，必须失败
    tampered_events = list(events)
    first = tampered_events[0]
    tampered_events[0] = Event(**{**first.__dict__, "amount_fen": first.amount_fen + 100})
    tampered_replay = snapshot.replay(snap, result, tampered_events, plots, params, rule, use)

    # ---- 控制台摘要 ----
    rev, cost, res = result["revenue"], result["cost"], result["result"]
    print("=" * 68)
    print("中小农户智能财务管理系统 · 核算内核演示（确定性 + 可验证）")
    print("=" * 68)
    print(f"经营面积      {result['area']['total_mu']} 亩"
          f"（自有 {result['area']['owned_mu']} / 租入 {result['area']['rented_mu']}）")
    print(f"经营收入      {money.fmt_yuan(rev['operating_fen']):>12} 元")
    print(f"补贴收入      {money.fmt_yuan(rev['subsidy_fen']):>12} 元（单列）")
    print(f"现金成本      {money.fmt_yuan(cost['cash_fen']):>12} 元")
    print(f"推算成本      {money.fmt_yuan(cost['imputed_fen']):>12} 元"
          f"（土地机会 {money.fmt_yuan(cost['land_opportunity_fen'])}"
          f" + 折旧 {money.fmt_yuan(cost['depreciation_fen'])}"
          f" + 自用工 {money.fmt_yuan(cost['family_labor_fen'])}）")
    print(f"总成本        {money.fmt_yuan(cost['total_fen']):>12} 元")
    print(f"净收益        {money.fmt_yuan(res['net_income_fen']):>12} 元"
          f"（亩均 {money.fmt_yuan(res['per_mu_net_fen'])} 元）")
    if result["breakeven"]["farm_gate"] is not None:
        print(f"保本价        地头 {money.fmt_yuan(result['breakeven']['farm_gate'])} 元"
              f" / 到场 {money.fmt_yuan(result['breakeven']['delivered'])} 元（每公斤）")
    print("-" * 68)
    print(f"重放校验      {'通过（逐分一致）' if replay['ok'] else '失败: ' + str(replay['diff'])}")
    print(f"篡改检出      {'是（改 1 元即被重放发现）' if not tampered_replay['ok'] else '否'}")
    print(f"审计链        {audit_ok['length']} 条，校验{'通过' if audit_ok['ok'] else '失败'}")
    print(f"最敏感因素    {sens['most_sensitive_factor']}"
          f"（净收益区间 {money.fmt_yuan(sens['worst_net_income_fen'])}"
          f" ~ {money.fmt_yuan(sens['best_net_income_fen'])} 元）")
    print("=" * 68)

    # ---- 落盘样本 ----
    (SAMPLE_DIR / "示例账本.jsonl").write_text(
        "\n".join(json.dumps(event_to_dict(e), ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )
    (SAMPLE_DIR / "示例快照.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md = [
        "# 示例报表（由 demo.py 生成，勿手工修改）",
        "",
        "> 账本：10 亩水稻（自有 8 亩 + 租入 2 亩），2026 年度，共 "
        f"{result['meta']['events_total']} 条事件（含 1 条草稿未计入）。",
        "> 参数包：4401xx / 2026.1；规则包：R1-2026.1。",
        "",
        reports.render_summary_md(result, snap),
        reports.render_cost_structure_md(result),
        reports.render_t1_md(result, snap),
        reports.render_sensitivity_md(sens),
        "## 重放校验",
        "",
        f"- 重放结果：{'一致（逐分相同）' if replay['ok'] else '不一致'}",
        f"- 篡改演示（把首笔收入加 1 元后重放）："
        f"{'检出差异 → 报表被阻止出具' if not tampered_replay['ok'] else '未检出（异常）'}",
        "",
        "## 审计链",
        "",
        f"- 条数：{audit_ok['length']}，校验：{'通过' if audit_ok['ok'] else '失败'}",
        f"- 链尾哈希：`{chain.head()[:32]}…`",
        "",
    ]
    (SAMPLE_DIR / "示例报表.md").write_text("\n".join(md), encoding="utf-8")
    print(f"已写出：{SAMPLE_DIR / '示例报表.md'}")
    return 0 if replay["ok"] and not tampered_replay["ok"] and audit_ok["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
