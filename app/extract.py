"""规则抽取器：把一句口语变成事件草稿（**不含任何模型**）。

对应 docs/技术方案V2.md §7.2 的抽取契约。模型层不在本期交付范围内，但"人工
一句话记账"这件事不必等模型：日期、金额、类别、数量大多是强模式，规则能做到
可解释、可单测、零内存占用。模型将来接入时替换的是本模块的实现，**输出契约不变**。

输出每个字段都带 `conf` 与 `span`，低于阈值进 `needs_confirm`，界面只问那一个点
（§7.3 人机协同规则）。规则拿不准的一律不给数——宁可让用户填，不得猜。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .packs import CATEGORY_ALIASES, CATEGORY_CATALOG, guess_type, normalize_category

__all__ = ["extract", "ExtractedField", "ExtractResult", "parse_chinese_number", "AMOUNT_CONF_MIN"]

AMOUNT_CONF_MIN = 0.85
CATEGORY_CONF_MIN = 0.80
DATE_CONF_MIN = 0.70

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_CN_NUM_CHARS = "零〇一二两三四五六七八九十百千万"

_ALL_CATEGORIES = tuple(c for names in CATEGORY_CATALOG.values() for c in names)


def parse_chinese_number(text: str) -> Decimal | None:
    """解析「两百」「一千二」「二十三」「十五」这类口语数字。

    规则：按单位分段累加；"一千二" 这种省略末尾单位的写法按 "一千二百" 理解
    （口语里就是这个意思）。解析不了返回 None，不猜。
    """
    if not text:
        return None
    text = text.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return Decimal(text)

    total = 0
    section = 0
    number = 0
    last_unit = 0
    zero_after_unit = False
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            if ch in ("零", "〇") and last_unit:
                zero_after_unit = True
        elif ch in ("十", "百", "千"):
            unit = _CN_UNITS[ch]
            if number == 0 and ch == "十":
                number = 1
            section += number * unit
            number = 0
            last_unit = unit
            zero_after_unit = False
        elif ch == "万":
            section += number
            total += section * 10000
            section = 0
            number = 0
            last_unit = 10000
            zero_after_unit = False
        else:
            return None
    if number and 100 <= last_unit < 10000 and not zero_after_unit:
        # "一千二" = 1200，"三百五" = 350；但"两千零五" = 2005，不能套用
        number *= max(1, last_unit // 10)
    return Decimal(total + section + number)


@dataclass
class ExtractedField:
    value: object
    conf: float
    span: tuple[int, int] | None = None
    alternatives: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out: dict = {"value": self.value, "conf": round(self.conf, 2)}
        if self.span:
            out["span"] = list(self.span)
        if self.alternatives:
            out["alternatives"] = self.alternatives
        return out


@dataclass
class ExtractResult:
    date: ExtractedField
    amount_fen: ExtractedField
    category: ExtractedField
    event_type: ExtractedField
    qty: ExtractedField | None = None
    unit: str | None = None
    note: str = ""
    needs_confirm: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {
            "date": self.date.as_dict(),
            "amount": self.amount_fen.as_dict(),
            "category": self.category.as_dict(),
            "type": self.event_type.as_dict(),
            "needs_confirm": list(self.needs_confirm),
            "note": self.note,
        }
        if self.qty is not None:
            out["qty"] = self.qty.as_dict()
            out["unit"] = self.unit
        return out


_AMOUNT_RE = re.compile(
    r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]+)\s*"
    r"(元|块钱|块|圆|角|毛)"
)
_BARE_NUM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])")
_QTY_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?|[" + _CN_NUM_CHARS + r"]+)\s*"
    r"(公斤|千克|kg|KG|斤|吨|亩|袋|包|瓶|桶|箱|小时|个|只)"
)
_ISO_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?")
_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})[日号](?!\d)")


def _extract_date(text: str, ref: date) -> ExtractedField:
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return ExtractedField(d.isoformat(), 0.97, m.span())
        except ValueError:
            pass
    m = _MD_RE.search(text)
    if m:
        try:
            d = date(ref.year, int(m.group(1)), int(m.group(2)))
            # 记账都是历史账：说"12月20日"时若已跨年，指去年
            if d > ref + timedelta(days=31):
                d = date(ref.year - 1, int(m.group(1)), int(m.group(2)))
            return ExtractedField(d.isoformat(), 0.93, m.span())
        except ValueError:
            pass
    relative = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2, "大前天": -3}
    for word, delta in relative.items():
        idx = text.find(word)
        if idx != -1:
            return ExtractedField((ref + timedelta(days=delta)).isoformat(), 0.96, (idx, idx + len(word)))
    m = _DAY_RE.search(text)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                d = date(ref.year, ref.month, day)
                if d > ref:
                    prev = ref.replace(day=1) - timedelta(days=1)
                    d = date(prev.year, prev.month, day)
                return ExtractedField(d.isoformat(), 0.80, m.span())
            except ValueError:
                pass
    return ExtractedField(ref.isoformat(), 0.55, None)


def _extract_amount(text: str) -> tuple[ExtractedField, tuple[int, int] | None]:
    m = _AMOUNT_RE.search(text)
    if m:
        raw = m.group(1)
        value = parse_chinese_number(raw) if any(c in _CN_NUM_CHARS for c in raw) else None
        if value is None:
            try:
                value = Decimal(raw.replace(",", ""))
            except Exception:  # noqa: BLE001
                value = None
        if value is not None:
            unit = m.group(2)
            fen = int((value * 100).to_integral_value())
            if unit in ("角",):
                fen = int((value * 10).to_integral_value())
            elif unit in ("毛",):
                fen = int((value * 10).to_integral_value())
            return ExtractedField(fen, 0.95, m.span()), m.span()
    # 没有"元/块"后缀：只给候选，置信度低，强制确认
    m = _BARE_NUM_RE.search(text)
    if m:
        value = Decimal(m.group(1))
        return ExtractedField(int((value * 100).to_integral_value()), 0.55, m.span()), m.span()
    return ExtractedField(0, 0.0, None), None


def _extract_category(text: str, event_type: str) -> tuple[ExtractedField, str]:
    # 先找目录里的标准科目名（如"化肥""雇工"本身就是标准科目）
    catalog_hits: list[tuple[int, str, str]] = []
    for name in _ALL_CATEGORIES:
        idx = text.find(name)
        if idx != -1:
            catalog_hits.append((idx, name, name))
    alias_hits: list[tuple[int, str, str]] = []
    for alias, name in CATEGORY_ALIASES.items():
        idx = text.find(alias)
        if idx != -1:
            alias_hits.append((idx, name, alias))

    hits = catalog_hits + alias_hits
    if hits:
        # 最长匹配优先：既认"化肥"也认"复合肥"，但"复合肥"更具体
        hits.sort(key=lambda h: (-len(h[2]), h[0]))
        idx, canonical, matched = hits[0]
        alternatives = sorted({h[1] for h in hits[1:4]} - {canonical})
        conf = 0.92 if any(h[1] == canonical and len(h[2]) > 1 for h in hits) else 0.88
        return ExtractedField(canonical, conf, (idx, idx + len(matched)), alternatives[:2]), canonical
    normalized = normalize_category(text, event_type)
    if normalized and normalized != text.strip():
        return ExtractedField(normalized, 0.75, None), normalized
    return ExtractedField("", 0.0, None), ""


def _extract_qty(text: str) -> tuple[ExtractedField | None, str | None]:
    m = _QTY_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1)
    value = parse_chinese_number(raw) if any(c in _CN_NUM_CHARS for c in raw) else None
    if value is None:
        try:
            value = Decimal(raw)
        except Exception:  # noqa: BLE001
            return None, None
    unit = m.group(2)
    unit = {"KG": "kg", "千克": "kg", "公斤": "kg"}.get(unit, unit)
    return ExtractedField(f"{value:f}", 0.90, m.span()), unit


def extract(text: str, reference_date: date | None = None) -> ExtractResult:
    """抽取一句话。`reference_date` 显式传入，保证同一句话可复现同一结果。"""
    raw = (text or "").strip()
    ref = reference_date or date.today()
    event_type = guess_type(raw)

    date_field = _extract_date(raw, ref)
    amount_field, amount_span = _extract_amount(raw)
    category_field, _ = _extract_category(raw, event_type)
    qty_field, unit = _extract_qty(raw)

    # 数量/日期里的数字不能被当成金额：如 "500公斤" 没有金额时，
    # 不能把 500 记成 500 元；"2026年" 里的 2026 也不能。
    if amount_span:
        a0, a1 = amount_span
        blocked = []
        if qty_field and qty_field.span:
            blocked.append(qty_field.span)
        if date_field.span:
            blocked.append(date_field.span)
        for (b0, b1) in blocked:
            if a0 >= b0 and a1 <= b1:
                amount_field = ExtractedField(0, 0.0, None)
                amount_span = None
                break

    needs: list[str] = []
    if amount_field.conf < AMOUNT_CONF_MIN:
        needs.append("amount")
    if category_field.conf < CATEGORY_CONF_MIN:
        needs.append("category")
    if date_field.conf < DATE_CONF_MIN:
        needs.append("date")

    type_field = ExtractedField(event_type, 0.85, None)
    return ExtractResult(
        date=date_field,
        amount_fen=amount_field,
        category=category_field,
        event_type=type_field,
        qty=qty_field,
        unit=unit,
        note=raw,
        needs_confirm=needs,
    )
