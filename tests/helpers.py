"""测试夹具：在临时目录里起一套完整的应用与本地 HTTP 服务。

集成测试直接走 HTTP，而不是只调领域服务——因为真正会坏的往往不是业务逻辑，
而是路由、会话、CSRF、表单编码这些"胶水"。
"""
from __future__ import annotations

import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

from app import repo
from app.calc import seed_packs
from app.clock import now_iso
from app.config import Config
from app.db import connect, init_db, transaction
from app.ids import token_for
from app.keys import KeyStore, hash_password
from app.packs import DEFAULT_PARAM_PACK, DEFAULT_RULE_PACK
from app.web.server import App, _Handler, _Server

__all__ = ["TestEnv", "build_env", "build_data", "Client", "login"]

ADMIN_USER = "admin"
ADMIN_PASSWORD = "village-2026"


@dataclass
class TestEnv:
    cfg: Config
    keys: KeyStore
    app: App
    httpd: _Server
    base: str
    data_dir: Path
    thread: threading.Thread | None = None

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=3)

    def conn(self):
        return connect(self.cfg)


class Client:
    """带 Cookie 的最小 HTTP 客户端（http.client 实现）。

    不用 urllib 的 CookieJar：它只在 2xx 响应上处理 Set-Cookie，而本站登录成功
    返回 302——用 CookieJar 会"登录成功但拿不到会话"，测试会误判成功能坏了。
    """

    def __init__(self, base: str) -> None:
        from urllib.parse import urlsplit

        parts = urlsplit(base)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.base = base.rstrip("/")
        self.cookies: dict[str, str] = {}
        self.last_status = 0
        self.last_location = ""
        self.last_body = ""

    def get(self, path: str, follow: bool = True):
        return self._request("GET", path, follow=follow)

    def post(self, path: str, data: dict | None = None, follow: bool = True):
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        return self._request("POST", path, body=body, follow=follow)

    def post_json(self, path: str, data: dict, csrf: str = "", follow: bool = True):
        """JSON 请求：CSRF 走 X-CSRF-Token 头，和前端 JS 的约定一致。"""
        import json

        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return self._request(
            "POST", path, body=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            follow=follow, extra_headers=headers,
        )

    def post_multipart(
        self, path: str, fields: dict[str, str], files: dict[str, tuple[str, bytes]],
        follow: bool = True,
    ):
        """multipart 上传：模拟浏览器带票据照片提交记账表单。"""
        import secrets

        boundary = "----nfm" + secrets.token_hex(8)
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode("utf-8")
            )
        for name, (filename, data) in files.items():
            chunks.append(
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode("utf-8")
            )
            chunks.append(data + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        return self._request(
            "POST", path, body=b"".join(chunks), follow=follow,
            extra_headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    def _request(self, method: str, path: str, *, body: bytes | None = None, follow: bool = True,
                 extra_headers: dict | None = None):
        import http.client

        if path.startswith("http"):
            from urllib.parse import urlsplit

            parts = urlsplit(path)
            path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=20)
        headers = dict(extra_headers or {})
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read().decode("utf-8", "replace")
            self.last_status = resp.status
            self.last_location = resp.getheader("Location") or ""
            self.last_body = payload
            for key, value in resp.getheaders():
                if key.lower() == "set-cookie":
                    pair = value.split(";", 1)[0]
                    if "=" in pair:
                        name, val = pair.split("=", 1)
                        if val in ("", "deleted"):
                            self.cookies.pop(name.strip(), None)
                        else:
                            self.cookies[name.strip()] = val
        finally:
            conn.close()
        if follow and self.last_status in (301, 302, 303, 307) and self.last_location:
            return self._request("GET", self.last_location, follow=False)
        return self

    @property
    def csrf(self) -> str:
        import re

        match = re.search(r'name="_csrf" value="([^"]+)"', self.last_body)
        return match.group(1) if match else ""


def build_env(
    *,
    farmers: int = 1,
    events: bool = True,
    year: int | None = None,
) -> TestEnv:
    from app.clock import today

    year = year or today().year
    data_dir = Path(mkdtemp(prefix="nfm-test-"))
    cfg = Config(data_dir=data_dir)
    init_db(cfg)
    keys = KeyStore.open(cfg)

    conn = connect(cfg)
    ts = now_iso()
    with transaction(conn):
        repo.upsert_village(
            conn, "V-TEST-001", "测试村", "4401xx",
            DEFAULT_RULE_PACK["version"], DEFAULT_PARAM_PACK["version"], ts,
        )
        seed_packs(conn, ts)
        digest, salt = hash_password(ADMIN_PASSWORD)
        repo.create_user(conn, ADMIN_USER, "村委管理员", "admin", digest, salt, ts)
        clerk_digest, clerk_salt = hash_password("clerk-2026")
        repo.create_user(conn, "clerk", "经办人小李", "clerk", clerk_digest, clerk_salt, ts)

        for i in range(farmers):
            token = token_for("V-TEST-001", i + 1, keys.token_secret)
            fid = repo.create_farmer(
                conn, token, keys.seal_text(f"农户{i + 1}", aad=b"farmer-name"), ts,
                group_name=f"{i + 1} 组",
                phone_enc=keys.seal_text("13800138000", aad=b"farmer-phone"),
            )
            repo.create_plot(conn, fid, "8", "owned", "水稻", "门口田", 0, ts)
            repo.create_plot(conn, fid, "2", "rented", "水稻", "租来的田", 0, ts)
            if events:
                p1 = repo.list_plots(conn, fid)[0]["id"]
                rows = [
                    (f"{year}-04-12", "income", "主粮销售", 1_352_000, "5200", "kg"),
                    (f"{year}-09-20", "income", "补贴", 30_000, None, None),
                    (f"{year}-04-12", "variable_cost", "化肥", 200_000, "500", "kg"),
                    (f"{year}-04-12", "variable_cost", "种子", 80_000, None, None),
                    (f"{year}-05-03", "variable_cost", "农药", 60_000, None, None),
                    (f"{year}-07-18", "variable_cost", "机械作业费", 150_000, None, None),
                    (f"{year}-08-02", "variable_cost", "运输", 40_000, None, None),
                    (f"{year}-01-10", "fixed_cost", "土地租金", 160_000, None, None),
                ]
                for d, etype, cat, amount, qty, unit in rows:
                    repo.insert_event(conn, {
                        "farmer_id": fid, "plot_id": p1, "date": d, "type": etype,
                        "category": cat, "amount_fen": amount, "qty": qty, "unit": unit,
                        "created_by": "test", "created_at": ts, "state": "confirmed",
                        "source": "manual",
                    })
                repo.save_machinery_use(conn, fid, str(year), "旋耕机", "12", 0, 1, ts)
    conn.close()

    app = App(cfg, keys)
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.app = app  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    env = TestEnv(
        cfg=cfg, keys=keys, app=app, httpd=httpd,
        base=f"http://{host}:{port}", data_dir=data_dir, thread=thread,
    )
    return env


def build_data(*, farmers: int = 1, events: bool = True, year: int | None = None):
    """只建数据、不起 HTTP 服务，供领域服务测试用。"""
    from app.clock import today

    year = year or today().year
    data_dir = Path(mkdtemp(prefix="nfm-data-"))
    cfg = Config(data_dir=data_dir)
    init_db(cfg)
    keys = KeyStore.open(cfg)

    conn = connect(cfg)
    ts = now_iso()
    with transaction(conn):
        repo.upsert_village(
            conn, "V-TEST-001", "测试村", "4401xx",
            DEFAULT_RULE_PACK["version"], DEFAULT_PARAM_PACK["version"], ts,
        )
        seed_packs(conn, ts)
        digest, salt = hash_password(ADMIN_PASSWORD)
        repo.create_user(conn, ADMIN_USER, "村委管理员", "admin", digest, salt, ts)
        ids = []
        for i in range(farmers):
            token = token_for("V-TEST-001", i + 1, keys.token_secret)
            fid = repo.create_farmer(
                conn, token, keys.seal_text(f"农户{i + 1}", aad=b"farmer-name"), ts,
                group_name=f"{i + 1} 组",
                phone_enc=keys.seal_text("13800138000", aad=b"farmer-phone"),
            )
            ids.append(fid)
            repo.create_plot(conn, fid, "8", "owned", "水稻", "门口田", 0, ts)
            repo.create_plot(conn, fid, "2", "rented", "水稻", "租来的田", 0, ts)
            if events:
                p1 = repo.list_plots(conn, fid)[0]["id"]
                rows = [
                    (f"{year}-04-12", "income", "主粮销售", 1_352_000, "5200", "kg"),
                    (f"{year}-09-20", "income", "补贴", 30_000, None, None),
                    (f"{year}-04-12", "variable_cost", "化肥", 200_000, "500", "kg"),
                    (f"{year}-04-12", "variable_cost", "种子", 80_000, None, None),
                    (f"{year}-05-03", "variable_cost", "农药", 60_000, None, None),
                    (f"{year}-07-18", "variable_cost", "机械作业费", 150_000, None, None),
                    (f"{year}-08-02", "variable_cost", "运输", 40_000, None, None),
                    (f"{year}-01-10", "fixed_cost", "土地租金", 160_000, None, None),
                ]
                for d, etype, cat, amount, qty, unit in rows:
                    repo.insert_event(conn, {
                        "farmer_id": fid, "plot_id": p1, "date": d, "type": etype,
                        "category": cat, "amount_fen": amount, "qty": qty, "unit": unit,
                        "created_by": "test", "created_at": ts, "state": "confirmed",
                        "source": "manual",
                    })
                repo.save_machinery_use(conn, fid, str(year), "旋耕机", "12", 0, 1, ts)
    return cfg, keys, conn, ids


def login(client: Client, username: str = ADMIN_USER, password: str = ADMIN_PASSWORD) -> Client:
    client.get("/login")
    client.post("/login", {"username": username, "password": password})
    return client
