"""系统自检：把"能不能安心用"变成一张可打印的清单。

对应 docs/技术方案V2.md §4.1 的 L5 运维层。自检不是走过场：它检查的每一条都是
真的会坏的东西——磁盘满了、密钥对不上、审计链断、凭证被换、规则包缺失。
"""
from __future__ import annotations

import shutil

from . import repo
from .audit_svc import AuditService
from .clock import now_iso
from .config import Config
from .evidence import EvidenceService
from .signing import load_signing_key
from .snapshot_svc import SnapshotService

__all__ = ["run_selfcheck"]


def _item(name: str, ok: bool, detail: str = "", hint: str = "", critical: bool = True) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "hint": hint, "critical": critical}


def run_selfcheck(conn, cfg: Config, keys) -> dict:
    items: list[dict] = []

    # 1) 数据库完整性
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = str(row[0]) if row else "unknown"
        items.append(_item("数据库完整性", result == "ok", result,
                           "备份数据后联系技术人员" if result != "ok" else ""))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("数据库完整性", False, str(exc)))

    # 2) 村信息与参数包
    village = repo.get_village(conn)
    items.append(_item("村信息", bool(village), village.get("name", "未初始化"),
                       "运行 init 命令建立村信息" if not village else ""))
    if village:
        pp = repo.get_param_pack(conn, village["region_code"], village["param_version"])
        rp = repo.get_rule_pack(conn, village["rule_version"])
        items.append(_item("参数包", bool(pp), f"{village['region_code']}/{village['param_version']}"))
        items.append(_item("规则包", bool(rp), village["rule_version"]))

    # 3) 审计链
    try:
        audit = AuditService(conn).verify()
        items.append(_item("审计链", audit["ok"], f"{audit.get('length', 0)} 条",
                           "" if audit["ok"] else f"断点 #{audit.get('broken_at')}：{audit.get('reason')}"))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("审计链", False, str(exc)))

    # 4) 签章密钥
    try:
        priv = load_signing_key(cfg, keys)
        items.append(_item("签章密钥", True, f"公钥 {priv.public_key.to_hex()[:16]}…"))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("签章密钥", False, str(exc), "恢复 keys/ 目录或重新生成（历史签名将无法复验）"))

    # 5) 凭证抽样
    try:
        ev = EvidenceService(conn, cfg, keys, "selfcheck")
        ids = [r["id"] for r in ev.list(limit=20)]
        bad = [eid for eid in ids if not ev.verify(eid)["ok"]]
        items.append(_item("凭证完整性", not bad, f"抽检 {len(ids)} 份，异常 {len(bad)} 份",
                           "凭证文件可能被移动或修改" if bad else ""))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("凭证完整性", False, str(exc)))

    # 6) 最近快照重放
    try:
        snaps = repo.list_snapshots(conn, limit=3)
        svc = SnapshotService(conn, cfg, keys, "selfcheck")
        results = [svc.verify(s["id"]) for s in snaps]
        ok = all(r["ok"] for r in results) if results else True
        detail = f"检查 {len(results)} 个" if results else "暂无快照"
        items.append(_item("快照可重放", ok, detail, "重放失败说明数据或代码被改动，立即停用并排查"))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("快照可重放", False, str(exc)))

    # 7) 磁盘空间
    try:
        usage = shutil.disk_usage(str(cfg.data_dir))
        free_mb = usage.free // (1024 * 1024)
        items.append(_item("磁盘剩余空间", free_mb >= 200, f"{free_mb} MB",
                           "空间不足 200 MB，请清理备份或扩容" if free_mb < 200 else ""))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("磁盘剩余空间", False, str(exc)))

    # 8) 最近备份
    try:
        from .backup import list_backups

        backups = list_backups(cfg)
        if backups:
            items.append(_item("最近备份", True, f"{backups[0]['name']}（{backups[0]['modified_at']}）"))
        else:
            # 没备份不算"系统坏了"，但必须提示——这是最常见的运维缺口
            items.append(_item("最近备份", False, "尚未备份", "建议每周备份一次到 U 盘", critical=False))
    except Exception as exc:  # noqa: BLE001
        items.append(_item("最近备份", False, str(exc), critical=False))

    return {
        "generated_at": now_iso(),
        "ok": all(i["ok"] for i in items if i.get("critical", True)),
        "items": items,
    }
