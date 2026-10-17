"""极简 XLSX 写出器（纯标准库）。

为什么不依赖 openpyxl：交付环境断网、无管理员权限，装不了第三方包。xlsx 本质是
一个 zip 包 + 若干 XML，本系统只需要"多工作表 + 文本/数字 + 加粗表头 + 千分位"，
自己写 200 行比让村委去装 Python 包可靠得多。

只实现用得到的子集：inlineStr（不搞 sharedStrings）、列宽、行高、四种单元格样式、
冻结首行。生成的文件 Excel / WPS / LibreOffice 都能打开——这一点在交付前用
LibreOffice 转换验证过（见 docs/部署与验证.md）。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

__all__ = ["Workbook", "Sheet", "to_bytes"]


def _col_letter(index: int) -> str:
    """1 → A, 27 → AA。"""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_ref(row: int, col: int) -> str:
    return f"{_col_letter(col)}{row}"


@dataclass
class Sheet:
    name: str
    rows: list[tuple[list, int]] = field(default_factory=list)  # (values, style_id)
    widths: list[float] = field(default_factory=list)
    freeze_header: bool = False

    def append(self, values: list, style: int = 0) -> None:
        self.rows.append((values, style))

    def width(self, *widths: float) -> None:
        self.widths = list(widths)

    def _xml(self) -> str:
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        ]
        # 元素顺序按 ECMA-376 的 CT_Worksheet 定义：sheetViews 必须在 cols 之前，
        # 否则 Excel 会提示"修复"，LibreOffice 可能整页渲染为空白。
        if self.freeze_header:
            parts.append(
                '<sheetViews><sheetView workbookViewId="0">'
                '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                "</sheetView></sheetViews>"
            )
        if self.widths:
            parts.append("<cols>")
            for i, w in enumerate(self.widths, start=1):
                parts.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
            parts.append("</cols>")
        parts.append("<sheetData>")
        for r_idx, (values, style) in enumerate(self.rows, start=1):
            parts.append(f'<row r="{r_idx}">')
            for c_idx, value in enumerate(values, start=1):
                ref = _cell_ref(r_idx, c_idx)
                if isinstance(value, bool):
                    value = "是" if value else "否"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    parts.append(f'<c r="{ref}" s="{style}"><v>{value}</v></c>')
                elif value is None or value == "":
                    parts.append(f'<c r="{ref}" s="{style}"/>')
                else:
                    text = escape(str(value))
                    parts.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
            parts.append("</row>")
        parts.append("</sheetData>")
        parts.append("</worksheet>")
        return "".join(parts)

    def _safe_name(self) -> str:
        name = re.sub(r"[\[\]:*?/\\]", "", self.name)[:31] or "Sheet"
        return escape(name)


# 样式编号约定：0 普通 1 加粗 2 金额(千分位) 3 表头 4 标题 5 说明(灰字) 6 金额加粗
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>
<fonts count="4">
<font><sz val="11"/><name val="宋体"/></font>
<font><b/><sz val="11"/><name val="宋体"/></font>
<font><b/><sz val="16"/><name val="宋体"/></font>
<font><sz val="9"/><color rgb="FF666666"/><name val="宋体"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEFEFEF"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right><top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="7">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" horizontal="right"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="1" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" horizontal="right"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="常规" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


@dataclass
class Workbook:
    sheets: list[Sheet] = field(default_factory=list)

    def add_sheet(self, name: str) -> Sheet:
        sheet = Sheet(name=name)
        self.sheets.append(sheet)
        return sheet

    def to_bytes(self) -> bytes:
        if not self.sheets:
            raise ValueError("工作簿至少要有一个工作表")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", self._content_types())
            z.writestr("_rels/.rels", _RELS)
            z.writestr("xl/workbook.xml", self._workbook_xml())
            z.writestr("xl/_rels/workbook.xml.rels", self._workbook_rels())
            z.writestr("xl/styles.xml", _STYLES)
            for i, sheet in enumerate(self.sheets, start=1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", sheet._xml())
        return buf.getvalue()

    def _content_types(self) -> str:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(self.sheets) + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>"
        )

    def _workbook_xml(self) -> str:
        sheets = "".join(
            f'<sheet name="{s._safe_name()}" sheetId="{i}" r:id="rId{i}"/>'
            for i, s in enumerate(self.sheets, start=1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheets}</sheets></workbook>"
        )

    def _workbook_rels(self) -> str:
        rels = "".join(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i}.xml"/>'
            for i in range(1, len(self.sheets) + 1)
        )
        n = len(self.sheets) + 1
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels}"
            f'<Relationship Id="rId{n}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
            "</Relationships>"
        )


_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/>'
    "</Relationships>"
)


def to_bytes(workbook: Workbook) -> bytes:
    return workbook.to_bytes()
