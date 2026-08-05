"""Synthetic XLSX builders used only by import tests."""

from __future__ import annotations

import html
import io
import zipfile
from collections.abc import Sequence

Worksheet = Sequence[tuple[int, Sequence[str]]]


def _column_name(index: int) -> str:
    value = index + 1
    name = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _worksheet_xml(rows: Worksheet) -> str:
    row_xml: list[str] = []
    for row_number, values in rows:
        cells = []
        for index, value in enumerate(values):
            reference = f"{_column_name(index)}{row_number}"
            escaped = html.escape(value)
            cells.append(
                f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">'
                f"{escaped}</t></is></c>"
            )
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )


def build_xlsx(
    sheets: Sequence[Worksheet],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    extra_entries: dict[str, bytes] | None = None,
) -> bytes:
    """Build a minimal standards-shaped XLSX with inline string cells."""

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        overrides = "".join(
            (
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.worksheet+xml"/>'
            )
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/>'
            f"{overrides}</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        sheet_nodes = "".join(
            (f'<sheet name="Sheet{index}" sheetId="{index}" r:id="rId{index}"/>')
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheet_nodes}</sheets></workbook>",
        )
        relationship_nodes = "".join(
            (
                f'<Relationship Id="rId{index}" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                f'worksheet" Target="worksheets/sheet{index}.xml"/>'
            )
            for index in range(1, len(sheets) + 1)
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relationship_nodes}</Relationships>",
        )
        for index, rows in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet_xml(rows))
        for name, payload in (extra_entries or {}).items():
            archive.writestr(name, payload)
    return output.getvalue()


def build_sparse_wide_xlsx(row_count: int) -> bytes:
    """Build stored XML whose only data cell is in the final XLSX column."""

    rows = "".join(
        (f'<row r="{row_number}"><c r="XFD{row_number}" t="inlineStr"><is><t>X</t></is></c></row>')
        for row_number in range(2, row_count + 2)
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>编码</t></is></c></row>'
        f"{rows}</sheetData></worksheet>"
    ).encode()
    source = io.BytesIO(build_xlsx([[]], compression=zipfile.ZIP_STORED))
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as source_archive,
        zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as output_archive,
    ):
        for entry in source_archive.infolist():
            payload = (
                worksheet
                if entry.filename == "xl/worksheets/sheet1.xml"
                else source_archive.read(entry)
            )
            output_archive.writestr(entry.filename, payload)
    return output.getvalue()
