"""页面视图：农户端与管理端的服务端渲染。

交互设计基线（docs/技术方案V2.md §10.1）：
- 一页一个任务，不搞多 tab 表单；
- 每笔记账 ≤ 3 步：选类型 → 填金额 → 保存；
- 术语零财务化："本钱/卖了多少/赚了多少"，不用"成本/收入/净利"；
- 追问一次只问一个字段，并给选项；
- 误触可撤销（草稿与作废都是一步操作）。
"""
from __future__ import annotations

import json
import urllib.parse

from .. import repo
from ..calc import calculate, calculate_sensitivity, load_context
from ..clock import now_iso, today
from ..errors import AppError, ValidationError
from ..extract import extract
from ..ledger import LedgerService, month_summary, parse_amount_fen
from ..packs import CATEGORY_CATALOG, TYPE_LABELS
from ..report import TEMPLATE_INFO, farmer_summary
from ..report_svc import ReportService
from ..snapshot_svc import SnapshotService
from . import templates as T
from .server import Request, Response

__all__ = ["register"]

ROLE_LABEL = {"admin": "管理员", "clerk": "经办人", "viewer": "只读"}
SOURCE_LABEL = {"manual": "手填", "import": "导入", "ocr": "拍照识别", "asr": "语音识别"}


# ---------- 通用小工具 ----------

def _msg(path: str, message: str = "", kind: str = "ok", say: str = "", **extra) -> Response:
    params = dict(extra)
    if message:
        params["msg"] = message
        params["kind"] = kind
    if say:
        params["say"] = say
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return Response.redirect(path + (("?" + query) if query else ""))


def _conn(req: Request):
    return req.conn


def _ledger(req: Request) -> LedgerService:
    return LedgerService(req.conn, req.user["display_name"] if req.user else "system")


def _farmers(req: Request) -> list[dict]:
    return repo.list_farmers(req.conn)


def _farmer(req: Request) -> dict | None:
    farmers = _farmers(req)
    fid = req.q("farmer")
    if fid.isdigit():
        for f in farmers:
            if int(f["id"]) == int(fid):
                return f
    return farmers[0] if farmers else None


def _name(req: Request, farmer: dict) -> str:
    """解密姓名（只用于界面显示；报表与导出永远只用 token）。"""
    try:
        return req.app.keys.unseal_text(farmer["name_enc"], aad=b"farmer-name")
    except Exception:  # noqa: BLE001
        return farmer.get("token", "（姓名无法解密）")


def _farmer_options(req: Request, current: dict | None) -> str:
    farmers = _farmers(req)
    if len(farmers) <= 1:
        return ""
    opts = "".join(
        f'<option value="{f["id"]}"{" selected" if current and f["id"] == current["id"] else ""}>'
        f'{T.esc(_name(req, f))}（{T.esc(f["token"])}）</option>'
        for f in farmers
    )
    return (
        '<form method="get" class="farmer-pick"><label>当前农户</label>'
        f'<select name="farmer" onchange="this.form.submit()">{opts}</select>'
        '<noscript><button class="btn">切换</button></noscript></form>'
    )


def _render(req: Request, title: str, body: str, *, active: str = "", message: str = "",
            kind: str = "ok", say: str = "") -> str:
    """统一渲染：没显式传提示时，从查询串里读（PRG 模式的落点）。"""
    if not message:
        q_message, q_kind, q_say = _read_message(req)
        message = message or q_message
        if q_message:
            kind = q_kind
        say = say or q_say
    user = req.user
    if user:
        user = {**user, "csrf": user.get("csrf", "")}
    return T.layout(title, body, user=user, active=active, message=message,
                    message_kind=kind, say=say)


def _read_message(req: Request) -> tuple[str, str, str]:
    return req.q("msg"), req.q("kind") or "ok", req.q("say")


def _csrf(req: Request) -> str:
    return (req.user or {}).get("csrf", "")


# ---------- 登录 ----------

def _login_page(req: Request, error: str = "", username: str = "") -> str:
    body = f"""
<div class="login-card card">
  <h1>乡村财务智管家</h1>
  <p class="muted">村委办公电脑 · 断网可用 · 账目不出村</p>
  <form method="post" action="/login">
    {T.field("账号", "username", value=username, required=True, autofocus=True)}
    {T.field("口令", "password", type="password", required=True)}
    <input type="hidden" name="next" value="{T.esc(req.q('next', '/'))}"/>
    <button class="btn primary block" type="submit">进来</button>
  </form>
  {f'<p class="err">{T.esc(error)}</p>' if error else ''}
  <p class="hint">第一次使用？请管理员先用 <code>python -m app.cli init</code> 建账并创建账号。</p>
</div>"""
    return T.layout("登录", body)


def register(app) -> None:
    @app.get("/login", auth=False)
    def login_form(req: Request):
        if req.user:
            return Response.redirect("/")
        return _login_page(req)

    @app.post("/login", auth=False)
    def login_submit(req: Request):
        username = (req.form.get("username") or "").strip()
        password = req.form.get("password") or ""
        key = f"{username}|{req.remote}"
        if app.limiter.blocked(key):
            return _login_page(req, "尝试次数过多，请 5 分钟后再试", username)
        user = repo.get_user_by_name(req.conn, username)
        from ..keys import verify_password

        if not user or user.get("disabled") or not verify_password(
            password, user["password_hash"], user["salt"]
        ):
            app.limiter.fail(key)
            return _login_page(req, "账号或口令不对", username)
        app.limiter.reset(key)
        from datetime import timedelta

        from ..clock import now as clock_now
        from ..ids import new_csrf_token, new_session_token

        token = new_session_token()
        csrf = new_csrf_token()
        # 必须与 now_iso() 用同一个时区（东八区），否则字符串比较会把会话判成过期
        expires_at = (clock_now() + timedelta(seconds=12 * 3600)).isoformat(timespec="seconds")
        repo.create_session(req.conn, token, int(user["id"]), csrf, now_iso(), expires_at,
                            req.headers.get("user-agent", ""))
        nxt = req.form.get("next") or "/"
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        resp = Response.redirect(nxt)
        resp.headers["Set-Cookie"] = (
            f"nfm_sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={12 * 3600}"
        )
        from ..db import set_meta

        set_meta(req.conn, "last_login_at", now_iso())
        return resp

    @app.post("/logout")
    def logout(req: Request):
        token = (req.user or {}).get("session_token")
        if token:
            repo.delete_session(req.conn, token)
        resp = Response.redirect("/login")
        resp.headers["Set-Cookie"] = "nfm_sid=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        return resp

    # ---------- 首页：记一笔 ----------

    @app.get("/")
    def home(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _render(
                req, "开始使用",
                "<div class='card'><h2>还没有农户档案</h2>"
                "<p>先请管理员在 <a href='/admin'>管理</a> 里添加农户与地块，再回来记账。</p></div>",
                active="home",
            )
        month = today().strftime("%Y-%m")
        summary = month_summary(req.conn, int(farmer["id"]), month)
        drafts = _ledger(req).drafts(int(farmer["id"]))
        recent = _ledger(req).list(int(farmer["id"]), limit=5)
        message, kind, say = _read_message(req)

        big = f"""
<div class="hero">
  <div class="hero-line">这个月</div>
  <div class="hero-row">
    <div class="metric"><span class="k">收入</span><span class="v">{T.money(summary['income_fen'])}</span><span class="u">元</span></div>
    <div class="metric"><span class="k">支出</span><span class="v">{T.money(summary['expense_fen'])}</span><span class="u">元</span></div>
    <div class="metric{' neg' if summary['balance_fen'] < 0 else ''}"><span class="k">结余</span><span class="v">{T.money(summary['balance_fen'])}</span><span class="u">元</span></div>
  </div>
</div>
<div class="actions">
  <a class="tile" href="/entry?farmer={farmer['id']}"><span class="ico">➕</span><span>手动记一笔</span></a>
  <a class="tile" href="/quick?farmer={farmer['id']}"><span class="ico">⌨️</span><span>说一句话记</span></a>
  <a class="tile" href="/summary?farmer={farmer['id']}"><span class="ico">📊</span><span>看这个月</span></a>
  <a class="tile" href="/reports?farmer={farmer['id']}"><span class="ico">📄</span><span>出报表</span></a>
</div>
<div class="offline-note">
  本机未安装拍照/语音识别模型：<b>拍照记账与语音输入暂不可用</b>，其余功能不受影响。
  可用「说一句话记」直接打字，系统自动拆分日期、金额、科目。
</div>"""

        draft_html = ""
        if drafts:
            rows = [
                [T.esc(d["date"]), T.esc(d["category"]), T.money(d["amount_fen"]),
                 f'<a class="btn small" href="/drafts?farmer={farmer["id"]}">去确认</a>']
                for d in drafts[:5]
            ]
            draft_html = T.card(f"待确认（{len(drafts)} 笔）", T.table(["日期", "科目", "金额（元）", ""], rows))

        recent_html = ""
        if recent:
            rows = [
                [T.esc(e["date"]), T.esc(e["category"]), T.esc(TYPE_LABELS.get(e["type"], e["type"])),
                 T.money(e["amount_fen"])]
                for e in recent
            ]
            recent_html = T.card("最近记的账", T.table(["日期", "科目", "类型", "金额（元）"], rows),
                                 foot=f'<a href="/ledger?farmer={farmer["id"]}">看全部</a>')

        body = (
            _farmer_options(req, farmer)
            + f'<div class="who-line">正在给 <b>{T.esc(_name(req, farmer))}</b>'
              f'（{T.esc(farmer["token"])}）记账</div>'
            + big + draft_html + recent_html
        )
        return _render(req, "记一笔", body, active="home", message=message, kind=kind, say=say)

    # ---------- 手动记账 ----------

    def _entry_form(req: Request, farmer: dict, values: dict | None = None, error: str = "") -> str:
        v = values or {}
        plots = repo.list_plots(req.conn, int(farmer["id"]))
        plot_opts = [("", "不指定地块")] + [
            (str(p["id"]), f"{p['name'] or ('地块' + str(p['id']))}（{p['area_mu']} 亩 · {p['crop']}）")
            for p in plots
        ]
        type_opts = [("variable_cost", "支出 · 农资/雇工/机械"), ("income", "收入 · 卖粮/补贴"),
                     ("fixed_cost", "支出 · 租金/保险"), ("asset", "资产 · 买农机"), ("liability", "负债 · 借款")]
        cat_opts: list[tuple[str, str]] = []
        for etype, names in CATEGORY_CATALOG.items():
            for name in names:
                cat_opts.append((name, f"{TYPE_LABELS[etype]} · {name}"))
        body = f"""
<div class="card entry">
  <h2>手动记一笔</h2>
  {f'<p class="err">{T.esc(error)}</p>' if error else ''}
  <form method="post" action="/entry" enctype="multipart/form-data">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="farmer" value="{farmer['id']}"/>
    {T.field("这是", "type", value=v.get("type", "variable_cost"), options=type_opts, required=True)}
    {T.field("多少钱（元）", "amount", value=v.get("amount", ""), type="text",
             placeholder="例如 200 或 200.50", required=True, autofocus=True)}
    {T.field("买/卖了什么", "category", value=v.get("category", "化肥"), options=cat_opts, required=True)}
    {T.field("哪天", "date", value=v.get("date", today().isoformat()), type="date", required=True)}
    {T.field("数量（可不填）", "qty", value=v.get("qty", ""), placeholder="例如 500", hint="填了数量就要选单位")}
    {T.field("单位", "unit", value=v.get("unit", ""),
             options=[("", "不填"), ("kg", "公斤"), ("斤", "斤"), ("袋", "袋"), ("瓶", "瓶"),
                      ("亩", "亩"), ("吨", "吨"), ("小时", "小时")])}
    {T.field("哪块地（可不填）", "plot_id", value=v.get("plot_id", ""), options=plot_opts)}
    {T.field("备注（可不填）", "note", value=v.get("note", ""), placeholder="例如：复合肥，老王农资店")}
    <div class="field"><label for="f-evidence">票据照片（可不填）</label>
      <input type="file" name="evidence" id="f-evidence"
             accept="image/*,.pdf" capture="environment"/>
      <p class="hint">拍照或选一张票据/收据，会加密存到本机，只在本村可查。</p></div>
    <button class="btn primary block big" type="submit">记好了</button>
  </form>
</div>"""
        return body

    @app.get("/entry")
    def entry_form(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        return _render(req, "手动记一笔", _entry_form(req, farmer), active="home")

    @app.post("/entry")
    def entry_submit(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        values = {k: req.form.get(k, "") for k in
                  ("type", "amount", "category", "date", "qty", "unit", "plot_id", "note")}
        try:
            amount_fen = parse_amount_fen(values["amount"])
            plot_id = int(values["plot_id"]) if values["plot_id"].isdigit() else None
            evidence_id = None
            upload = req.files.get("evidence")
            if upload and upload[1]:
                from ..evidence import EvidenceService

                filename, data = upload
                if len(data) > 20 * 1024 * 1024:
                    raise ValidationError("票据文件太大了（上限 20 MB），请先压缩")
                evidence_id = EvidenceService(
                    req.conn, req.app.cfg, req.app.keys, _ledger(req).actor
                ).store(data, EvidenceService.kind_of(filename), filename)
            eid = _ledger(req).record(
                farmer_id=int(farmer["id"]),
                date_text=values["date"],
                type=values["type"],
                category=values["category"],
                amount_fen=amount_fen,
                qty=values["qty"] or None,
                unit=values["unit"] or None,
                plot_id=plot_id,
                note=values["note"],
                source="manual",
                state="confirmed",
                evidence_id=evidence_id,
            )
        except AppError as exc:
            return _render(req, "手动记一笔",
                           _entry_form(req, farmer, values, exc.message), active="home",
                           message=exc.message, kind="err")
        say = f"记好了，{values['category']}{values['amount']}元"
        return _msg("/", f"记好了：{values['category']} {values['amount']} 元", say=say,
                    farmer=farmer["id"])

    # ---------- 一句话记账 ----------

    @app.get("/quick")
    def quick_form(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        body = f"""
<div class="card">
  <h2>说一句话记</h2>
  <p class="muted">像平时说话一样打出来，系统帮你拆成日期、金额、科目。拿不准的会再问你一次。</p>
  <form method="post" action="/quick">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="farmer" value="{farmer['id']}"/>
    {T.field("这一笔", "text", value=req.form.get("text", ""), rows=4, required=True,
             placeholder="例如：今天买化肥花了200元",
             hint="也可以用「两百」「一千二」这类说法")}
    <button class="btn primary block big" type="submit">拆一下看看</button>
  </form>
  <p class="hint">示例：昨天卖稻谷收入1200元 ／ 5月3日请人插秧花了一千二 ／ 买尿素两袋350块</p>
</div>"""
        return _render(req, "说一句话记", body, active="home")

    @app.post("/quick")
    def quick_extract(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        text = (req.form.get("text") or "").strip()
        if not text:
            return _render(req, "说一句话记", T.card("说一句话记", "<p>还没输入内容。</p>"),
                           active="home", message="请输入一句话", kind="err")
        result = extract(text, today())
        data = result.as_dict()
        needs = set(result.needs_confirm)
        warn = ""
        if needs:
            warn = (
                f'<p class="warn">有一项拿不准，请确认：'
                f'{"、".join({"amount": "金额", "category": "科目", "date": "日期"}[n] for n in needs)}</p>'
            )
        amount_value = ("" if data["amount"]["value"] == 0
                        else f"{data['amount']['value'] / 100:.2f}")
        type_opts = [("variable_cost", "支出"), ("income", "收入"), ("fixed_cost", "支出·固定"),
                     ("asset", "资产"), ("liability", "负债")]
        cat_opts = [(c, f"{TYPE_LABELS[t]} · {c}") for t, names in CATEGORY_CATALOG.items() for c in names]
        body = f"""
<div class="card">
  <h2>请核对这一笔</h2>
  <p class="muted">原话：{T.esc(text)}</p>
  {warn}
  <form method="post" action="/quick/save">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="farmer" value="{farmer['id']}"/>
    <input type="hidden" name="note" value="{T.esc(text)}"/>
    {T.field("金额（元）", "amount", value=amount_value, required=True,
             placeholder="没识别出来，请自己填")}
    {T.field("科目", "category", value=data["category"]["value"] or "化肥",
             options=cat_opts, required=True)}
    {T.field("日期", "date", value=data["date"]["value"], type="date", required=True)}
    {T.field("这是", "type", value=data["type"]["value"], options=type_opts, required=True)}
    {T.field("数量", "qty", value=str(data.get("qty", {}).get("value", "")) if data.get("qty") else "")}
    {T.field("单位", "unit", value=data.get("unit") or "",
             options=[("", "不填"), ("kg", "公斤"), ("斤", "斤"), ("袋", "袋"), ("瓶", "瓶"), ("亩", "亩")])}
    <div class="row">
      <button class="btn primary block big" type="submit">对，记上</button>
      <a class="btn block" href="/quick?farmer={farmer['id']}">不对，重说</a>
    </div>
  </form>
</div>"""
        return _render(req, "核对这一笔", body, active="home")

    @app.post("/quick/save")
    def quick_save(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        try:
            amount_fen = parse_amount_fen(req.form.get("amount"))
            _ledger(req).record(
                farmer_id=int(farmer["id"]),
                date_text=req.form.get("date") or today().isoformat(),
                type=req.form.get("type") or "variable_cost",
                category=req.form.get("category") or "其他变动成本",
                amount_fen=amount_fen,
                qty=req.form.get("qty") or None,
                unit=req.form.get("unit") or None,
                note=(req.form.get("note") or "")[:200],
                source="manual",
                state="confirmed",
            )
        except AppError as exc:
            return _msg(f"/quick?farmer={farmer['id']}", exc.message, "err")
        return _msg("/", "记好了", say=f"记好了，{req.form.get('category')}{req.form.get('amount')}元",
                    farmer=farmer["id"])

    # ---------- 待确认（草稿） ----------

    @app.get("/drafts")
    def drafts(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        items = _ledger(req).drafts(int(farmer["id"]))
        if not items:
            body = T.card("待确认", "<p>没有待确认的账，都清干净了。</p>")
        else:
            rows = []
            for d in items:
                rows.append([
                    T.esc(d["date"]), T.esc(d["category"]),
                    T.money(d["amount_fen"]), T.esc(d.get("note") or ""),
                    f"""<form method="post" action="/drafts/{d['id']}/confirm" class="inline">
                    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                    <button class="btn small primary">确认</button></form>
                    <form method="post" action="/drafts/{d['id']}/void" class="inline">
                    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                    <button class="btn small">作废</button></form>""",
                ])
            body = T.card(f"待确认（{len(items)} 笔）",
                          T.table(["日期", "科目", "金额（元）", "原话", "操作"], rows))
        return _render(req, "待确认", _farmer_options(req, farmer) + body, active="ledger")

    @app.post("/drafts/{eid}/confirm")
    def draft_confirm(req: Request, eid: str):
        _ledger(req).confirm(eid)
        return _msg("/drafts", "已确认")

    @app.post("/drafts/{eid}/void")
    def draft_void(req: Request, eid: str):
        _ledger(req).void(eid, "草稿作废")
        return _msg("/drafts", "已作废")

    # ---------- 账本 ----------

    @app.get("/ledger")
    def ledger(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        month = req.q("month") or today().strftime("%Y-%m")
        items = _ledger(req).list(int(farmer["id"]), month=month, limit=None)
        summary = month_summary(req.conn, int(farmer["id"]), month)
        rows = []
        for e in items:
            voided = e["state"] == "voided"
            action = "" if voided else (
                f"""<form method="post" action="/ledger/{e['id']}/void" class="inline"
                onsubmit="return confirm('确实要作废这一笔吗？作废后不能恢复。');">
                <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                <button class="btn small">作废</button></form>"""
            )
            rows.append([
                T.esc(e["date"]), T.esc(e["category"]), T.esc(TYPE_LABELS.get(e["type"], e["type"])),
                T.money(e["amount_fen"]),
                T.esc((e.get("note") or "")[:20]),
                ("已作废" if voided else T.esc(SOURCE_LABEL.get(e["source"], e["source"]))),
                action,
            ])
        body = f"""
<div class="card">
  <h2>{T.esc(month)} 的账</h2>
  <div class="hero-row small">
    <div class="metric"><span class="k">收入</span><span class="v">{T.money(summary['income_fen'])}</span></div>
    <div class="metric"><span class="k">支出</span><span class="v">{T.money(summary['expense_fen'])}</span></div>
    <div class="metric"><span class="k">结余</span><span class="v">{T.money(summary['balance_fen'])}</span></div>
  </div>
  <form method="get" class="inline"><input type="hidden" name="farmer" value="{farmer['id']}"/>
    <label>换个月份</label><input type="month" name="month" value="{T.esc(month)}"/>
    <button class="btn">查看</button></form>
  {T.table(["日期", "科目", "类型", "金额（元）", "备注", "来源", ""], rows) if rows else "<p class='muted'>这个月还没有账。</p>"}
</div>"""
        return _render(req, "账本", _farmer_options(req, farmer) + body, active="ledger")

    @app.post("/ledger/{eid}/void")
    def ledger_void(req: Request, eid: str):
        _ledger(req).void(eid, req.form.get("reason") or "手动作废")
        return _msg("/ledger", "已作废，可在报表中看到影响")

    # ---------- 年度看账 ----------

    @app.get("/summary")
    def summary(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        year = req.q("year") or str(today().year)
        try:
            ctx = load_context(req.conn, int(farmer["id"]), year)
            result = calculate(ctx)
            sens = calculate_sensitivity(ctx)
        except AppError as exc:
            return _render(req, "看年账", T.card("暂时算不出来", f"<p class='err'>{T.esc(exc.message)}</p>"),
                           active="summary")
        s = farmer_summary(result, sens)
        structure_rows = [
            [T.esc(item["name"]), T.esc(item["amount_text"]), f"{item['share'] * 100:.0f}%"]
            for item in s["cost_structure"]
        ]
        be = result["breakeven"]
        body = f"""
<div class="hero">
  <div class="hero-line">{T.esc(year)} 年，{T.esc(s['headline'])}</div>
  <div class="hero-big">{T.esc(s['net_text'])} <small>元</small></div>
  <div class="hero-sub">平均每亩 {T.esc(s['per_mu_text'])} 元</div>
</div>
<div class="grid2">
  {T.card("钱花在哪了", T.table(["项目", "金额（元）", "占比"], structure_rows))}
  {T.card("卖多少钱不亏", f'''
    <p class="big-num">地头 <b>{T.esc(s['breakeven_farm_gate'] or '—')}</b> 元/公斤</p>
    <p class="muted">粮贩上门收，低于这个价就亏了。</p>
    <p class="big-num">到场 <b>{T.esc(s['breakeven_delivered'] or '—')}</b> 元/公斤</p>
    <p class="muted">自己拉到粮库，含运输、烘干、装卸的钱。</p>
  ''')}
</div>
{T.card("可能赚多少（区间，不是精确值）", f'''
  <p class="muted">把"自己出工折算、卖价、产量"各上下浮动 20% 后，今年的结果大致落在这个区间：</p>
  <p class="big-num">{T.esc(s.get('range_text', '—'))}</p>
  <p class="muted">影响最大的是：<b>{T.esc(s.get('most_sensitive', '—'))}</b>。点这里看 <a href="/reports?farmer={farmer['id']}">给银行的报表</a>。</p>
''')}
"""
        return _render(req, "看年账", _farmer_options(req, farmer) + body, active="summary")

    # ---------- 报表 ----------

    @app.get("/reports")
    def reports(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        year = req.q("year") or str(today().year)
        snaps = SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).list(
            int(farmer["id"])
        )
        snap_rows = []
        for s in snaps:
            snap_rows.append([
                T.esc(s["id"]), T.esc(s["period"]), T.esc(s["signed_by"]),
                f'<code>{T.esc(s["merkle_root"][:12])}…</code>', T.esc(s["created_at"]),
            ])
        export_forms = []
        for s in snaps[:3]:
            buttons = []
            for tpl, info in TEMPLATE_INFO.items():
                buttons.append(
                    f"""<form method="post" action="/reports/export" class="inline">
                    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                    <input type="hidden" name="sid" value="{s['id']}"/>
                    <input type="hidden" name="template" value="{tpl}"/>
                    <input type="hidden" name="format" value="html"/>
                    <button class="btn small" title="{T.esc(info['audience'])}">{T.esc(info['name'])}</button>
                    </form>"""
                )
            export_forms.append(
                f"<div class='export-row'><b>{T.esc(s['id'])}</b> {''.join(buttons)}"
                f"""<form method="post" action="/reports/export" class="inline">
                <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                <input type="hidden" name="sid" value="{s['id']}"/>
                <input type="hidden" name="template" value="T1-v1"/>
                <input type="hidden" name="format" value="xlsx"/>
                <button class="btn small">导出 Excel</button></form>
                <form method="post" action="/reports/export" class="inline">
                <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
                <input type="hidden" name="sid" value="{s['id']}"/>
                <input type="hidden" name="template" value="T1-v1"/>
                <input type="hidden" name="format" value="zip"/>
                <button class="btn small">银行材料包</button></form>
                <a class="btn small" href="/verify?sid={s['id']}">验一验</a></div>"""
            )
        body = f"""
<div class="card">
  <h2>{T.esc(year)} 年报表</h2>
  <p class="muted">报表一旦生成就锁定成"快照"：之后补记的账不会改变已盖章的报表。</p>
  <form method="post" action="/reports/create">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="farmer" value="{farmer['id']}"/>
    <label>出哪一年</label><input type="number" name="period" value="{T.esc(year)}" min="2000" max="2100"/>
    <button class="btn primary">生成并签章</button>
  </form>
  {T.table(["快照号", "年度", "签章人", "Merkle 根", "生成时间"], snap_rows) if snap_rows else "<p class='muted'>还没有生成过报表。</p>"}
</div>
{T.card("导出", "".join(export_forms) if export_forms else "<p class='muted'>先生成快照。</p>")}
"""
        return _render(req, "出报表", _farmer_options(req, farmer) + body, active="reports")

    @app.post("/reports/create")
    def report_create(req: Request):
        farmer = _farmer(req)
        if not farmer:
            return _msg("/admin", "请先添加农户", "err")
        period = (req.form.get("period") or str(today().year)).strip()
        if not (period.isdigit() and len(period) == 4):
            return _msg(f"/reports?farmer={farmer['id']}", "年度要填四位数字，例如 2026", "err")
        try:
            snap = SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).create(
                int(farmer["id"]), period
            )
        except AppError as exc:
            return _msg(f"/reports?farmer={farmer['id']}&year={period}", exc.message, "err")
        return _msg(f"/reports?farmer={farmer['id']}&year={period}",
                    f"已生成快照 {snap['id']} 并完成签章")

    @app.post("/reports/export")
    def report_export(req: Request):
        sid = req.form.get("sid") or ""
        tpl = req.form.get("template") or "T1-v1"
        fmt = req.form.get("format") or "html"
        try:
            result = ReportService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).export(
                sid, tpl, fmt
            )
        except AppError as exc:
            return _msg("/reports", exc.message, "err")
        return Response.redirect(result.url)

    # ---------- 验一验 ----------

    @app.get("/verify")
    def verify_page(req: Request):
        sid = req.q("sid")
        result_html = ""
        if sid:
            try:
                v = SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).verify(sid)
                result_html = _verify_result_html(v)
            except AppError as exc:
                result_html = f"<p class='err'>{T.esc(exc.message)}</p>"
        body = f"""
{T.card("验证一份快照", f'''
  <p class="muted">验证做两件事：① 用公钥验 SM2 签名；② 只用快照自带的输入重算，与快照里的结果逐项比对。</p>
  <form method="get"><label>快照号</label><input name="sid" value="{T.esc(sid)}" placeholder="S-2026-0001"/>
  <button class="btn primary">验一验</button></form>
  <hr/>
  <form method="post" action="/verify/json">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <label>或粘贴银行收到的快照 JSON</label>
    <textarea name="payload" rows="6" placeholder='{{"period": "2026", ...}}'></textarea>
    <button class="btn">只验签名</button>
  </form>
''')}
{result_html}
"""
        return _render(req, "验一验", body, active="reports")

    @app.post("/verify/json")
    def verify_json(req: Request):
        from ..verify import verify_exported

        try:
            data = json.loads(req.form.get("payload") or "{}")
        except json.JSONDecodeError:
            return _msg("/verify", "粘贴的内容不是合法 JSON", "err")
        snap = data.get("snapshot", data)
        result = verify_exported(snap)
        html = (
            f"<div class='card'><h2>验证结果：{'通过' if result['ok'] else '不通过'}</h2>"
            f"<ul><li>SM2 签名：{'有效' if result['signature_ok'] else '无效'}</li>"
            f"<li>签章人：{T.esc(result.get('signed_by', '—'))}</li>"
            f"<li>Merkle 根：<code>{T.esc(result.get('merkle_root', ''))}</code></li>"
            f"<li>事件数：{T.esc(result.get('event_count', ''))}</li>"
            f"<li>出表时间：{T.esc(result.get('created_at', ''))}</li></ul>"
            f"<p class='muted'>{T.esc(result.get('note', ''))}</p></div>"
        )
        return _render(req, "验证结果", html, active="reports")

    # ---------- 帮助 ----------

    @app.get("/help")
    def help_page(req: Request):
        body = f"""
{T.card("三步记一笔", '''
  <ol class="steps">
    <li><b>点「手动记一笔」或「说一句话记」</b>——不用先学财务词，按平时说法填就行。</li>
    <li><b>填金额、选科目、选日期</b>——金额填元，可以带小数；科目点了就有选项。</li>
    <li><b>点「记好了」</b>——会听到一句确认，账就落下了；记错了可以当场「作废」重记。</li>
  </ol>
''')}
{T.card("常问的几件事", '''
  <dl class="faq">
    <dt>断网了还能用吗？</dt><dd>能。所有功能都在这台电脑上跑，不需要网络。</dd>
    <dt>我的姓名、手机号会被发出去吗？</dt><dd>不会。报表和给银行的材料里只有代币（比如 F-ABC123），
      姓名与手机号在本机加密保存，只有村委能看。</dd>
    <dt>报表为什么不能改？</dt><dd>报表生成时会锁定成"快照"并盖章。之后补记的账不会改旧报表，
      要更新就再生成一份新的，两份都能查。</dd>
    <dt>银行怎么确认报表是真的？</dt><dd>给银行的材料包里带"快照"文件，里面有数字指纹和村委签名。
      银行用公钥验签、再比对指纹即可，不需要拿走全部账本。</dd>
    <dt>换了电脑怎么办？</dt><dd>管理员在本机做一次「备份」，把备份文件拷到新电脑，
      用 restore 命令恢复即可（备份含密钥，U 盘要上锁）。</dd>
    <dt>怎么防止别人乱看？</dt><dd>每人一个账号，口令至少 8 位；管理员可停用账号；
      所有操作都进审计链，改不了也删不掉。</dd>
  </dl>
''')}
{T.card("这台电脑现在能做什么、不能做什么", '''
  <table class="grid">
    <thead><tr><th>能力</th><th>状态</th><th>说明</th></tr></thead>
    <tbody>
      <tr><td>手动记账 / 一句话记账</td><td>可用</td><td>规则抽取，不需要模型</td></tr>
      <tr><td>票据拍照归档</td><td>可用</td><td>加密存本机，供抽验</td></tr>
      <tr><td>年度核算 / 保本价 / 敏感性</td><td>可用</td><td>确定性引擎，可逐分复算</td></tr>
      <tr><td>T1/T2/T3 报表与导出</td><td>可用</td><td>HTML 打印、Excel、银行材料包</td></tr>
      <tr><td>快照签章与离线验证</td><td>可用</td><td>SM2 + SM3</td></tr>
      <tr><td>拍照自动识别金额</td><td><b>未安装</b></td><td>需本机 OCR 模型；当前请手填</td></tr>
      <tr><td>语音说话自动记账</td><td><b>未安装</b></td><td>需本机离线语音模型；当前请打字</td></tr>
      <tr><td>语音播报确认</td><td>取决于系统</td><td>浏览器自带语音可用时自动播报</td></tr>
    </tbody>
  </table>
  <p class="muted">界面会把"缺什么"直接说出来，不做静默降级——这是本系统的设计原则。</p>
''')}
"""
        return _render(req, "帮助", body, active="help")


def _verify_result_html(v: dict) -> str:
    sig = v["signature"]
    replay = v["replay"]
    rows = [
        ["SM2 签名", "有效" if sig["ok"] else "无效"],
        ["签章人", sig.get("signed_by", "")],
        ["设备标识", sig.get("device_id", "")],
        ["算法", sig.get("algorithm", "")],
        ["事件集哈希一致", "是" if replay["checks"]["event_set_hash"] else "否"],
        ["Merkle 根一致", "是" if replay["checks"]["merkle_root"] else "否"],
        ["重放结果逐项一致", "是" if replay["checks"]["output"] else "否"],
        ["事件条数", str(v.get("event_count", ""))],
        ["出表时间", v.get("created_at", "")],
        ["审计链", "通过" if v.get("audit_chain", {}).get("ok") else "未通过"],
    ]
    return T.card(
        f"验证结果：{'通过' if v['ok'] else '不通过'}",
        T.table(["检查项", "结果"], rows)
        + "<p class='muted'>本结果证明：账本未被事后修改、报表忠实于账本、有人对它负责。"
          "它不证明农户没有造假账——防造假靠抽验原始票据与交叉核对。</p>",
    )
