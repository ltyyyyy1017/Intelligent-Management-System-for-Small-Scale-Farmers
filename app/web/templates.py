"""页面模板与可复用组件（纯字符串渲染，无模板引擎）。

为什么不用 Jinja2：断网环境装不了；而本系统的页面结构简单、都由服务端拼好，
引入模板引擎带来的"灵活"用不上，反而多一个依赖与一套语法要维护。
代价是 HTML 与 Python 混在一起——所以所有变量一律经过 `esc()`，绝不用裸 f-string 拼用户输入。
"""
from __future__ import annotations

from html import escape

__all__ = [
    "esc",
    "layout",
    "card",
    "money",
    "button",
    "flash",
    "badge",
    "table",
    "field",
]

NAV = [
    ("/", "记一笔", "home"),
    ("/ledger", "账本", "ledger"),
    ("/summary", "看年账", "summary"),
    ("/reports", "出报表", "reports"),
    ("/privacy", "隐私", "privacy"),
    ("/admin", "管理", "admin"),
    ("/help", "帮助", "help"),
]

#: 需要管理员权限才显示的导航项
ADMIN_ONLY = {"/admin"}

_CSS_VERSION = "3"


def esc(value: object) -> str:
    return escape("" if value is None else str(value))


def money(fen: int | None) -> str:
    """界面金额：分 → 元，带千分位；负数保留符号。"""
    from core.money import fmt_yuan

    return fmt_yuan(int(fen or 0))


def layout(
    title: str,
    body: str,
    *,
    user: dict | None = None,
    active: str = "",
    message: str = "",
    message_kind: str = "info",
    say: str = "",
) -> str:
    is_admin = bool(user and user.get("role") == "admin")
    nav = "".join(
        f'<a class="nav-item{" active" if key == active else ""}" href="{href}">{esc(label)}</a>'
        for href, label, key in NAV
        if href not in ADMIN_ONLY or is_admin
    )
    if user:
        who = (
            f'<span class="who">{esc(user.get("display_name") or user.get("username"))}'
            f'<small>{esc({"admin": "管理员", "clerk": "经办人", "viewer": "只读"}.get(user.get("role"), ""))}</small></span>'
            '<form method="post" action="/logout" class="inline">'
            '<input type="hidden" name="_csrf" value="{csrf}"><button class="linklike">退出</button></form>'
        )
        who = who.replace("{csrf}", esc(user.get("csrf", "")))
        top = f'<header class="top"><div class="brand">乡村财务智管家</div><nav>{nav}</nav>{who}</header>'
    else:
        top = '<header class="top"><div class="brand">乡村财务智管家</div></header>'

    notice = ""
    if message:
        notice = f'<div class="flash {esc(message_kind)}" role="status">{esc(message)}</div>'
    say_html = f'<div id="say" data-text="{esc(say)}" hidden></div>' if say else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(title)} · 乡村财务智管家</title>
<link rel="stylesheet" href="/static/app.css?v={_CSS_VERSION}"/>
</head>
<body>
{top}
<main class="wrap">
{notice}
{say_html}
{body}
</main>
<footer class="foot">断网可用 · 数据不出村 · 金额以分为单位可复算</footer>
<script src="/static/app.js?v={_CSS_VERSION}" defer></script>
</body>
</html>"""


def flash(text: str, kind: str = "info") -> str:
    return f'<div class="flash {esc(kind)}" role="status">{esc(text)}</div>'


def card(title: str, body: str, *, cls: str = "", foot: str = "") -> str:
    foot_html = f'<div class="card-foot">{foot}</div>' if foot else ""
    return (
        f'<section class="card {esc(cls)}"><h2>{esc(title)}</h2>'
        f'<div class="card-body">{body}</div>{foot_html}</section>'
    )


def button(label: str, *, href: str = "", kind: str = "primary", name: str = "",
           value: str = "", cls: str = "") -> str:
    if href:
        return f'<a class="btn {esc(kind)} {esc(cls)}" href="{esc(href)}">{esc(label)}</a>'
    return (
        f'<button class="btn {esc(kind)} {esc(cls)}" type="submit"'
        f'{" name=" + esc(name) if name else ""}{" value=" + esc(value) if value else ""}>'
        f"{esc(label)}</button>"
    )


def badge(text: str, kind: str = "") -> str:
    return f'<span class="badge {esc(kind)}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list[str]], *, cls: str = "") -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f'<table class="grid {esc(cls)}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def field(
    label: str, name: str, *, value: str = "", type: str = "text", placeholder: str = "",
    hint: str = "", required: bool = False, options: list[tuple[str, str]] | None = None,
    rows: int = 0, autofocus: bool = False,
) -> str:
    """一个大字段。字号、间距、点击区都在 CSS 里按老年友好基线设定。"""
    req = '<span class="req">必填</span>' if required else ""
    common = (
        f'name="{esc(name)}" id="f-{esc(name)}"'
        f'{" required" if required else ""}{" autofocus" if autofocus else ""}'
        f' placeholder="{esc(placeholder)}"'
    )
    if options is not None:
        opts = "".join(
            f'<option value="{esc(v)}"{" selected" if str(v) == str(value) else ""}>{esc(t)}</option>'
            for v, t in options
        )
        control = f'<select {common}>{opts}</select>'
    elif rows:
        control = f'<textarea {common} rows="{rows}">{esc(value)}</textarea>'
    else:
        control = f'<input type="{esc(type)}" {common} value="{esc(value)}"/>'
    hint_html = f'<p class="hint">{esc(hint)}</p>' if hint else ""
    return (
        f'<div class="field"><label for="f-{esc(name)}">{esc(label)}{req}</label>'
        f"{control}{hint_html}</div>"
    )
