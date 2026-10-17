"""快照、Merkle 根与重放校验。

对应 docs/技术方案V2.md §5.4、§8.4、§8.5。

哈希算法说明：生产实现使用国密 SM3（GB/T 32905）；Python 标准库不含 SM3，
参考实现用 SHA-256 等价替代，接口与流程完全一致，替换只需改 `_digest`。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Iterable, Sequence

from .models import Event, MachineryUse, Params, Plot, RulePack
from .costing import compute

__all__ = [
    "canonical_events",
    "event_set_hash",
    "merkle_root",
    "make_snapshot",
    "replay",
]


def _digest(data: bytes) -> str:
    """SM3 占位实现：生产替换为 sm3(data).hexdigest()。"""
    return hashlib.sha256(data).hexdigest()


def canonical_events(events: Iterable[Event]) -> bytes:
    """事件集规范化序列化：按 (date, id) 排序，逐条 canonical()，换行分隔。

    排序保证"同一组事件"不管录入口味如何，哈希一致；逐条 canonical 保证
    字段顺序固定、金额为整数，不受 JSON 键序或浮点格式影响。
    """
    ordered = sorted(events, key=lambda e: (e.date, e.id))
    return "\n".join(e.canonical() for e in ordered).encode("utf-8")


def event_set_hash(events: Iterable[Event]) -> str:
    return _digest(canonical_events(events))


def merkle_root(leaves: Sequence[str]) -> str:
    """事件批次 Merkle 根。空集返回全零哈希；奇数节点复制自身。"""
    if not leaves:
        return "0" * 64
    level = [bytes.fromhex(x) for x in leaves]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest()  # 与 _digest 同构
            for i in range(0, len(level), 2)
        ]
    return level[0].hex()


def _event_leaves(events: Iterable[Event]) -> list[str]:
    return [_digest(e.canonical().encode("utf-8")) for e in sorted(events, key=lambda e: (e.date, e.id))]


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"不可序列化类型 {type(obj)!r}")


def make_snapshot(
    result: dict,
    events: Sequence[Event],
    signed_by: str,
    period: str,
) -> dict:
    """生成不可变快照：输入哈希 + 版本 + 完整输出 + Merkle 根 + 签章载荷。

    签章本身（SM2 私钥运算）不在参考实现内——它依赖 UKey/密钥文件，属于部署环节。
    这里产出的是"待签章载荷"，字段与 §8.4 一致。
    """
    payload = {
        "period": period,
        "event_set_hash": event_set_hash(events),
        "merkle_root": merkle_root(_event_leaves(events)),
        "rule_version": result["meta"]["rule_version"],
        "param_version": result["meta"]["param_version"],
        "output_sha256": hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False, default=_json_default).encode("utf-8")
        ).hexdigest(),
        "signed_by": signed_by,
    }
    payload["signature_payload_sha256"] = _digest(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return payload


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
        "merkle_root": merkle_root(_event_leaves(events)) == snapshot["merkle_root"],
        "output": recomputed == result,
    }
    diff: list[str] = []
    if not checks["output"]:
        for key in sorted(set(result) | set(recomputed)):
            if result.get(key) != recomputed.get(key):
                diff.append(key)
    return {"ok": all(checks.values()), "checks": checks, "diff": diff, "recomputed": recomputed}
