"""数据访问层（SQLite）。

只做"取/存"，不含业务判断：校验在 domain 层，权限在 web 层。这样同一套仓储
既能被 Web 调用，也能被 CLI 与测试直接使用，不用起 HTTP 服务就能跑集成测试。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from .db import next_counter
from .ids import event_id

__all__ = [
    "row_to_dict",
    "get_village",
    "upsert_village",
    "create_farmer",
    "list_farmers",
    "get_farmer",
    "create_plot",
    "list_plots",
    "save_machinery_use",
    "list_machinery_use",
    "insert_event",
    "get_event",
    "list_events",
    "set_event_state",
    "update_event",
    "delete_event",
    "count_events",
    "save_param_pack",
    "get_param_pack",
    "list_param_packs",
    "save_rule_pack",
    "get_rule_pack",
    "list_rule_packs",
    "insert_snapshot",
    "list_snapshots",
    "get_snapshot",
    "get_events_by_ids",
    "insert_report",
    "list_reports",
    "audit_rows",
    "append_audit_row",
    "create_user",
    "get_user",
    "get_user_by_name",
    "list_users",
    "update_user_password",
    "set_user_disabled",
    "create_session",
    "get_session",
    "delete_session",
    "purge_expired_sessions",
    "insert_outbound",
    "list_outbound",
    "get_counter",
]


def row_to_dict(row: Any) -> dict:
    return dict(row) if row is not None else {}


# ---------- 村 ----------

def get_village(conn) -> dict:
    row = conn.execute("SELECT * FROM village LIMIT 1").fetchone()
    return row_to_dict(row)


def upsert_village(
    conn, village_id: str, name: str, region_code: str, rule_version: str,
    param_version: str, created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO village(id, name, region_code, rule_version, param_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, region_code=excluded.region_code, "
        "rule_version=excluded.rule_version, param_version=excluded.param_version",
        (village_id, name, region_code, rule_version, param_version, created_at),
    )


# ---------- 农户 ----------

def create_farmer(
    conn, token: str, name_enc: bytes, created_at: str, group_name: str = "",
    phone_enc: bytes | None = None, note: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO farmer(token, name_enc, group_name, phone_enc, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, name_enc, group_name or None, phone_enc, note or None, created_at),
    )
    return int(cur.lastrowid)


def list_farmers(conn, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM farmer"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY id"
    return [row_to_dict(r) for r in conn.execute(sql)]


def get_farmer(conn, farmer_id: int) -> dict:
    row = conn.execute("SELECT * FROM farmer WHERE id = ?", (farmer_id,)).fetchone()
    return row_to_dict(row)


# ---------- 地块 ----------

def create_plot(
    conn, farmer_id: int, area_mu: str, tenure: str, crop: str,
    name: str, rent_fen_per_mu: int, created_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO plot(farmer_id, name, area_mu, tenure, crop, rent_fen_per_mu, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (farmer_id, name or None, area_mu, tenure, crop, rent_fen_per_mu, created_at),
    )
    return int(cur.lastrowid)


def list_plots(conn, farmer_id: int | None = None) -> list[dict]:
    if farmer_id is None:
        rows = conn.execute("SELECT * FROM plot ORDER BY farmer_id, id")
    else:
        rows = conn.execute("SELECT * FROM plot WHERE farmer_id = ? ORDER BY id", (farmer_id,))
    return [row_to_dict(r) for r in rows]


# ---------- 农机作业量 ----------

def save_machinery_use(
    conn, farmer_id: int, period: str, name: str, mu: str,
    prior_depreciation_fen: int, year_index: int, updated_at: str,
) -> None:
    conn.execute(
        "INSERT INTO machinery_use(farmer_id, period, name, mu, prior_depreciation_fen, "
        "year_index, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(farmer_id, period, name) DO UPDATE SET mu=excluded.mu, "
        "prior_depreciation_fen=excluded.prior_depreciation_fen, "
        "year_index=excluded.year_index, updated_at=excluded.updated_at",
        (farmer_id, period, name, mu, prior_depreciation_fen, year_index, updated_at),
    )


def list_machinery_use(conn, farmer_id: int, period: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM machinery_use WHERE farmer_id = ? AND period = ? ORDER BY name",
        (farmer_id, period),
    )
    return [row_to_dict(r) for r in rows]


# ---------- 事件 ----------

def insert_event(conn, fields: dict) -> str:
    """分配事件号并落库。调用方须在事务内。"""
    seq = next_counter(conn, "event")
    eid = event_id(seq)
    conn.execute(
        "INSERT INTO event(id, seq, farmer_id, plot_id, date, type, category, amount_fen, qty, "
        "unit, counterparty_token, confidence, evidence_id, source, state, estimated, note, "
        "created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid, seq, fields["farmer_id"], fields.get("plot_id"), fields["date"], fields["type"],
            fields["category"], fields["amount_fen"], fields.get("qty"), fields.get("unit"),
            fields.get("counterparty_token"), fields.get("confidence", 1.0),
            fields.get("evidence_id"), fields.get("source", "manual"),
            fields.get("state", "draft"), 1 if fields.get("estimated") else 0,
            fields.get("note"), fields["created_by"], fields["created_at"], fields["created_at"],
        ),
    )
    return eid


def get_event(conn, eid: str) -> dict:
    row = conn.execute("SELECT * FROM event WHERE id = ?", (eid,)).fetchone()
    return row_to_dict(row)


def list_events(
    conn,
    farmer_id: int | None = None,
    state: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_voided: bool = False,
    limit: int | None = None,
) -> list[dict]:
    where: list[str] = []
    args: list[Any] = []
    if farmer_id is not None:
        where.append("farmer_id = ?")
        args.append(farmer_id)
    if state:
        where.append("state = ?")
        args.append(state)
    elif not include_voided:
        where.append("state != 'voided'")
    if date_from:
        where.append("date >= ?")
        args.append(date_from)
    if date_to:
        where.append("date <= ?")
        args.append(date_to)
    sql = "SELECT * FROM event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [row_to_dict(r) for r in conn.execute(sql, args)]


def set_event_state(conn, eid: str, state: str, updated_at: str) -> None:
    conn.execute("UPDATE event SET state = ?, updated_at = ? WHERE id = ?", (state, updated_at, eid))


def update_event(conn, eid: str, fields: dict, updated_at: str) -> None:
    allowed = {
        "date", "type", "category", "amount_fen", "qty", "unit", "counterparty_token",
        "confidence", "evidence_id", "source", "state", "estimated", "note", "plot_id",
    }
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "estimated":
            value = 1 if value else 0
        sets.append(f"{key} = ?")
        args.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    args.extend([updated_at, eid])
    conn.execute(f"UPDATE event SET {', '.join(sets)} WHERE id = ?", args)


def delete_event(conn, eid: str) -> None:
    conn.execute("DELETE FROM event WHERE id = ?", (eid,))


def count_events(conn, farmer_id: int | None = None, state: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM event"
    where, args = [], []
    if farmer_id is not None:
        where.append("farmer_id = ?")
        args.append(farmer_id)
    if state:
        where.append("state = ?")
        args.append(state)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return int(conn.execute(sql, args).fetchone()["n"])


# ---------- 参数包 / 规则包 ----------

def save_param_pack(conn, version: str, region_code: str, data: dict, created_at: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO param_pack(version, region_code, json, created_at, note) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(region_code, version) DO NOTHING",
        (version, region_code, json.dumps(data, ensure_ascii=False), created_at, note or None),
    )


def get_param_pack(conn, region_code: str, version: str) -> dict:
    row = conn.execute(
        "SELECT * FROM param_pack WHERE region_code = ? AND version = ?", (region_code, version)
    ).fetchone()
    if not row:
        return {}
    out = row_to_dict(row)
    out["data"] = json.loads(out.pop("json"))
    return out


def list_param_packs(conn, region_code: str | None = None) -> list[dict]:
    if region_code:
        rows = conn.execute(
            "SELECT version, region_code, created_at, note FROM param_pack "
            "WHERE region_code = ? ORDER BY created_at DESC", (region_code,)
        )
    else:
        rows = conn.execute(
            "SELECT version, region_code, created_at, note FROM param_pack ORDER BY created_at DESC"
        )
    return [row_to_dict(r) for r in rows]


def save_rule_pack(conn, version: str, data: dict, effective_from: str, created_at: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO rule_pack(version, json, effective_from, created_at, note) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(version) DO NOTHING",
        (version, json.dumps(data, ensure_ascii=False), effective_from, created_at, note or None),
    )


def get_rule_pack(conn, version: str) -> dict:
    row = conn.execute("SELECT * FROM rule_pack WHERE version = ?", (version,)).fetchone()
    if not row:
        return {}
    out = row_to_dict(row)
    out["data"] = json.loads(out.pop("json"))
    return out


def list_rule_packs(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT version, effective_from, created_at, note FROM rule_pack ORDER BY created_at DESC"
    )
    return [row_to_dict(r) for r in rows]


# ---------- 快照 / 报表 ----------

def insert_snapshot(conn, fields: dict) -> None:
    conn.execute(
        "INSERT INTO snapshot(id, period, farmer_id, event_ids_json, inputs_json, event_set_hash, "
        "merkle_root, rule_version, param_version, output_json, snapshot_json, signature, public_key, "
        "signed_by, device_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fields["id"], fields["period"], fields["farmer_id"], fields["event_ids_json"],
            fields["inputs_json"], fields["event_set_hash"], fields["merkle_root"],
            fields["rule_version"], fields["param_version"], fields["output_json"],
            fields["snapshot_json"], fields.get("signature"), fields.get("public_key"),
            fields["signed_by"], fields.get("device_id"), fields["created_at"],
        ),
    )


def list_snapshots(conn, farmer_id: int | None = None, limit: int = 50) -> list[dict]:
    if farmer_id is None:
        rows = conn.execute(
            "SELECT id, period, farmer_id, merkle_root, signed_by, created_at FROM snapshot "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        )
    else:
        rows = conn.execute(
            "SELECT id, period, farmer_id, merkle_root, signed_by, created_at FROM snapshot "
            "WHERE farmer_id = ? ORDER BY created_at DESC LIMIT ?", (farmer_id, limit)
        )
    return [row_to_dict(r) for r in rows]


def get_snapshot(conn, sid: str) -> dict:
    row = conn.execute("SELECT * FROM snapshot WHERE id = ?", (sid,)).fetchone()
    return row_to_dict(row)


def get_events_by_ids(conn, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT * FROM event WHERE id IN ({marks})", ids)
    return [row_to_dict(r) for r in rows]


def insert_report(conn, fields: dict) -> None:
    conn.execute(
        "INSERT INTO report(id, snapshot_id, template, format, rel_path, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            fields["id"], fields["snapshot_id"], fields["template"], fields["format"],
            fields["rel_path"], fields["created_at"], fields["created_by"],
        ),
    )


def list_reports(conn, snapshot_id: str | None = None, limit: int = 50) -> list[dict]:
    if snapshot_id:
        rows = conn.execute(
            "SELECT * FROM report WHERE snapshot_id = ? ORDER BY created_at DESC", (snapshot_id,)
        )
    else:
        rows = conn.execute("SELECT * FROM report ORDER BY created_at DESC LIMIT ?", (limit,))
    return [row_to_dict(r) for r in rows]


# ---------- 审计 ----------

def audit_rows(conn) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]


def append_audit_row(conn, entry: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log(seq, ts, actor, action, object_ref, payload_json, payload_digest, "
        "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            entry["seq"], entry["ts"], entry["actor"], entry["action"], entry["object_ref"],
            json.dumps(entry.get("payload", {}), ensure_ascii=False, sort_keys=True),
            entry["payload_digest"], entry["prev_hash"], entry["hash"],
        ),
    )


# ---------- 账号 ----------

def create_user(
    conn, username: str, display_name: str, role: str, password_hash: str, salt: str, created_at: str
) -> int:
    cur = conn.execute(
        "INSERT INTO user(username, display_name, role, password_hash, salt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (username, display_name, role, password_hash, salt, created_at),
    )
    return int(cur.lastrowid)


def get_user(conn, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM user WHERE id = ?", (user_id,)).fetchone()
    return row_to_dict(row)


def get_user_by_name(conn, username: str) -> dict:
    row = conn.execute("SELECT * FROM user WHERE username = ?", (username,)).fetchone()
    return row_to_dict(row)


def list_users(conn) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute("SELECT * FROM user ORDER BY id")]


def update_user_password(conn, user_id: int, password_hash: str, salt: str) -> None:
    conn.execute("UPDATE user SET password_hash = ?, salt = ? WHERE id = ?", (password_hash, salt, user_id))


def set_user_disabled(conn, user_id: int, disabled: bool) -> None:
    conn.execute("UPDATE user SET disabled = ? WHERE id = ?", (1 if disabled else 0, user_id))


# ---------- 会话 ----------

def create_session(conn, token: str, user_id: int, csrf: str, created_at: str, expires_at: str, ua: str = "") -> None:
    conn.execute(
        "INSERT INTO session(token, user_id, csrf, created_at, expires_at, user_agent) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf, created_at, expires_at, ua[:300]),
    )


def get_session(conn, token: str) -> dict:
    row = conn.execute("SELECT * FROM session WHERE token = ?", (token,)).fetchone()
    return row_to_dict(row)


def delete_session(conn, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token = ?", (token,))


def purge_expired_sessions(conn, now_iso: str) -> None:
    conn.execute("DELETE FROM session WHERE expires_at < ?", (now_iso,))


# ---------- 外发 ----------

def insert_outbound(conn, fields: dict) -> int:
    cur = conn.execute(
        "INSERT INTO outbound(created_at, actor, purpose, recipient, snapshot_id, fields_json, "
        "rel_path, decision, audit_seq, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fields["created_at"], fields["actor"], fields["purpose"], fields["recipient"],
            fields.get("snapshot_id"), json.dumps(fields.get("fields", []), ensure_ascii=False),
            fields.get("rel_path"), fields["decision"], fields.get("audit_seq"), fields.get("note"),
        ),
    )
    return int(cur.lastrowid)


def list_outbound(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute("SELECT * FROM outbound ORDER BY created_at DESC LIMIT ?", (limit,))
    return [row_to_dict(r) for r in rows]


def get_counter(conn, name: str) -> int:
    row = conn.execute("SELECT value FROM counter WHERE name = ?", (name,)).fetchone()
    return int(row["value"]) if row else 0
