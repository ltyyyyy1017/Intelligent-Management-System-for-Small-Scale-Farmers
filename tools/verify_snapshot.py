"""银行/监管侧的离线快照验证工具（单文件，不依赖本系统数据库）。

用法：
    python tools/verify_snapshot.py 快照.json
    python tools/verify_snapshot.py 快照.json --inputs 输入.json    # 连重算一起验

它能证明：这份快照确实由对应公钥签发、且未被改动。
它不能证明：农户没有造假账——那要靠抽验原始票据与交叉核对。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.verify import VerificationError, verify_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证一份快照文件（离线，不需要数据库）")
    parser.add_argument("snapshot", help="快照 JSON 文件（材料包里的 快照.json）")
    parser.add_argument("--inputs", default=None, help="可选：快照自带输入 JSON，用于重算比对")
    parser.add_argument("--json", action="store_true", help="只输出机器可读的 JSON")
    args = parser.parse_args(argv)

    try:
        result = verify_file(args.snapshot, args.inputs)
    except VerificationError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"验证失败：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=" * 58)
        print("快照验证结果")
        print("=" * 58)
        print(f"  SM2 签名      : {'有效' if result['signature_ok'] else '无效'}")
        print(f"  载荷摘要自洽  : {'是' if result['digests_ok'] else '否'}")
        print(f"  签章人        : {result.get('signed_by') or '—'}")
        print(f"  设备标识      : {result.get('device_id') or '—'}")
        print(f"  事件条数      : {result.get('event_count')}")
        print(f"  出表时间      : {result.get('created_at') or '—'}")
        print(f"  Merkle 根     : {result.get('merkle_root')}")
        if "inputs" in result:
            checks = result["inputs"]["checks"]
            print("  重算比对      : " + "、".join(
                f"{name}={'一致' if ok else '不一致'}" for name, ok in checks.items()
            ))
        print("-" * 58)
        print("结论：通过" if result["ok"] else "结论：不通过")
        print("说明：本结论证明账本未被事后修改、报表忠实于账本、签章人对此负责；")
        print("      不证明农户没有造假账（那属于业务风控，需要抽验原始票据）。")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
