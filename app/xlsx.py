"""최소 Excel(.xlsx) 작성기. 표준 라이브러리(zipfile)만 쓴다 → 배포 zip에 라이브러리를 추가하지 않는다.

시트 1개, 머리글 굵게·배경색, 첫 행 고정, 열 너비, 숫자는 숫자 셀, 문자열은 inlineStr.
"""
import io
import zipfile
from xml.sax.saxutils import escape

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

# 스타일 0: 기본, 1: 머리글(굵게·회색 배경·테두리), 2: 합계 행(굵게), 3: 소수 2자리, 4: 합계 행 소수 2자리
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="0.00"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="맑은 고딕"/></font><font><b/><sz val="11"/><name val="맑은 고딕"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFE7ECF3"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>
<border><left/><right/><top/><bottom style="thin"><color rgb="FF9AA5B4"/></bottom><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="164" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style, dec):
    if v is None or v == "":
        return f'<c r="{ref}" s="{style}"/>' if style else ""
    if isinstance(v, bool):
        v = "예" if v else "아니오"
    if isinstance(v, (int, float)):
        st = {0: 3, 2: 4}.get(style, style) if dec and isinstance(v, float) else style
        return f'<c r="{ref}" s="{st}"><v>{v}</v></c>'
    text = escape(str(v)).replace("\n", "&#10;")
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _width(v):
    s = "" if v is None else str(v)
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)   # 한글 등 전각은 2칸


def build(sheet, headers, rows, footer=None, decimals=()):
    """xlsx 바이트를 만든다. decimals: 소수 2자리로 보일 열 번호(0부터)."""
    dec = set(decimals)
    widths = [_width(h) for h in headers]
    for r in list(rows) + ([footer] if footer else []):
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], min(_width(v), 60))
    out = []
    def row(ri, vals, style):
        cells = "".join(_cell(f"{_col(ci)}{ri}", v, style, ci in dec) for ci, v in enumerate(vals))
        out.append(f'<row r="{ri}">{cells}</row>')
    row(1, headers, 1)
    for k, r in enumerate(rows, start=2):
        row(k, r, 0)
    if footer:
        row(len(rows) + 2, footer, 2)
    last = f"{_col(len(headers) - 1)}{len(rows) + 1 + (1 if footer else 0)}"
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(w + 2, 62)}" customWidth="1"/>'
                   for i, w in enumerate(widths))
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 f'<dimension ref="A1:{last}"/>'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 f'<cols>{cols}</cols><sheetData>{"".join(out)}</sheetData>'
                 f'<autoFilter ref="A1:{_col(len(headers) - 1)}{len(rows) + 1}"/></worksheet>')
    name = escape(sheet[:31])
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{name}" sheetId="1" r:id="rId1"/></sheets>'
          f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">\'{name}\'!$A$1:${_col(len(headers) - 1)}${len(rows) + 1}</definedName></definedNames>'
          '</workbook>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()
