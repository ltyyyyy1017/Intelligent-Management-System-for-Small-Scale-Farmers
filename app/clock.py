"""统一取时。

不直接在业务代码里写 `datetime.now()` 的原因：快照、审计链、报表脚注里的
时间都必须能被打桩（测试里固定时间），否则"可复算"就只是句口号。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date

__all__ = ["now", "now_iso", "today", "period_of", "current_period", "parse_date", "parse_iso"]

# 乡村场景固定东八区：村委电脑时区被人改错时，账目时间也不能跟着漂
CST = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(tz=CST)


def now_iso() -> str:
    """带时区的秒级时间戳，如 2026-04-12T09:30:00+08:00。"""
    return now().isoformat(timespec="seconds")


def today() -> date:
    return now().date()


def period_of(d: date) -> str:
    """归期：年度账，期 = 'YYYY'。"""
    return f"{d.year:04d}"


def current_period() -> str:
    return period_of(today())


def parse_date(text: str) -> date:
    return date.fromisoformat(text)


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)
