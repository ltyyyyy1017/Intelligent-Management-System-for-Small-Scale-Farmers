"""Excel / CSV 批量导入（纯标准库读 xlsx）。

为什么自己读 xlsx：村委电脑装不了 openpyxl。xlsx 就是 zip + XML，这里只做
"取第一张表、读单元格值"这一件事，够用且可控。

导入三原则：
1. **默认进草稿**，不直接进正式账——批量数据最容易带错，让人在「待确认」里过一眼；
2. **坏行不静默丢弃**：逐行校验，失败的行带行号与原值返回，让经办人自己修；
3. **重复行跳过并计数**：同一农户同一天同一科目同一金额，多半是表里重复了。
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree import ElementTree as ET

from .errors import UserError, ValidationError
from .ledger import LedgerService, parse_amount_fen

__all__ = ["read_table", "import_rows", "IMPORT_COLUMNS", "ImportSummary"]

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# Excel 的日期序列起点是 1899-12-30（含 1900 闰年 bug 的历史沿用）
_EXCEL_EPOCH = date(1899, 12, 30)

IMPORT_COLUMNS = ("日期", "类型", "科目", "金额", "数量", "单位", "备注")

_TYPE_ALIASES = {
    "收入": "income", "卖": "income", "income": "income",
    "支出": "variable_cost", "花": "variable_cost", "买": "variable_cost",
    "变动成本": "variable_cost", "variable_cost": "variable_cost",
    "固定成本": "fixed_cost", "fixed_cost": "fixed_cost",
    "资产": "asset", "asset": "asset",
    "负债": "liability", "借款": "liability", "liability": "liability",
}


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    idx = 0
    for ch in letters.group(1):
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def _text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    return [_text(si) for si in root.findall(f"{_NS}si")]


def _first_sheet(z: zipfile.ZipFile) -> str:
    names = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    if not names:
        raise UserError("这个 Excel 里没有工作表")
    names.sort(key=lambda n: int(re.search(r"sheet(\d+)", n).group(1)))
    return names[0]


def _cell_value(cell, shared: list[str]) -> Any:
    ctype = cell.get("t")
    if ctype == "s":
        v = cell.find(f"{_NS}v")
        try:
            return shared[int(v.text)] if v is not None else ""
        except (ValueError, IndexError):
            return ""
    if ctype == "inlineStr":
        return _text(cell.find(f"{_NS}is"))
    if ctype == "str":
        return _text(cell.find(f"{_NS}v"))
    v = cell.find(f"{_NS}v")
    if v is None or v.text is None:
        return ""
    raw = v.text.strip()
    try:
        num = float(raw)
    except ValueError:
        return raw
    return int(num) if num.is_integer() else num


def read_xlsx(data: bytes) -> list[list[Any]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UserError("这不是有效的 .xlsx 文件（可能是旧的 .xls，请另存为 xlsx）") from exc
    with z:
        shared = _shared_strings(z)
        root = ET.fromstring(z.read(_first_sheet(z)))
    rows: list[list[Any]] = []
    for row in root.iter(f"{_NS}row"):
        values: dict[int, Any] = {}
        for cell in row.findall(f"{_NS}c"):
            values[_col_index(cell.get("r", ""))] = _cell_value(cell, shared)
        if values:
            width = max(values) + 1
            rows.append([values.get(i, "") for i in range(width)])
    return rows


def read_csv(data: bytes) -> list[list[Any]]:
    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise UserError("CSV 编码无法识别，请另存为 UTF-8")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def read_table(data: bytes, filename: str = "") -> list[list[Any]]:
    suffix = (filename or "").lower().rsplit(".", 1)[-1]
    if suffix in ("xlsx",):
        return read_xlsx(data)
    if suffix in ("csv", "txt"):
        return read_csv(data)
    # 无扩展名时按内容猜：xlsx 以 PK 开头
    return read_xlsx(data) if data[:2] == b"PK" else read_csv(data)


def _norm_header(value: Any) -> str:
    text = str(value or "").strip().replace(" ", "")
    for alias, canonical in (
        ("日期", "日期"), ("时间", "日期"), ("date", "日期"),
        ("类型", "类型"), ("收支", "类型"), ("type", "类型"),
        ("科目", "科目"), ("类别", "科目"), ("项目", "科目"), ("category", "科目"),
        ("金额", "金额"), ("钱", "金额"), ("元", "金额"), ("amount", "金额"),
        ("数量", "数量"), ("qty", "数量"),
        ("单位", "单位"), ("unit", "单位"),
        ("备注", "备注"), ("说明", "备注"), ("note", "备注"),
    ):
        if alias in text:
            return canonical
    return text


def _parse_date(value: Any, year_hint: str | None = None) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        days = int(value)
        if 20000 < days < 80000:  # Excel 序列号大致范围
            return (_EXCEL_EPOCH + timedelta(days=days)).isoformat()
    text = str(value or "").strip()
    if not text:
        raise ValidationError("日期为空")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # 只写了"5月6日""5/6"：用年度提示补年（strptime 对缺年份的格式有歧义，手工解析）
    match = re.fullmatch(r"(\d{1,2})[月/](\d{1,2})日?", text)
    if match:
        try:
            month, day = int(match.group(1)), int(match.group(2))
            return date(int(year_hint or date.today().year), month, day).isoformat()
        except ValueError:
            pass
    raise ValidationError(f"日期看不懂：{text!r}")


@dataclass
class ImportSummary:
    total: int = 0
    created: int = 0
    duplicates: int = 0
    errors: list[dict] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "created": self.created,
            "duplicates": self.duplicates,
            "errors": self.errors[:50],
            "error_count": len(self.errors),
            "event_ids": self.event_ids[:50],
        }


def import_rows(
    conn,
    actor: str,
    farmer_id: int,
    rows: list[list[Any]],
    *,
    state: str = "draft",
    year_hint: str | None = None,
    max_rows: int = 5000,
) -> ImportSummary:
    if not rows:
        raise UserError("表格是空的")
    header = [_norm_header(v) for v in rows[0]]
    missing = [c for c in ("日期", "科目", "金额") if c not in header]
    if missing:
        raise UserError(
            f"表头缺少列：{'、'.join(missing)}。第一行请写：{' | '.join(IMPORT_COLUMNS)}"
        )
    index = {name: header.index(name) for name in header if name in IMPORT_COLUMNS}

    svc = LedgerService(conn, actor)
    summary = ImportSummary()
    for offset, row in enumerate(rows[1:], start=2):
        if summary.total >= max_rows:
            summary.errors.append({"row": offset, "error": f"超过单次导入上限 {max_rows} 行"})
            break
        if not any(str(cell).strip() for cell in row):
            continue
        summary.total += 1

        def get(name: str) -> Any:
            i = index.get(name)
            return row[i] if i is not None and i < len(row) else ""

        try:
            date_iso = _parse_date(get("日期"), year_hint)
            amount_text = get("金额")
            if isinstance(amount_text, (int, float)) and not isinstance(amount_text, bool):
                amount_fen = int(Decimal(str(amount_text)) * 100)
            else:
                amount_fen = parse_amount_fen(amount_text)
            category = str(get("科目") or "").strip()
            if not category:
                raise ValidationError("科目为空")
            type_text = str(get("类型") or "").strip()
            event_type = _TYPE_ALIASES.get(type_text)
            if event_type is None:
                event_type = "income" if any(k in category for k in ("销售", "收入", "补贴")) else "variable_cost"
            qty = get("数量")
            unit = str(get("单位") or "").strip() or None
            if qty in ("", None):
                qty = None
            eid = svc.record(
                farmer_id=farmer_id,
                date_text=date_iso,
                type=event_type,
                category=category,
                amount_fen=amount_fen,
                qty=qty,
                unit=unit,
                note=str(get("备注") or "")[:200],
                source="import",
                state=state,
            )
            summary.created += 1
            summary.event_ids.append(eid)
        except ValidationError as exc:
            summary.errors.append({"row": offset, "error": exc.message, "raw": [str(c) for c in row][:8]})
        except Exception as exc:  # noqa: BLE001 - 重复行等业务冲突按"跳过"处理
            from .errors import Conflict

            if isinstance(exc, Conflict):
                summary.duplicates += 1
            else:
                summary.errors.append({"row": offset, "error": str(exc)})
    return summary
