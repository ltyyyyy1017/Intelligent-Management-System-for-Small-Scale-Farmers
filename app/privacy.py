"""隐私分级、脱敏与外发复检。

对应 docs/技术方案V2.md §8.1–§8.2。三条设计要点：

1. **L1 不是"脱敏后可以发"，而是结构上不可能出现在外发包里。**
   报表渲染器只从 L2/L3 白名单取字段，L1 字段在数据模型里就没有通往模板的路径。
2. **校验位是规则层的价值。** 身份证 ISO 7064、银行卡 Luhn、手机号段，
   把"看起来像号码"和"真的是号码"分开，显著降误报。
3. **外发必须复检最终字节流。** 单字段脱敏会漏掉组合泄露（村名 + 面积 + 作物
   在村内唯一可指），所以打包后再扫一遍，命中 L1 直接阻断。

本模块是纯函数：不读库、不读时钟，便于单独测试与在验证工具里复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

__all__ = [
    "L1",
    "L2",
    "L3",
    "Finding",
    "scan",
    "mask",
    "mask_text",
    "has_l1",
    "l1_findings",
    "validate_id_card",
    "validate_bank_card",
    "tokenize_name",
    "ensure_no_l1",
    "OUTBOUND_ALLOWED_LEVELS",
    "FIELD_LEVELS",
]

L1, L2, L3 = "L1", "L2", "L3"

#: 外发白名单：只允许 L2/L3（L1 永远不在白名单里）
OUTBOUND_ALLOWED_LEVELS = (L2, L3)


@dataclass(frozen=True)
class Finding:
    level: str
    kind: str
    start: int
    end: int
    text: str
    reason: str

    def masked(self) -> str:
        return mask(self.text, self.kind)


# ---- 校验位 ----

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def validate_id_card(text: str) -> bool:
    """GB 11643 公民身份号码校验（18 位）。校验位不对的一律不算 L1。"""
    if not re.fullmatch(r"\d{17}[\dXx]", text):
        return False
    body = text[:17]
    total = sum(int(ch) * w for ch, w in zip(body, _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == text[17].upper()


def validate_bank_card(text: str) -> bool:
    """Luhn 校验。13–19 位纯数字，且校验通过才算银行卡号。"""
    digits = text.replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\d{13,19}", digits):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# ---- 规则 ----

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")
_ID_RE = re.compile(r"(?<![\dXx])(\d{17}[\dXx])(?![\dXx])")
_BANK_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
_ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fa5]{1,8}(?:省|市|县|区|镇|乡)[\u4e00-\u9fa5]{0,12}?"
    r"(?:村|组|屯|号|街|路)[\u4e00-\u9fa5\d]{0,8}"
)
_GROUP_RE = re.compile(r"[\u4e00-\u9fa5]{1,6}村\s*[\d一二三四五六七八九十]+\s*组")

# 字段 → 级别（外发白名单直接从这张表生成，避免"以为过滤了其实没过滤"）
FIELD_LEVELS: dict[str, str] = {
    "id_card": L1, "bank_card": L1, "phone": L1, "real_name": L1,
    "address": L1, "plot_coord": L1, "face": L1,
    "village": L2, "group": L2, "area_mu": L2, "crop": L2, "counterparty": L2,
    "loan_purpose": L2, "plot_name": L2,
    "revenue": L3, "cost": L3, "net_income": L3, "yield_per_mu": L3,
    "breakeven_price": L3, "cost_structure": L3, "area_total_mu": L3,
}


def scan(text: str, known_names: Iterable[str] = (), include_l2: bool = True) -> list[Finding]:
    """扫描文本里的敏感实体。

    `known_names` 传入本村农户姓名（解密后的明文只在本进程内存中），
    用于兜住"规则抓不到、但确实是人名"的情况。名字不落日志、不回显。
    """
    if not text:
        return []
    findings: list[Finding] = []

    for m in _ID_RE.finditer(text):
        if validate_id_card(m.group(1)):
            findings.append(Finding(L1, "id_card", m.start(1), m.end(1), m.group(1), "身份证校验位通过"))

    for m in _PHONE_RE.finditer(text):
        findings.append(Finding(L1, "phone", m.start(), m.end(), m.group(0), "手机号段 + 11 位"))

    for m in _BANK_RE.finditer(text):
        raw = m.group(1)
        if validate_bank_card(raw) and not _overlaps(findings, m.start(1), m.end(1)):
            findings.append(Finding(L1, "bank_card", m.start(1), m.end(1), raw, "Luhn 校验通过"))

    for name in known_names:
        name = (name or "").strip()
        if len(name) < 2:
            continue
        start = text.find(name)
        while start != -1:
            if not _overlaps(findings, start, start + len(name)):
                findings.append(Finding(L1, "real_name", start, start + len(name), name, "本村农户姓名"))
            start = text.find(name, start + len(name))

    for m in _ADDRESS_RE.finditer(text):
        if not _overlaps(findings, m.start(), m.end()):
            findings.append(Finding(L1, "address", m.start(), m.end(), m.group(0), "含行政区划/门牌"))

    if include_l2:
        for m in _GROUP_RE.finditer(text):
            if not _overlaps(findings, m.start(), m.end()):
                findings.append(Finding(L2, "group", m.start(), m.end(), m.group(0), "村组定位"))

    return sorted(findings, key=lambda f: (f.start, f.end))


def _overlaps(findings: list[Finding], start: int, end: int) -> bool:
    return any(not (end <= f.start or start >= f.end) for f in findings)


# ---- 掩码 ----

def mask(text: str, kind: str) -> str:
    """按类型掩码。规则：保留能让人确认"是我那条"的最少信息。"""
    if not text:
        return ""
    n = len(text)
    if kind == "phone":
        digits = re.sub(r"\D", "", text)
        return digits[:3] + "*" * 4 + digits[-4:] if len(digits) >= 7 else "*" * n
    if kind == "id_card":
        return text[:6] + "*" * 8 + text[-4:]
    if kind == "bank_card":
        digits = re.sub(r"\D", "", text)
        return digits[:4] + "*" * max(0, len(digits) - 8) + digits[-4:]
    if kind == "real_name":
        return text[0] + "*" * max(1, n - 1)
    if kind in ("address", "group"):
        return text[:2] + "*" * max(0, n - 2)
    return "*" * n


def mask_text(text: str, known_names: Iterable[str] = ()) -> str:
    """把文本里所有 L1 实体替换为掩码，保持其余内容不变（界面显示用）。"""
    findings = [f for f in scan(text, known_names, include_l2=False) if f.level == L1]
    if not findings:
        return text
    out = []
    cursor = 0
    for f in findings:
        out.append(text[cursor : f.start])
        out.append(f.masked())
        cursor = f.end
    out.append(text[cursor:])
    return "".join(out)


def l1_findings(text: str, known_names: Iterable[str] = ()) -> list[Finding]:
    return [f for f in scan(text, known_names) if f.level == L1]


def has_l1(text: str, known_names: Iterable[str] = ()) -> bool:
    return bool(l1_findings(text, known_names))


def tokenize_name(name: str, secret: bytes, prefix: str = "T-") -> str:
    """对外标识：T- + 8 位 Base32。同一个名字在同一设备上稳定，跨村不可关联。"""
    from core.crypto.sm3 import sm3

    from .ids import BASE32

    digest = sm3(b"name|" + name.encode("utf-8") + secret)
    bits = int.from_bytes(digest[:5], "big")
    chars = []
    for _ in range(8):
        chars.append(BASE32[bits & 31])
        bits >>= 5
    return prefix + "".join(reversed(chars))


def ensure_no_l1(payload: str, known_names: Iterable[str] = ()) -> None:
    """外发前最后一道闸：命中 L1 直接抛错，调用方必须中止打包。"""
    hits = l1_findings(payload, known_names)
    if hits:
        from .errors import DataIntegrityError

        kinds = "、".join(sorted({h.kind for h in hits}))
        raise DataIntegrityError(
            f"外发内容里仍有 L1 直接标识（{kinds}），已阻止发送",
            detail="l1_leak",
        )
