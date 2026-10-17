"""管理端与隐私中心页面。

管理端只对 admin 开放：农户/地块档案、参数包与规则包、账号、审计链、备份、自检。
隐私中心对所有角色开放：让每个使用者都能看见"系统本地存了什么、外发会带什么"，
这比在说明书里写一句"我们很安全"有用得多。
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from .. import repo
from ..backup import create_backup, list_backups
from ..clock import now_iso, today
from ..db import next_counter, transaction
from ..errors import AppError, UserError, ValidationError
from ..ids import token_for
from ..keys import hash_password
from ..packs import CATEGORY_CATALOG, DEFAULT_PARAM_PACK, DEFAULT_RULE_PACK
from ..privacy import L1, L2, L3, FIELD_LEVELS
from ..privacy_svc import OutboundService
from ..selfcheck import run_selfcheck
from ..snapshot_svc import SnapshotService
from . import templates as T
from .server import Request, Response
from .views import _csrf, _farmer, _farmer_options, _msg, _name, _render

__all__ = ["register"]

ROLE_LABEL = {"admin": "管理员", "clerk": "经办人", "viewer": "只读"}


def _require_admin(req: Request) -> None:
    if not req.user or req.user.get("role") != "admin":
        from ..errors import PermissionDenied

        raise PermissionDenied("只有管理员能做这个操作")


def register(app) -> None:
    # ---------- 管理首页 ----------

    @app.get("/admin", roles=("admin",))
    def admin_home(req: Request):
        _require_admin(req)
        conn = req.conn
        village = repo.get_village(conn)
        farmers = repo.list_farmers(conn, include_archived=True)
        users = repo.list_users(conn)
        params = repo.list_param_packs(conn)
        rules = repo.list_rule_packs(conn)
        backups = list_backups(req.app.cfg)
        message, kind, say = req.q("msg"), req.q("kind") or "ok", ""

        farmer_rows = []
        for f in farmers:
            plots = repo.list_plots(conn, int(f["id"]))
            farmer_rows.append([
                T.esc(f["token"]), T.esc(_name(req, f)), T.esc(f.get("group_name") or ""),
                str(len(plots)), T.esc(f["created_at"][:10]),
            ])

        user_rows = [
            [T.esc(u["username"]), T.esc(u["display_name"]), T.esc(ROLE_LABEL.get(u["role"], u["role"])),
             "停用" if u["disabled"] else "启用",
             "—" if u["id"] == req.user["id"] else f"""<form method="post" action="/admin/users/{u['id']}/toggle" class="inline">
             <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
             <button class="btn small">{'启用' if u['disabled'] else '停用'}</button></form>"""]
            for u in users
        ]

        plot_options = "".join(
            f'<option value="{f["id"]}">{T.esc(_name(req, f))}</option>' for f in farmers
        ) or "<option value=''>先添加农户</option>"

        body = f"""
<div class="grid2">
{T.card("本村信息", f'''
  <p><b>村名：</b>{T.esc(village.get('name', '未初始化'))}</p>
  <p><b>区域代码：</b>{T.esc(village.get('region_code', ''))}</p>
  <p><b>规则包：</b>{T.esc(village.get('rule_version', ''))}　
     <b>参数包：</b>{T.esc(village.get('param_version', ''))}</p>
  <p><b>设备标识：</b><code>{T.esc(req.app.keys.device_id)}</code></p>
  <p class="muted">设备标识与 SM2 签章共同绑定"谁在什么设备上出的这份报表"。</p>
''')}
{T.card("系统自检", f'''
  <p class="muted">检查数据库、审计链、密钥、凭证、快照重放、磁盘空间。</p>
  <a class="btn primary" href="/admin/selfcheck">跑一次自检</a>
''')}
</div>

{T.card("农户档案", f'''
  {T.table(["代币", "姓名", "村组", "地块数", "建档日期"], farmer_rows) if farmer_rows else "<p class='muted'>还没有农户。</p>"}
  <hr/>
  <form method="post" action="/admin/farmers">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    {T.field("姓名", "name", required=True, placeholder="只在本机加密保存，报表里只出现代币")}
    {T.field("村组", "group", placeholder="例如：3 组")}
    {T.field("手机号（可不填）", "phone", placeholder="加密保存，界面掩码显示")}
    <button class="btn primary">添加农户</button>
  </form>
''')}

{T.card("地块", f'''
  <form method="post" action="/admin/plots">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    {T.field("农户", "farmer_id", options=[(str(f["id"]), f"{_name(req, f)}（{f['token']}）") for f in farmers])}
    {T.field("地块名", "name", placeholder="例如：门口田")}
    {T.field("面积（亩）", "area_mu", required=True, placeholder="例如 8 或 8.5")}
    {T.field("权属", "tenure", options=[("owned", "自有"), ("rented", "租入")], value="owned")}
    {T.field("作物", "crop", options=[(c, c) for c in ("水稻", "玉米", "蔬菜", "果园")], value="水稻")}
    <button class="btn primary">添加地块</button>
  </form>
''') if farmers else T.card("地块", "<p class='muted'>先添加农户。</p>")}

<div class="grid2">
{T.card("参数包", f'''
  {T.table(["版本", "区域", "创建时间"], [[T.esc(p['version']), T.esc(p['region_code']), T.esc(p['created_at'][:10])] for p in params]) if params else "<p class='muted'>无</p>"}
  <details><summary>导入新参数包（JSON）</summary>
  <form method="post" action="/admin/packs">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="kind" value="param"/>
    <textarea name="payload" rows="8" placeholder='{{"region_code": "4401xx", "version": "2026.2", ...}}'></textarea>
    <button class="btn primary">保存参数包</button>
  </form></details>
''')}
{T.card("规则包", f'''
  {T.table(["版本", "生效日", "创建时间"], [[T.esc(r['version']), T.esc(r['effective_from']), T.esc(r['created_at'][:10])] for r in rules]) if rules else "<p class='muted'>无</p>"}
  <details><summary>导入新规则包（JSON）</summary>
  <form method="post" action="/admin/packs">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="kind" value="rule"/>
    <textarea name="payload" rows="8" placeholder='{{"version": "R2-2026.1", ...}}'></textarea>
    <button class="btn primary">保存规则包</button>
  </form></details>
''')}
</div>

{T.card("账号", f'''
  {T.table(["账号", "姓名", "角色", "状态", "操作"], user_rows)}
  <hr/>
  <form method="post" action="/admin/users">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    {T.field("账号", "username", required=True)}
    {T.field("姓名", "display_name", required=True)}
    {T.field("角色", "role", options=[("clerk", "经办人（记账/出报表）"), ("admin", "管理员"), ("viewer", "只读")], value="clerk")}
    {T.field("初始口令", "password", type="password", required=True, hint="至少 8 位；首次登录后请提醒本人修改")}
    <button class="btn primary">新建账号</button>
  </form>
''')}

{T.card("备份与审计", f'''
  <p>备份包含数据库、凭证、密钥与已导出的报表。含密钥，U 盘须上锁保管。</p>
  <form method="post" action="/admin/backup" class="inline">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <button class="btn primary">立即备份</button>
  </form>
  <a class="btn" href="/admin/audit">看审计链</a>
  {T.table(["备份文件", "大小（KB）", "加密", "时间"], [[T.esc(b['name']), str(b['size'] // 1024), "是" if b['encrypted'] else "否", T.esc(b['modified_at'])] for b in backups[:5]]) if backups else "<p class='muted'>还没有备份。</p>"}
''')}
"""
        return _render(req, "管理", body, active="admin", message=message, kind=kind)

    # ---------- 农户 / 地块 ----------

    @app.post("/admin/farmers", roles=("admin",))
    def add_farmer(req: Request):
        _require_admin(req)
        name = (req.form.get("name") or "").strip()
        if not name:
            return _msg("/admin", "姓名不能为空", "err")
        village = repo.get_village(req.conn)
        with transaction(req.conn):
            seq = next_counter(req.conn, "farmer")
            token = token_for(village.get("id", "V"), seq, req.app.keys.token_secret)
            repo.create_farmer(
                req.conn,
                token,
                req.app.keys.seal_text(name, aad=b"farmer-name"),
                now_iso(),
                group_name=(req.form.get("group") or "").strip(),
                phone_enc=(
                    req.app.keys.seal_text(req.form.get("phone").strip(), aad=b"farmer-phone")
                    if (req.form.get("phone") or "").strip() else None
                ),
            )
            from ..audit_svc import AuditService

            AuditService(req.conn).append(
                req.user["display_name"], "farmer.create", token, {"group": req.form.get("group") or ""}
            )
        return _msg("/admin", f"已添加农户 {name}")

    @app.post("/admin/plots", roles=("admin",))
    def add_plot(req: Request):
        _require_admin(req)
        fid = req.form.get("farmer_id") or ""
        if not fid.isdigit():
            return _msg("/admin", "请选择农户", "err")
        try:
            area = Decimal(req.form.get("area_mu") or "0")
            if area <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return _msg("/admin", "面积要填大于 0 的数字", "err")
        with transaction(req.conn):
            repo.create_plot(
                req.conn, int(fid), f"{area:f}", req.form.get("tenure") or "owned",
                req.form.get("crop") or "水稻", (req.form.get("name") or "").strip(), 0, now_iso(),
            )
            from ..audit_svc import AuditService

            AuditService(req.conn).append(
                req.user["display_name"], "plot.create", fid,
                {"area_mu": f"{area:f}", "tenure": req.form.get("tenure") or "owned"},
            )
        return _msg("/admin", "地块已添加")

    # ---------- 参数包 / 规则包 ----------

    @app.post("/admin/packs", roles=("admin",))
    def import_pack(req: Request):
        _require_admin(req)
        kind = req.form.get("kind")
        try:
            data = json.loads(req.form.get("payload") or "{}")
        except json.JSONDecodeError:
            return _msg("/admin", "内容不是合法 JSON", "err")
        if not isinstance(data, dict) or not data.get("version"):
            return _msg("/admin", "缺少 version 字段", "err")
        from .. import repo as rp

        with transaction(req.conn):
            if kind == "param":
                region = str(data.get("region_code") or "")
                if not region:
                    return _msg("/admin", "参数包缺少 region_code", "err")
                rp.save_param_pack(req.conn, str(data["version"]), region, data, now_iso())
            else:
                rp.save_rule_pack(
                    req.conn, str(data["version"]), data,
                    str(data.get("effective_from") or today().isoformat()), now_iso(),
                )
            from ..audit_svc import AuditService

            AuditService(req.conn).append(
                req.user["display_name"], f"pack.import.{kind}", str(data["version"]), {}
            )
        return _msg("/admin", f"已保存{'参数包' if kind == 'param' else '规则包'} {data['version']}")

    # ---------- 账号 ----------

    @app.post("/admin/users", roles=("admin",))
    def add_user(req: Request):
        _require_admin(req)
        username = (req.form.get("username") or "").strip()
        display = (req.form.get("display_name") or "").strip()
        role = req.form.get("role") or "clerk"
        password = req.form.get("password") or ""
        if len(username) < 3:
            return _msg("/admin", "账号至少 3 个字符", "err")
        if len(password) < 8:
            return _msg("/admin", "口令至少 8 位", "err")
        if repo.get_user_by_name(req.conn, username):
            return _msg("/admin", "这个账号已存在", "err")
        digest, salt = hash_password(password)
        with transaction(req.conn):
            repo.create_user(req.conn, username, display or username, role, digest, salt, now_iso())
            from ..audit_svc import AuditService

            AuditService(req.conn).append(
                req.user["display_name"], "user.create", username, {"role": role}
            )
        return _msg("/admin", f"已创建账号 {username}")

    @app.post("/admin/users/{uid}/toggle", roles=("admin",))
    def toggle_user(req: Request, uid: str):
        _require_admin(req)
        if int(uid) == int(req.user["id"]):
            return _msg("/admin", "不能停用自己", "err")
        user = repo.get_user(req.conn, int(uid))
        if not user:
            return _msg("/admin", "账号不存在", "err")
        with transaction(req.conn):
            repo.set_user_disabled(req.conn, int(uid), not user["disabled"])
            from ..audit_svc import AuditService

            AuditService(req.conn).append(
                req.user["display_name"], "user.toggle", user["username"],
                {"disabled": not user["disabled"]},
            )
        return _msg("/admin", f"已{'停用' if not user['disabled'] else '启用'} {user['username']}")

    # ---------- 备份 / 审计 / 自检 ----------

    @app.post("/admin/backup", roles=("admin",))
    def do_backup(req: Request):
        _require_admin(req)
        try:
            path = create_backup(req.app.cfg)
        except AppError as exc:
            return _msg("/admin", exc.message, "err")
        return _msg("/admin", f"备份完成：{path.name}")

    @app.get("/admin/audit", roles=("admin",))
    def audit_page(req: Request):
        _require_admin(req)
        from ..audit_svc import AuditService

        svc = AuditService(req.conn)
        verify = svc.verify()
        entries = svc.entries(limit=50)
        rows = [
            [str(e["seq"]), T.esc(e["ts"]), T.esc(e["actor"]), T.esc(e["action"]),
             T.esc(e["object_ref"]), f"<code>{T.esc(e['hash'][:16])}…</code>"]
            for e in entries
        ]
        body = T.card(
            f"审计链（{verify.get('length', 0)} 条）· {'校验通过' if verify['ok'] else '校验未通过'}",
            T.table(["#", "时间", "操作人", "动作", "对象", "哈希"], rows)
            + (
                "<p class='muted'>任何一条被改动或删除，其后所有记录的哈希都会对不上。</p>"
                if verify["ok"]
                else f"<p class='err'>断点 #{verify.get('broken_at')}：{T.esc(verify.get('reason', ''))}"
                     "　请立即停止使用并备份现场。</p>"
            ),
        )
        return _render(req, "审计链", body, active="admin")

    @app.get("/admin/selfcheck", roles=("admin",))
    def selfcheck_page(req: Request):
        _require_admin(req)
        result = run_selfcheck(req.conn, req.app.cfg, req.app.keys)
        rows = [
            [T.esc(i["name"]), "通过" if i["ok"] else "未通过", T.esc(i["detail"]), T.esc(i["hint"])]
            for i in result["items"]
        ]
        body = T.card(
            f"自检结果：{'全部通过' if result['ok'] else '有问题'}",
            T.table(["检查项", "结果", "说明", "处理建议"], rows)
            + f"<p class='muted'>生成时间：{T.esc(result['generated_at'])}</p>",
        )
        return _render(req, "自检", body, active="admin")

    # ---------- 隐私中心 ----------

    @app.get("/privacy")
    def privacy_page(req: Request):
        farmer = _farmer(req)
        svc = OutboundService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"])
        masked = svc.masked_farmer(farmer) if farmer else {}
        snaps = SnapshotService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).list(
            int(farmer["id"]) if farmer else None
        )
        snap_opts = [(s["id"], f"{s['id']}（{s['period']} 年）") for s in snaps]
        history = svc.history(10)
        hist_rows = [
            [T.esc(h["created_at"]), T.esc(h["recipient"]), T.esc(h["purpose"]),
             T.esc({"approved": "已发送", "blocked": "被阻断", "cancelled": "已取消"}.get(h["decision"], h["decision"])),
             T.esc(h.get("note") or "")]
            for h in history
        ]
        body = f"""
<div class="grid2">
{T.card("本地存了什么", f'''
  <p>农户：<b>{T.esc(masked.get('name_masked', '—'))}</b>（对外标识 <code>{T.esc(masked.get('token', '—'))}</code>）</p>
  <p>手机：{T.esc(masked.get('phone_masked') or '未登记')}</p>
  <p>村组：{T.esc(masked.get('group') or '—')}</p>
  <p class="muted">姓名与手机号以 SM4-GCM 密文存本机，界面默认掩码；报表里只出现代币。</p>
''')}
{T.card("数据分级", f'''
  <table class="grid">
  <thead><tr><th>级别</th><th>内容</th><th>能否外发</th></tr></thead>
  <tbody>
  <tr><td>L1 直接标识</td><td>身份证、银行卡、手机号、真实姓名、精确住址</td><td><b>禁止</b>（结构上不出现在外发包）</td></tr>
  <tr><td>L2 准标识</td><td>村组、面积、作物、交易对手</td><td>按必要性保留/泛化/代币化</td></tr>
  <tr><td>L3 经营汇总</td><td>成本结构、收益、亩产、保本价</td><td>最小必要范围内允许</td></tr>
  </tbody></table>
''')}
</div>

{T.card("给银行生成材料包（受控外发）", f'''
  <p class="muted">材料包只包含 L2/L3 字段。打包后会再扫一遍内容，发现 L1 立即阻断并记入审计。</p>
  {f"""<form method="post" action="/privacy/package">
    <input type="hidden" name="_csrf" value="{_csrf(req)}"/>
    <input type="hidden" name="farmer" value="{farmer['id']}"/>
    {T.field("用哪份快照", "sid", options=snap_opts)}
    {T.field("发给谁", "recipient", required=True, placeholder="例如：XX 农商行 XX 支行")}
    {T.field("用途", "purpose", required=True, placeholder="例如：春耕贷款申请")}
    <label class="check"><input type="checkbox" name="consent" value="1" required/>
      我已核对上方字段，同意把这份材料交给上述机构。</label>
    <button class="btn primary block">生成材料包</button>
  </form>""" if snap_opts else "<p class='muted'>还没有快照，先去 <a href='/reports'>出报表</a>。</p>"}
''')}

{T.card("外发记录", T.table(["时间", "接收方", "用途", "结果", "备注"], hist_rows) if hist_rows else "<p class='muted'>还没有外发记录。</p>")}
"""
        return _render(req, "隐私", _farmer_options(req, farmer) + body if farmer else body, active="privacy")

    @app.post("/privacy/package")
    def privacy_package(req: Request):
        farmer = _farmer(req)
        sid = req.form.get("sid") or ""
        try:
            result = OutboundService(req.conn, req.app.cfg, req.app.keys, req.user["display_name"]).build(
                sid,
                (req.form.get("recipient") or "").strip(),
                (req.form.get("purpose") or "").strip(),
                req.form.get("consent") == "1",
            )
        except AppError as exc:
            return _msg("/privacy", exc.message, "err")
        return Response.redirect(result["url"])
