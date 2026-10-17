"""极简本地 Web 框架：路由、表单、会话、CSRF、静态文件、错误页。

安全基线（村委局域网环境）：
- 只监听 127.0.0.1（除非显式 `--lan`，并在启动时打印风险提示）；
- 会话 Cookie：HttpOnly + SameSite=Strict + Path=/；
- 所有写操作校验 CSRF（表单隐藏字段或 X-CSRF-Token 头）；
- 登录失败按用户名 + 来源地址限速，5 次锁 5 分钟；
- 响应带 CSP / X-Frame-Options / nosniff，避免被内网其他页面套框。
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import socket
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .. import repo
from ..clock import now_iso
from ..config import Config
from ..db import connect, init_db
from ..errors import AppError, PermissionDenied, UserError
from ..ids import new_csrf_token, new_session_token

__all__ = ["App", "Request", "Response", "serve", "MAX_BODY"]

MAX_BODY = 25 * 1024 * 1024
SESSION_TTL_SECONDS = 12 * 3600

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass
class Request:
    app: "App"
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    cookies: dict[str, str]
    form: dict[str, str] = field(default_factory=dict)
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    user: dict | None = None
    remote: str = ""
    conn: object = None

    @property
    def is_json(self) -> bool:
        return "application/json" in self.headers.get("content-type", "")

    def q(self, name: str, default: str = "") -> str:
        return self.query.get(name, default)

    def json(self) -> dict:
        try:
            data = json.loads(self.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UserError("请求内容不是合法的 JSON") from exc
        if not isinstance(data, dict):
            raise UserError("请求内容必须是 JSON 对象")
        return data


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def html(cls, html: str, status: int = 200) -> "Response":
        return cls(status=status, body=html.encode("utf-8"), content_type="text/html; charset=utf-8")

    @classmethod
    def json(cls, data: object, status: int = 200) -> "Response":
        return cls(
            status=status,
            body=json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(status=302, body=b"", headers={"Location": location})

    @classmethod
    def bytes(cls, data: bytes, content_type: str, filename: str = "") -> "Response":
        headers = {}
        if filename:
            from urllib.parse import quote

            headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        return cls(body=data, content_type=content_type, headers=headers)


class _LoginLimiter:
    """登录限速：内存实现即可，重启后清零可接受（攻击者也要重启服务）。"""

    def __init__(self, limit: int = 5, window: int = 300) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            self._hits[key] = hits
            return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._hits.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


class App:
    def __init__(self, cfg: Config, keys) -> None:
        self.cfg = cfg
        self.keys = keys
        self.routes: list[tuple[str, re.Pattern, Callable, bool, tuple[str, ...]]] = []
        self.limiter = _LoginLimiter()
        self.static_dir = Path(__file__).with_name("static")
        from . import admin_views, api, views

        views.register(self)
        admin_views.register(self)
        api.register(self)

    # ---------- 路由 ----------

    def route(
        self, method: str, pattern: str, *, auth: bool = True, roles: tuple[str, ...] = ()
    ) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.routes.append(
                (method.upper(), re.compile(f"^{pattern}$"), func, auth, roles)
            )
            return func

        return decorator

    def get(self, pattern: str, **kw) -> Callable:
        return self.route("GET", pattern, **kw)

    def post(self, pattern: str, **kw) -> Callable:
        return self.route("POST", pattern, **kw)

    # ---------- 会话 ----------

    def current_user(self, conn, cookies: dict[str, str]) -> dict | None:
        token = cookies.get("nfm_sid", "")
        if not token:
            return None
        row = repo.get_session(conn, token)
        if not row:
            return None
        if row["expires_at"] < now_iso():
            repo.delete_session(conn, token)
            return None
        user = repo.get_user(conn, int(row["user_id"]))
        if not user or user.get("disabled"):
            return None
        user = dict(user)
        user.pop("password_hash", None)
        user.pop("salt", None)
        user["csrf"] = row["csrf"]
        user["session_token"] = token
        return user

    def _check_csrf(self, req: Request) -> None:
        if req.method in _SAFE_METHODS:
            return
        token = (
            req.form.get("_csrf")
            or req.headers.get("x-csrf-token", "")
            or req.query.get("_csrf", "")
        )
        expected = (req.user or {}).get("csrf", "")
        if not expected or not secrets.compare_digest(token, expected):
            raise PermissionDenied("页面已过期，请刷新后重试", detail="csrf")

    # ---------- 分发 ----------

    def dispatch(self, req: Request) -> Response:
        conn = connect(self.cfg)
        req.conn = conn
        try:
            req.user = self.current_user(conn, req.cookies)
            for method, pattern, func, auth, roles in self.routes:
                if method != req.method:
                    continue
                match = pattern.match(req.path)
                if not match:
                    continue
                if auth and not req.user:
                    if req.is_json or req.path.startswith("/api/"):
                        return Response.json({"error": "请先登录"}, status=401)
                    return Response.redirect("/login")
                if roles and (not req.user or req.user.get("role") not in roles):
                    raise PermissionDenied("当前账号没有这个权限")
                # 未登录的路由（登录页）没有会话也就没有 CSRF 令牌，跳过校验；
                # 其余写操作一律要求令牌与会话中的一致。
                if auth:
                    self._check_csrf(req)
                result = func(req, **match.groupdict())
                if isinstance(result, Response):
                    return result
                if isinstance(result, str):
                    return Response.html(result)
                return Response.json(result)
            if req.path.startswith("/static/"):
                return self._static(req)
            if req.path.startswith("/files/"):
                return self._files(req)
            return self._not_found(req)
        except AppError as exc:
            return self._error(req, exc.status, exc.message, exc.detail)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._error(req, 500, "系统出错了，这笔记账没有保存。请重试或联系管理员。", "internal")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 静态与文件 ----------

    def _static(self, req: Request) -> Response:
        rel = urllib.parse.unquote(req.path[len("/static/") :])
        target = (self.static_dir / rel).resolve()
        if not str(target).startswith(str(self.static_dir.resolve())) or not target.is_file():
            return Response(status=404, body=b"not found", content_type="text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(body=target.read_bytes(), content_type=ctype,
                        headers={"Cache-Control": "public, max-age=300"})

    def _files(self, req: Request) -> Response:
        if not req.user:
            return Response.redirect("/login")
        rel = urllib.parse.unquote(req.path[len("/files/") :])
        base = {
            "exports/": self.cfg.export_dir,
            "evidence/": self.cfg.evidence_dir,
        }
        for prefix, root in base.items():
            if rel.startswith(prefix):
                name = rel[len(prefix) :]
                target = (root / name).resolve()
                if not str(target).startswith(str(root.resolve())) or not target.is_file():
                    return self._not_found(req)
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return Response(body=target.read_bytes(), content_type=ctype)
        return self._not_found(req)

    # ---------- 错误页 ----------

    def _not_found(self, req: Request) -> Response:
        if req.is_json or req.path.startswith("/api/"):
            return Response.json({"error": "没有这个接口"}, status=404)
        from .templates import layout

        return Response.html(
            layout("找不到页面", "<div class='card'><h2>找不到这个页面</h2>"
                    "<p>可能是链接过期了。回到 <a href='/'>首页</a> 继续。</p></div>"),
            status=404,
        )

    def _error(self, req: Request, status: int, message: str, detail: str = "") -> Response:
        if req.is_json or req.path.startswith("/api/"):
            return Response.json({"error": message, "detail": detail}, status=status)
        from .templates import layout

        body = (
            f"<div class='card'><h2>操作没有完成</h2><p class='err'>{message}</p>"
            "<p><a class='btn primary' href='javascript:history.back()'>返回上一页</a> "
            "<a class='btn' href='/'>回首页</a></p></div>"
        )
        return Response.html(layout("操作没有完成", body, user=req.user), status=status)


# ---------- 请求解析 ----------

def _parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = urllib.parse.unquote(v.strip())
    return out


def _parse_multipart(body: bytes, boundary: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    form: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delimiter = b"--" + boundary
    for chunk in body.split(delimiter):
        if not chunk or chunk in (b"--\r\n", b"--", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk.startswith(b"--"):
            break
        head, _, data = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data[: -2] if data.endswith(b"\r\n") else data
        text = head.decode("utf-8", "replace")
        name_match = re.search(r'name="([^"]*)"', text)
        if not name_match:
            continue
        name = name_match.group(1)
        file_match = re.search(r'filename="([^"]*)"', text)
        if file_match:
            files[name] = (file_match.group(1), data)
        else:
            form[name] = data.decode("utf-8", "replace")
    return form, files


class _Handler(BaseHTTPRequestHandler):
    server_version = "NFM/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        return

    def _serve(self) -> None:
        app: App = self.server.app  # type: ignore[attr-defined]
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in self.headers.items()}

        form: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        ctype = headers.get("content-type", "")
        if body:
            if ctype.startswith("application/x-www-form-urlencoded"):
                form = {
                    k: v[0]
                    for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()
                }
            elif ctype.startswith("multipart/form-data"):
                match = re.search(r"boundary=([^;]+)", ctype)
                if match:
                    form, files = _parse_multipart(body, match.group(1).strip('"').encode())

        req = Request(
            app=app,
            method=self.command,
            path=urllib.parse.unquote(parsed.path),
            query=query,
            headers=headers,
            body=body,
            cookies=_parse_cookies(headers.get("cookie", "")),
            form=form,
            files=files,
            remote=self.client_address[0],
        )
        started = time.time()
        try:
            resp = app.dispatch(req)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            resp = Response(status=500, body=b"internal error", content_type="text/plain; charset=utf-8")

        payload = resp.body
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        elapsed = (time.time() - started) * 1000
        if elapsed > 500:
            print(f"[slow] {self.command} {parsed.path} {elapsed:.0f}ms", flush=True)

    do_GET = _serve
    do_POST = _serve
    do_HEAD = _serve


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(cfg: Config, keys, *, port: int | None = None, host: str | None = None,
          open_browser: bool = True, ready=None) -> None:
    init_db(cfg)
    app = App(cfg, keys)
    bind_host = host or cfg.host
    bind_port = port or cfg.port
    httpd = _Server((bind_host, bind_port), _Handler)
    httpd.app = app  # type: ignore[attr-defined]
    url = f"http://{'127.0.0.1' if bind_host in ('0.0.0.0', '::') else bind_host}:{bind_port}/"
    print("=" * 60)
    print("乡村财务智管家 · 本地服务已启动")
    print(f"  地址：{url}")
    print(f"  数据目录：{cfg.data_dir}")
    hosts = {h.strip() for h in socket.gethostbyname_ex(socket.gethostname())[2]} if bind_host in ("0.0.0.0", "::") else set()
    if hosts:
        print(f"  局域网访问：{', '.join(sorted(hosts))}（仅限村内网，切勿映射到公网）")
    print("  停止：按 Ctrl+C")
    print("=" * 60, flush=True)
    if ready:
        ready(url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
