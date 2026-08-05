"""Bounded, DTD-free parsing of the small XLSX subset used by product imports."""

from __future__ import annotations

import asyncio
import multiprocessing
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.process import BaseProcess
from pathlib import PurePosixPath

from defusedxml import ElementTree  # type: ignore[import-untyped]

XLSX_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
XML_DANGER_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
CELL_REFERENCE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$", re.ASCII)
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True, slots=True)
class ContainerMetrics:
    """Auditable XLSX ZIP metrics established before XML parsing."""

    compressed_bytes: int
    decompressed_bytes: int
    compression_ratio: float


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One physical worksheet row and its cell values."""

    row_number: int
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedWorkbook:
    """The first worksheet plus the count of all non-empty worksheets."""

    headers: tuple[str, ...]
    rows: tuple[ParsedRow, ...]
    nonempty_sheet_count: int


class XlsxRejected(Exception):
    """A safe rejection raised before product data is considered."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, object],
        status_code: int = 422,
        format_error: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.status_code = status_code
        self.format_error = format_error


def inspect_xlsx_container(
    content: bytes,
    *,
    max_decompressed_bytes: int,
    max_compression_ratio: float,
) -> ContainerMetrics:
    """Validate ZIP signature, bounds, and XML safety without parsing business XML."""

    signature = content[:4]
    if not any(content.startswith(candidate) for candidate in XLSX_SIGNATURES):
        raise XlsxRejected(
            "invalid_xlsx_signature",
            "文件不是有效的 XLSX (ZIP 签名不符)。",
            detail={
                "reason": "zip_signature_mismatch",
                "actual_signature_hex": signature.hex(),
            },
        )
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            entries = archive.infolist()
            decompressed_bytes = sum(entry.file_size for entry in entries)
            compressed_bytes = sum(entry.compress_size for entry in entries)
            compression_ratio = decompressed_bytes / max(compressed_bytes, 1)
            if decompressed_bytes > max_decompressed_bytes:
                raise XlsxRejected(
                    "xlsx_decompressed_too_large",
                    "XLSX 解压后内容超过 50 MB 上限。",
                    detail={
                        "reason": "decompressed_size_exceeded",
                        "actual_decompressed_bytes": decompressed_bytes,
                        "max_decompressed_bytes": max_decompressed_bytes,
                    },
                    status_code=413,
                )
            if compression_ratio > max_compression_ratio:
                raise XlsxRejected(
                    "xlsx_compression_ratio_too_high",
                    "XLSX 压缩比超过 100:1 上限。",
                    detail={
                        "reason": "compression_ratio_exceeded",
                        "actual_compression_ratio": compression_ratio,
                        "max_compression_ratio": max_compression_ratio,
                    },
                    status_code=413,
                )
            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise XlsxRejected(
                        "encrypted_xlsx_not_supported",
                        "不支持加密的 XLSX 文件。",
                        detail={"reason": "encrypted_zip_entry", "entry": entry.filename},
                    )
                if not entry.filename.casefold().endswith((".xml", ".rels")):
                    continue
                xml_content = archive.read(entry)
                if XML_DANGER_PATTERN.search(xml_content) is not None:
                    raise XlsxRejected(
                        "unsafe_xlsx_xml",
                        "XLSX 包含不允许的 DTD 或外部实体声明。",
                        detail={
                            "reason": "dtd_or_entity_declaration",
                            "entry": entry.filename,
                        },
                    )
    except XlsxRejected:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise XlsxRejected(
            "invalid_xlsx_container",
            "文件不是有效的 XLSX 容器。",
            detail={
                "reason": "invalid_zip_container",
                "error_type": type(error).__name__,
            },
        ) from error
    return ContainerMetrics(
        compressed_bytes=compressed_bytes,
        decompressed_bytes=decompressed_bytes,
        compression_ratio=compression_ratio,
    )


def _xml_root(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        payload = archive.read(name)
    except KeyError as error:
        raise ValueError(f"Missing XLSX entry: {name}") from error
    return ElementTree.fromstring(payload)


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        root = _xml_root(archive, "xl/sharedStrings.xml")
    except ValueError:
        return ()
    return tuple(
        "".join(node.text or "" for node in item.findall(f".//{{{SPREADSHEET_NS}}}t"))
        for item in root.findall(f"{{{SPREADSHEET_NS}}}si")
    )


def _worksheet_paths(archive: zipfile.ZipFile) -> tuple[str, ...]:
    workbook = _xml_root(archive, "xl/workbook.xml")
    relationships = _xml_root(archive, "xl/_rels/workbook.xml.rels")
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
        if "Id" in relationship.attrib and "Target" in relationship.attrib
    }
    paths: list[str] = []
    for sheet in workbook.findall(f".//{{{SPREADSHEET_NS}}}sheet"):
        relationship_id = sheet.attrib.get(f"{{{OFFICE_REL_NS}}}id")
        if relationship_id is None or relationship_id not in targets:
            raise ValueError("Worksheet relationship is missing")
        target = targets[relationship_id]
        if target.startswith("/"):
            resolved = target.lstrip("/")
        else:
            resolved = posixpath.normpath(posixpath.join("xl", target))
        if ".." in PurePosixPath(resolved).parts:
            raise ValueError("Worksheet relationship escapes the XLSX package")
        paths.append(resolved)
    if not paths:
        raise ValueError("Workbook has no worksheets")
    return tuple(paths)


def _column_index(reference: str, fallback: int) -> int:
    match = CELL_REFERENCE_PATTERN.fullmatch(reference)
    if match is None:
        return fallback
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _cell_text(
    cell: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{SPREADSHEET_NS}}}t"))
    value_node = cell.find(f"{{{SPREADSHEET_NS}}}v")
    if value_node is None or value_node.text is None:
        return ""
    value = str(value_node.text)
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as error:
            raise ValueError("Shared string index is invalid") from error
    if cell_type == "b":
        return "TRUE" if value == "1" else "FALSE"
    return value


def _rows(
    root: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> tuple[ParsedRow, ...]:
    parsed: list[ParsedRow] = []
    fallback_row_number = 0
    for row in root.findall(f".//{{{SPREADSHEET_NS}}}row"):
        fallback_row_number += 1
        raw_row_number = row.attrib.get("r")
        try:
            row_number = int(raw_row_number) if raw_row_number is not None else fallback_row_number
        except ValueError as error:
            raise ValueError("Worksheet row number is invalid") from error
        values: list[str] = []
        fallback_column = 0
        for cell in row.findall(f"{{{SPREADSHEET_NS}}}c"):
            column = _column_index(cell.attrib.get("r", ""), fallback_column)
            if column < 0 or column > 16_383:
                raise ValueError("Worksheet column is outside the XLSX limit")
            while len(values) <= column:
                values.append("")
            values[column] = _cell_text(cell, shared_strings)
            fallback_column = column + 1
        parsed.append(ParsedRow(row_number=row_number, values=tuple(values)))
    return tuple(parsed)


def parse_xlsx(content: bytes) -> ParsedWorkbook:
    """Parse first-sheet rows and count non-empty sheets after safety inspection."""

    with zipfile.ZipFile(BytesIO(content)) as archive:
        shared_strings = _shared_strings(archive)
        paths = _worksheet_paths(archive)
        worksheets = tuple(_rows(_xml_root(archive, path), shared_strings) for path in paths)
    first_rows = worksheets[0]
    header_row = next((row for row in first_rows if row.row_number == 1), None)
    headers = header_row.values if header_row is not None else ()
    data_rows = tuple(row for row in first_rows if row.row_number > 1)
    nonempty_sheet_count = sum(
        any(any(value.strip() for value in row.values) for row in worksheet)
        for worksheet in worksheets
    )
    return ParsedWorkbook(
        headers=headers,
        rows=data_rows,
        nonempty_sheet_count=nonempty_sheet_count,
    )


def _parse_worker(sender: PipeConnection, content: bytes) -> None:
    try:
        sender.send(("success", parse_xlsx(content)))
    except Exception as error:
        sender.send(("failure", type(error).__name__, str(error)))
    finally:
        sender.close()


def _wait_for_process(process: BaseProcess, timeout_seconds: float) -> bool:
    process.join(timeout_seconds)
    if not process.is_alive():
        return True
    process.terminate()
    process.join()
    return False


def _terminate_and_reap(process: BaseProcess) -> None:
    if process.is_alive():
        process.terminate()
    process.join()


async def parse_xlsx_with_timeout(
    content: bytes,
    *,
    timeout_seconds: float,
) -> ParsedWorkbook:
    """Parse in a disposable process so timeout cancellation leaves no worker behind."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_parse_worker, args=(sender, content), daemon=True)
    started_at = time.monotonic()
    process.start()
    sender.close()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(receiver.recv),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await asyncio.to_thread(_terminate_and_reap, process)
        receiver.close()
        elapsed = time.monotonic() - started_at
        raise XlsxRejected(
            "xlsx_parse_timeout",
            f"XLSX 解析超过 {timeout_seconds:g} 秒上限, 已中止。",
            detail={
                "reason": "parse_timeout",
                "actual_elapsed_seconds": elapsed,
                "max_parse_seconds": timeout_seconds,
            },
            status_code=408,
        ) from None
    except (EOFError, OSError):
        await asyncio.to_thread(_terminate_and_reap, process)
        receiver.close()
        raise XlsxRejected(
            "invalid_xlsx",
            "XLSX 解析失败。",
            detail={"reason": "parser_process_failed", "exit_code": process.exitcode},
            format_error=True,
        ) from None
    await asyncio.to_thread(process.join)
    receiver.close()
    if result[0] == "success":
        workbook = result[1]
        if isinstance(workbook, ParsedWorkbook):
            return workbook
        raise RuntimeError("Parser returned an unexpected result")
    raise XlsxRejected(
        "invalid_xlsx",
        "XLSX 结构无法解析。",
        detail={
            "reason": "xml_or_workbook_parse_failed",
            "error_type": str(result[1]),
        },
        format_error=True,
    )
