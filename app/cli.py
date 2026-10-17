"""命令行入口：部署、备份、验证、自检。

村委实际只用两条命令：
    python -m app.cli init      # 第一次：建账、建管理员
    python -m app.cli serve     # 以后：启动，双击 启动.bat 就是这个

其余命令给技术维护与答辩现场使用（验证、导出、自检、备份）。
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from . import repo
from .audit_svc import AuditService
from .backup import create_backup, list_backups, restore_backup
from .calc import seed_packs
from .clock import now_iso
from .config import Config
from .db import connect, init_db, transaction
from .errors import AppError
from .ids import token_for
from .keys import KeyStore, hash_password
from .packs import DEFAULT_PARAM_PACK, DEFAULT_RULE_PACK
from .report_svc import ReportService
from .selfcheck import run_selfcheck
from .snapshot_svc import SnapshotService

__all__ = ["main"]


def _cfg(args) -> Config:
    return Config(data_dir=Path(args.data).expanduser().resolve())


def _connect(args):
    cfg = _cfg(args)
    init_db(cfg)
    keys = KeyStore.open(cfg, getattr(args, "passphrase", None) or None)
    return cfg, keys, connect(cfg)


def _ask(prompt: str, default: str) -> str:
    """交互式询问；非交互环境（管道/CI）直接取默认值，绝不阻塞。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return default
        answer = input(f"{prompt}（默认 {default}）：").strip()
        return answer or default
    except (EOFError, KeyboardInterrupt):
        return default


def cmd_init(args) -> int:
    cfg = _cfg(args)
    init_db(cfg)
    keys = KeyStore.open(cfg)
    conn = connect(cfg)
    if repo.get_village(conn) and not args.force:
        print(f"已经初始化过了：{cfg.data_dir}")
        return 0

    village_name = args.village or _ask("村名", "试点村")
    region = args.region or _ask("区域代码（县/区，6 位）", "4401xx")
    village_id = args.village_id or f"V-{region}-001"
    with transaction(conn):
        repo.upsert_village(
            conn, village_id, village_name, region,
            DEFAULT_RULE_PACK["version"], DEFAULT_PARAM_PACK["version"], now_iso(),
        )
        seed_packs(conn, now_iso())
        AuditService(conn, village_id).append("system", "village.init", village_id,
                                              {"name": village_name, "region": region})

    if not repo.get_user_by_name(conn, args.admin):
        password = args.password or getpass.getpass("设置管理员口令（至少 8 位）：")
        if len(password) < 8:
            print("口令太短（至少 8 位）", file=sys.stderr)
            return 2
        digest, salt = hash_password(password)
        with transaction(conn):
            repo.create_user(conn, args.admin, args.admin_display, "admin", digest, salt, now_iso())
            AuditService(conn, village_id).append("system", "user.create", args.admin, {"role": "admin"})

    print("=" * 56)
    print("初始化完成")
    print(f"  村：{village_name}（{region}）")
    print(f"  数据目录：{cfg.data_dir}")
    print(f"  设备标识：{keys.device_id}")
    print(f"  管理员：{args.admin}")
    print("下一步：python -m app.cli serve")
    print("=" * 56)
    return 0


def cmd_serve(args) -> int:
    from .web.server import serve

    cfg = _cfg(args)
    init_db(cfg)
    keys = KeyStore.open(cfg)
    conn = connect(cfg)
    if not repo.get_village(conn):
        print("还没有初始化：先运行 python -m app.cli init", file=sys.stderr)
        return 2
    host = "0.0.0.0" if args.lan else cfg.host
    if args.lan:
        print("⚠ 已开启局域网访问：请确认只在村内网使用，切勿映射到公网。")
    serve(cfg, keys, port=args.port, host=host, open_browser=not args.no_browser)
    return 0


def cmd_demo(args) -> int:
    """造一套可直接演示的数据（10 亩水稻，一年账）。"""
    cfg = _cfg(args)
    init_db(cfg)
    keys = KeyStore.open(cfg)
    conn = connect(cfg)
    ts = now_iso()
    village_id = "V-4401-001"
    with transaction(conn):
        repo.upsert_village(conn, village_id, args.village, "4401xx",
                            DEFAULT_RULE_PACK["version"], DEFAULT_PARAM_PACK["version"], ts)
        seed_packs(conn, ts)
        if not repo.list_farmers(conn):
            token = token_for(village_id, 1, keys.token_secret)
            fid = repo.create_farmer(
                conn, token, keys.seal_text("张三", aad=b"farmer-name"), ts, group_name="3 组"
            )
            repo.create_plot(conn, fid, "8", "owned", "水稻", "门口田", 0, ts)
            repo.create_plot(conn, fid, "2", "rented", "水稻", "租来的田", 0, ts)
            p1 = repo.list_plots(conn, fid)[0]["id"]
            year = args.year
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
                    "farmer_id": fid, "plot_id": p1, "date": d, "type": etype, "category": cat,
                    "amount_fen": amount, "qty": qty, "unit": unit, "created_by": "demo",
                    "created_at": ts, "state": "confirmed", "source": "manual",
                })
            repo.save_machinery_use(conn, fid, str(year), "旋耕机", "12", 0, 1, ts)
            AuditService(conn, village_id).append("system", "demo.seed", token, {"year": year})
    print(f"演示数据已写入 {cfg.data_dir}")
    print("现在可以：python -m app.cli serve")
    return 0


def cmd_backup(args) -> int:
    cfg = _cfg(args)
    passphrase = args.passphrase or (getpass.getpass("备份口令（留空则不加密）：") if args.encrypt else "")
    path = create_backup(cfg, passphrase or "")
    print(f"备份完成：{path}")
    print("提醒：备份含设备密钥，U 盘请上锁保管。")
    return 0


def cmd_restore(args) -> int:
    cfg = _cfg(args)
    restore_backup(cfg, args.file, args.passphrase or "", overwrite=args.force)
    print(f"已恢复到 {cfg.data_dir}")
    return 0


def cmd_snapshot(args) -> int:
    cfg, keys, conn = _connect(args)
    farmer = repo.get_farmer(conn, args.farmer)
    if not farmer:
        print("农户不存在", file=sys.stderr)
        return 2
    snap = SnapshotService(conn, cfg, keys, "cli").create(int(farmer["id"]), args.period)
    print(f"已生成快照 {snap['id']}")
    print(f"  Merkle 根：{snap['merkle_root']}")
    print(f"  签名：{snap['signature'][:32]}…")
    return 0


def cmd_verify(args) -> int:
    from .verify import VerificationError, verify_file

    try:
        result = verify_file(args.snapshot, args.inputs)
    except VerificationError as exc:
        print(f"验证失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n结论：" + ("通过（签名有效，且重算一致）" if result["ok"] else "不通过"))
    return 0 if result["ok"] else 1


def cmd_selfcheck(args) -> int:
    cfg, keys, conn = _connect(args)
    result = run_selfcheck(conn, cfg, keys)
    for item in result["items"]:
        flag = "✓" if item["ok"] else "✗"
        print(f"{flag} {item['name']}: {item['detail']}" + (f"（{item['hint']}）" if item["hint"] else ""))
    print("\n总评：" + ("全部通过" if result["ok"] else "存在问题"))
    return 0 if result["ok"] else 1


def cmd_export(args) -> int:
    cfg, keys, conn = _connect(args)
    svc = ReportService(conn, cfg, keys, "cli")
    result = svc.export(args.snapshot, args.template, args.format)
    target = Path(args.out).expanduser().resolve() if args.out else result.path
    if args.out:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(result.path, target)
    print(f"已导出：{target}")
    return 0


def cmd_reset_password(args) -> int:
    cfg, keys, conn = _connect(args)
    user = repo.get_user_by_name(conn, args.username)
    if not user:
        print("账号不存在", file=sys.stderr)
        return 2
    password = args.password or getpass.getpass("新口令：")
    if len(password) < 8:
        print("口令太短（至少 8 位）", file=sys.stderr)
        return 2
    digest, salt = hash_password(password)
    with transaction(conn):
        repo.update_user_password(conn, int(user["id"]), digest, salt)
        AuditService(conn).append("cli", "user.reset_password", args.username, {})
    print("已重置口令")
    return 0


def cmd_protect_keys(args) -> int:
    cfg = _cfg(args)
    passphrase = args.passphrase or getpass.getpass("设备密钥口令：")
    if len(passphrase) < 8:
        print("口令太短（至少 8 位）", file=sys.stderr)
        return 2
    KeyStore.protect(cfg, passphrase)
    print("已为设备密钥设置口令保护。以后启动需要输入该口令（或设置环境变量 NFM_KEY_PASSPHRASE）。")
    return 0


def cmd_backups(args) -> int:
    for item in list_backups(_cfg(args)):
        print(f"{item['name']:52s} {item['size'] // 1024:8d} KB  "
              f"{'加密' if item['encrypted'] else '未加密'}  {item['modified_at']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="乡村财务智管家 · 命令行")
    p.add_argument("--data", default="data", help="数据目录（默认 ./data）")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="初始化村与管理员")
    i.add_argument("--village", default=None, help="村名（不填会交互询问）")
    i.add_argument("--village-id", default="")
    i.add_argument("--region", default=None, help="区域代码，如 4401xx（不填会交互询问）")
    i.add_argument("--admin", default="admin")
    i.add_argument("--admin-display", default="村委管理员")
    i.add_argument("--password", default="")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="启动本地服务")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--lan", action="store_true", help="允许村内局域网访问（默认仅本机）")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("demo", help="写入一套演示数据")
    d.add_argument("--village", default="新围村")
    d.add_argument("--year", type=int, default=date.today().year)
    d.set_defaults(func=cmd_demo)

    b = sub.add_parser("backup", help="备份数据")
    b.add_argument("--encrypt", action="store_true", help="用口令加密备份")
    b.add_argument("--passphrase", default="")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="从备份恢复")
    r.add_argument("file")
    r.add_argument("--passphrase", default="")
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_restore)

    sn = sub.add_parser("snapshot", help="生成并签章一份年度快照")
    sn.add_argument("--farmer", type=int, default=1)
    sn.add_argument("--period", default=str(date.today().year))
    sn.set_defaults(func=cmd_snapshot)

    v = sub.add_parser("verify", help="验证快照（可离线）")
    v.add_argument("snapshot")
    v.add_argument("--inputs", default=None)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("selfcheck", help="系统自检")
    c.set_defaults(func=cmd_selfcheck)

    e = sub.add_parser("export", help="导出报表")
    e.add_argument("snapshot")
    e.add_argument("--template", default="T1-v1")
    e.add_argument("--format", default="html", choices=["html", "xlsx", "zip"])
    e.add_argument("--out", default="")
    e.set_defaults(func=cmd_export)

    rp = sub.add_parser("reset-password", help="重置账号口令")
    rp.add_argument("username")
    rp.add_argument("--password", default="")
    rp.set_defaults(func=cmd_reset_password)

    pk = sub.add_parser("protect-keys", help="为设备密钥设置口令保护")
    pk.add_argument("--passphrase", default="")
    pk.set_defaults(func=cmd_protect_keys)

    bl = sub.add_parser("backups", help="列出备份")
    bl.set_defaults(func=cmd_backups)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except AppError as exc:
        print(f"失败：{exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
