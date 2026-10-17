"""独立的快照验证工具（银行/监管侧也可用）。

两条使用姿势：
1. 只有快照 JSON（银行拿到的材料包里就这一个文件）→ 验签 + 校验字段自洽；
2. 快照 + 输入（村委本机或系统内）→ 再重算一遍，逐项比对。

刻意不依赖数据库与应用状态：这个模块只认字节与数学，便于被第三方独立审阅、
也便于打包成一个几十行的脚本给银行 IT 看。
"""
from __future__ import annotations

import json
from decimal import Decimal

from core import snapshot as core_snapshot
from core.costing import compute

from .signing import verify_signature

__all__ = ["verify_exported", "verify_with_inputs", "verify_file", "VerificationError"]


class VerificationError(ValueError):
    pass


def verify_exported(snapshot: dict) -> dict:
    """只凭快照 JSON 能证明的部分：签名有效、关键字段齐全、摘要自洽。"""
    if not isinstance(snapshot, dict):
        raise VerificationError("快照必须是 JSON 对象")
    for field in ("period", "event_set_hash", "merkle_root", "output_hash", "signature", "public_key"):
        if not snapshot.get(field):
            raise VerificationError(f"快照缺少字段：{field}")

    payload = core_snapshot.signing_bytes(snapshot)
    signature_ok = verify_signature(snapshot["public_key"], payload, snapshot["signature"])

    # 快照自带的"待签载荷哈希"是否与当前字节一致。
    # 注意要与签发时的载荷口径一致：去掉签发后才追加的 signature/public_key，
    # 以及它自己——否则每次都会算出一个不同的值，误报为"被改动"。
    body = {
        k: v
        for k, v in snapshot.items()
        if k not in ("signature", "public_key", "signature_payload_hash")
    }
    body_hash = core_snapshot._digest(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    digests_ok = body_hash == snapshot.get("signature_payload_hash")

    return {
        "ok": bool(signature_ok and digests_ok),
        "signature_ok": bool(signature_ok),
        "digests_ok": bool(digests_ok),
        "signed_by": snapshot.get("signed_by", ""),
        "merkle_root": snapshot.get("merkle_root", ""),
        "event_set_hash": snapshot.get("event_set_hash", ""),
        "event_count": snapshot.get("event_count", ""),
        "created_at": snapshot.get("created_at", ""),
        "device_id": snapshot.get("device_id", ""),
        "note": (
            "签名有效：该快照确实由对应公钥签发且未被改动。"
            "要验证'报表忠实于账本'，还需在村委本机用原始输入重算（见 verify_with_inputs）。"
        ),
    }


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(type(obj))


def verify_with_inputs(snapshot: dict, inputs: dict) -> dict:
    """用快照 + 输入重算，逐项比对。inputs 结构与 SnapshotService 的 inputs_json 一致。"""
    from .snapshot_svc import deserialize_inputs

    data = deserialize_inputs(inputs)
    recomputed = compute(
        data["events"], data["plots"], data["params"], data["rule"], data["machinery_use"]
    )
    event_hash = core_snapshot.event_set_hash(data["events"])
    merkle = core_snapshot.merkle_root(core_snapshot.merkle_leaves(data["events"]))
    output_hash = core_snapshot._digest(
        json.dumps(recomputed, sort_keys=True, ensure_ascii=False, default=_json_default).encode("utf-8")
    )
    checks = {
        "event_set_hash": event_hash == snapshot.get("event_set_hash"),
        "merkle_root": merkle == snapshot.get("merkle_root"),
        "output_hash": output_hash == snapshot.get("output_hash"),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "recomputed_event_set_hash": event_hash,
        "recomputed_merkle_root": merkle,
        "recomputed_output_hash": output_hash,
    }


def verify_file(path: str, inputs_path: str | None = None) -> dict:
    try:
        snapshot = json.loads(open(path, "r", encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"读不了快照文件：{exc}") from exc
    snapshot = snapshot.get("snapshot", snapshot) if isinstance(snapshot, dict) else snapshot
    result = verify_exported(snapshot)
    if inputs_path:
        try:
            inputs = json.loads(open(inputs_path, "r", encoding="utf-8").read())
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationError(f"读不了输入文件：{exc}") from exc
        result["inputs"] = verify_with_inputs(snapshot, inputs)
        result["ok"] = bool(result["ok"] and result["inputs"]["ok"])
    return result
