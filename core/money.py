"""金额与定点运算：全链路整数分，禁止 float 参与金额计算。

设计约束（见 docs/技术方案V2.md §5.3）：
1. 金额一律以「分」为单位的 int 存储与传递；
2. 乘除等可能产生小数的运算，用 Decimal 定点并显式 ROUND_HALF_UP；
3. 只在展示层做格式化，计算链路不舍入。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "fen",
    "yuan",
    "mul",
    "div",
    "fmt_yuan",
    "fmt_wan",
    "ratio",
]


def fen(value: object) -> int:
    """把元（int / str / Decimal）转成分，四舍五入到分。

    >>> fen(200)
    20000
    >>> fen("2.6")
    260
    >>> fen(Decimal("0.005"))
    1
    """
    if isinstance(value, float):
        raise TypeError("金额禁止使用 float，请传 str 或 Decimal")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:  # pragma: no cover - 防御
        raise ValueError(f"无法解析金额: {value!r}") from exc
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def yuan(amount_fen: int) -> Decimal:
    """分 → 元的 Decimal（精确两位）。"""
    if not isinstance(amount_fen, int):
        raise TypeError("amount_fen 必须是 int（分）")
    return (Decimal(amount_fen) / 100).quantize(Decimal("0.01"))


def _dec(value: object) -> Decimal:
    if isinstance(value, float):
        raise TypeError("禁止使用 float 参与金额运算，请传 str 或 Decimal")
    return Decimal(str(value))


def mul(amount_fen: int, factor: object) -> int:
    """分 × 系数（如 0.7 折算系数），四舍五入到分。"""
    return int((Decimal(amount_fen) * _dec(factor)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def div(amount_fen: int, divisor: object) -> int:
    """分 ÷ 除数，四舍五入到分；除数为 0 时显式报错而非静默返回 0。"""
    d = _dec(divisor)
    if d == 0:
        raise ZeroDivisionError("除数不能为 0（保本价/亩均计算需先校验分母）")
    return int((Decimal(amount_fen) / d).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def ratio(numerator_fen: int, denominator_fen: int) -> Decimal:
    """比例（用于成本结构占比），保留 4 位小数。"""
    if denominator_fen == 0:
        return Decimal("0.0000")
    return (Decimal(numerator_fen) / Decimal(denominator_fen)).quantize(Decimal("0.0001"))


def fmt_yuan(amount_fen: int, thousand: bool = True) -> str:
    """分 → '1,234.56' 形式，供报表使用。"""
    d = yuan(amount_fen)
    sign = "-" if d < 0 else ""
    d = abs(d)
    raw = f"{d:.2f}"
    if not thousand:
        return sign + raw
    whole, _, frac = raw.partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}{','.join(groups)}.{frac}"


def fmt_wan(amount_fen: int) -> str:
    """分 → '1.23 万' 形式，供摘要页大字展示。"""
    d = Decimal(amount_fen) / Decimal(1000000)  # 分 -> 万元
    d = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d} 万"
