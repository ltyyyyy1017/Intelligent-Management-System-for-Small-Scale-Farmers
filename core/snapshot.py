"""快照、Merkle 根与重放校验。

对应 docs/技术方案V2.md §5.4、§8.4、§8.5。

哈希与签名的分工：
- 摘要用 **SM3**（GB/T 32905），与生产口径一致，不再用 SHA-256 占位；
- 快照本身只产出**待签字节**（`signing_bytes`），SM2 私钥运算不在这里做——
  私钥属于部署环境（UKey/加密密钥文件），核算内核不接触密钥。

重放校验是这套方案的技术支点：同一份事件集 + 同一版本规则，重算必须**逐字节**
一致。不一致就意味着"数据被改"或"代码被改"，两者都必须拒绝出具报表。
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Iterable, Sequence

from .costing import compute
from .crypto.sm3 import sm3, sm3_hex
from .models import Event, MachineryUse, Params, Plot, RulePack

__all__ = [
    "DIGEST_ALG",
    "canonical_events",
    "event_set_hash",
    "merkle_root",
    "merkle_leaves",
    "make_snapshot",
    "signing_bytes",
    "snapshot_digest",
    "attach_signature",
    "replay",
]

DIGEST_ALG = "SM3"


def _digest(data: bytes) -> str:
    return sm3_hex(data)


def canonical_events(events: Iterable[Event]) -> bytes:
    """事件集规范化序列化：按 (date, id) 排序，逐条 canonical()，换行分隔。

    排序保证"同一组事件"不管录入口味如何，哈希一致；逐条 canonical 保证
    字段顺序固定、金额为整数，不受 JSON 键序或浮点格式影响。
    """
    ordered = sorted(events, key=lambda e: (e.date, e.id))
    return "\n".join(e.canonical() for e in ordered).encode("utf-8")


def event_set_hash(events: Iterable[Event]) -> str:
    return _digest(canonical_events(events))


def merkle_leaves(events: Iterable[Event]) -> list[str]:
    return [
        _digest(e.canonical().encode("utf-8"))
        for e in sorted(events, key=lambda e: (e.date, e.id))
    ]


def merkle_root(leaves: Sequence[str]) -> str:
    """事件批次 Merkle 根。空集返回全零哈希；奇数节点复制自身。"""
    if not leaves:
        return "0" * 64
    level = [bytes.fromhex(x) for x in leaves]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            sm3(level[i] + level[i + 1])
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"不可序列化类型 {type(obj)!r}")


def make_snapshot(
    result: dict,
    events: Sequence[Event],
    signed_by: str,
    period: str,
    created_at: str = "",
    device_id: str = "",
    extra: dict | None = None,
) -> dict:
    """生成不可变快照：输入哈希 + 版本 + 输出摘要 + Merkle 根 + 待签载荷。

    `created_at` 由调用方传入（不读时钟），保证同输入同输出；
    真实业务里它是"出表时间"，重放时保留原值、不参与重算比对。
    `device_id` 在签名**之前**写入载荷，签章因此同时绑定人与设备。
    """
    payload = {
        "period": period,
        "created_at": created_at,
        "digest_alg": DIGEST_ALG,
        "event_set_hash": event_set_hash(events),
        "merkle_root": merkle_root(merkle_leaves(events)),
        "event_count": len(events),
        "rule_version": result["meta"]["rule_version"],
        "param_version": result["meta"]["param_version"],
        "region_code": result["meta"]["region_code"],
        "output_hash": _digest(
            json.dumps(result, sort_keys=True, ensure_ascii=False, default=_json_default).encode("utf-8")
        ),
        "signed_by": signed_by,
    }
    if device_id:
        payload["device_id"] = device_id
    if extra:
        payload["extra"] = extra
    payload["signature_payload_hash"] = _digest(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return payload


def signing_bytes(snapshot: dict) -> bytes:
    """待签字节：不含 signature 字段的规范化 JSON。

    银行侧要做的是"用村委公钥验证 signature 覆盖了这些字节"，因此这里的序列化
    规则必须稳定、可被第三方独立复现（sort_keys + UTF-8 + 无多余空白）。
    """
    body = {k: v for k, v in snapshot.items() if k not in ("signature", "public_key")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def snapshot_digest(snapshot: dict) -> str:
    """待签字节的 SM3 摘要，供纸质报表二维码/脚注打印。"""
    return _digest(signing_bytes(snapshot))


def attach_signature(snapshot: dict, signature_hex: str, public_key_hex: str) -> dict:
    """把 SM2 签名与公钥挂到快照上，返回新 dict（不修改入参）。"""
    signed = dict(snapshot)
    signed["signature"] = signature_hex
    signed["public_key"] = public_key_hex
    return signed


def replay(
    snapshot: dict,
    result: dict,
    events: Sequence[Event],
    plots: Sequence[Plot],
    params: Params,
    rule: RulePack | None = None,
    machinery_use: Sequence[MachineryUse] = (),
) -> dict:
    """重放校验：用快照记录的输入重算，逐项比对。

    返回 {"ok": bool, "checks": {...}, "diff": [...]}。
    任何一项不一致都说明"数据被改"或"代码被改"，两者都必须拒绝出具报表。
    """
    recomputed = compute(events, plots, params, rule, machinery_use)
    checks = {
        "event_set_hash": event_set_hash(events) == snapshot["event_set_hash"],
        "merkle_root": merkle_root(merkle_leaves(events)) == snapshot["merkle_root"],
        "output": recomputed == result,
    }
    diff: list[str] = []
    if not checks["output"]:
        for key in sorted(set(result) | set(recomputed)):
            if result.get(key) != recomputed.get(key):
                diff.append(key)
    return {"ok": all(checks.values()), "checks": checks, "diff": diff, "recomputed": recomputed}
