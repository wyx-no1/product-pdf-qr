"""Bounded, DTD-free parsing of the small XLSX subset used by product imports."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import posixpath
import re
import resource

# A fixed /bin/ps invocation reads this worker's own Darwin virtual size.
import subprocess  # nosec B404
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BytesIO
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.process import BaseProcess
from pathlib import PurePosixPath

from defusedxml import ElementTree  # type: ignore[import-untyped]

from product_pdf_qr.config import DEFAULT_IMPORT_PARSE_MEMORY_BYTES

XLSX_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
XML_DANGER_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
CELL_REFERENCE_PATTERN = re.compile(r"^([A-Z]+)([1-9][0-9]*)$", re.ASCII)
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CODE_HEADERS = frozenset({"编码", "产品编码"})
NAME_HEADERS = frozenset({"名称", "产品名称"})


@dataclass(frozen=True, slots=True)
class ContainerMetrics:
    """Auditable XLSX ZIP metrics established before XML parsing."""

    compressed_bytes: int
    decompressed_bytes: int
    compression_ratio: float


@dataclass(frozen=True, slots=True)
class ParsedCells:
    """Sparse worksheet cells keyed by zero-based XLSX column index."""

    entries: tuple[tuple[int, str], ...]

    @classmethod
    def from_dense(cls, values: tuple[str, ...]) -> ParsedCells:
        """Build sparse cells for tests and non-parser callers."""

        return cls(tuple((index, value) for index, value in enumerate(values) if value))

    def get(self, index: int) -> str:
        """Return one cell without allocating through the requested column."""

        return next((value for column, value in self.entries if column == index), "")

    def has_nonblank(self) -> bool:
        """Return whether any represented cell contains non-whitespace text."""

        return any(value.strip() for _column, value in self.entries)

    def populated_values(self) -> tuple[str, ...]:
        """Return represented values in column order for diagnostics."""

        return tuple(value for _column, value in self.entries)


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """One physical worksheet row and its sparse cell values."""

    row_number: int
    values: ParsedCells
    has_nonblank_cells: bool


@dataclass(frozen=True, slots=True)
class ParsedWorkbook:
    """The first worksheet plus the count of all non-empty worksheets."""

    headers: ParsedCells
    rows: tuple[ParsedRow, ...]
    nonempty_sheet_count: int


XlsxParser = Callable[[bytes, int], ParsedWorkbook]


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


def _row_number(row: ElementTree.Element, fallback: int) -> int:
    raw_row_number = row.attrib.get("r")
    try:
        return int(raw_row_number) if raw_row_number is not None else fallback
    except ValueError as error:
        raise ValueError("Worksheet row number is invalid") from error


def _row_cells(
    row: ElementTree.Element,
    shared_strings: tuple[str, ...],
    *,
    retained_columns: frozenset[int] | None,
) -> tuple[ParsedCells, bool]:
    values: list[tuple[int, str]] = []
    fallback_column = 0
    has_nonblank_cells = False
    for cell in row.iter(f"{{{SPREADSHEET_NS}}}c"):
        column = _column_index(cell.attrib.get("r", ""), fallback_column)
        if column < 0 or column > 16_383:
            raise ValueError("Worksheet column is outside the XLSX limit")
        value = _cell_text(cell, shared_strings)
        has_nonblank_cells = has_nonblank_cells or bool(value.strip())
        if value and (retained_columns is None or column in retained_columns):
            values.append((column, value))
        fallback_column = column + 1
    return ParsedCells(tuple(values)), has_nonblank_cells


def _consume_rows(
    rows: Iterable[ElementTree.Element],
    shared_strings: tuple[str, ...],
    *,
    max_data_rows: int | None = None,
    clear_elements: bool,
) -> tuple[tuple[ParsedRow, ...], bool]:
    parsed_data_rows: list[ParsedRow] = []
    header_values = ParsedCells(())
    header_has_nonblank_cells = False
    header_seen = False
    retained_columns: frozenset[int] | None = None
    nonblank_data_rows = 0
    worksheet_is_nonempty = False

    for fallback_row_number, row in enumerate(
        rows,
        start=1,
    ):
        try:
            row_number = _row_number(row, fallback_row_number)
            parsed_values, has_nonblank_cells = _row_cells(
                row,
                shared_strings,
                retained_columns=None if row_number == 1 else retained_columns,
            )
            worksheet_is_nonempty = worksheet_is_nonempty or has_nonblank_cells
            if row_number == 1:
                if not header_seen:
                    header_seen = True
                    header_values = parsed_values
                    header_has_nonblank_cells = has_nonblank_cells
                    retained_columns = frozenset(
                        column
                        for column, value in header_values.entries
                        if value.strip().casefold() in CODE_HEADERS | NAME_HEADERS
                    )
                continue
            if row_number < 1 or not has_nonblank_cells:
                continue
            nonblank_data_rows += 1
            if max_data_rows is not None and nonblank_data_rows > max_data_rows:
                raise XlsxRejected(
                    "xlsx_row_limit_exceeded",
                    "数据行超过 5,000 行上限, 请分批导入。",
                    detail={
                        "reason": "row_limit_exceeded",
                        "actual_rows": nonblank_data_rows,
                        "max_rows": max_data_rows,
                    },
                    status_code=413,
                )
            parsed_data_rows.append(
                ParsedRow(
                    row_number=row_number,
                    values=parsed_values,
                    has_nonblank_cells=True,
                )
            )
        finally:
            if clear_elements:
                row.clear()

    consumed_columns = retained_columns or frozenset()
    parsed_data_rows = [
        ParsedRow(
            row_number=row.row_number,
            values=ParsedCells(
                tuple(
                    (column, value)
                    for column, value in row.values.entries
                    if column in consumed_columns
                )
            ),
            has_nonblank_cells=True,
        )
        for row in parsed_data_rows
    ]
    parsed: list[ParsedRow] = []
    if header_seen:
        parsed.append(
            ParsedRow(
                row_number=1,
                values=header_values,
                has_nonblank_cells=header_has_nonblank_cells,
            )
        )
    parsed.extend(parsed_data_rows)
    return tuple(parsed), worksheet_is_nonempty


def _rows(
    root: ElementTree.Element,
    shared_strings: tuple[str, ...],
    *,
    max_data_rows: int | None = None,
) -> tuple[ParsedRow, ...]:
    parsed, _is_nonempty = _consume_rows(
        root.iter(f"{{{SPREADSHEET_NS}}}row"),
        shared_strings,
        max_data_rows=max_data_rows,
        clear_elements=False,
    )
    return parsed


def _stream_first_worksheet(
    archive: zipfile.ZipFile,
    path: str,
    shared_strings: tuple[str, ...],
    *,
    max_data_rows: int,
) -> tuple[tuple[ParsedRow, ...], bool]:
    with archive.open(path) as source:
        row_elements = (
            element
            for _event, element in ElementTree.iterparse(source, events=("end",))
            if element.tag == f"{{{SPREADSHEET_NS}}}row"
        )
        return _consume_rows(
            row_elements,
            shared_strings,
            max_data_rows=max_data_rows,
            clear_elements=True,
        )


def _stream_worksheet_is_nonempty(
    archive: zipfile.ZipFile,
    path: str,
    shared_strings: tuple[str, ...],
) -> bool:
    is_nonempty = False
    with archive.open(path) as source:
        for _event, element in ElementTree.iterparse(source, events=("end",)):
            if element.tag == f"{{{SPREADSHEET_NS}}}c":
                is_nonempty = is_nonempty or bool(_cell_text(element, shared_strings).strip())
            if element.tag == f"{{{SPREADSHEET_NS}}}row":
                element.clear()
    return is_nonempty


def parse_xlsx(content: bytes, *, max_rows: int = 5_000) -> ParsedWorkbook:
    """Parse first-sheet rows and count non-empty sheets after safety inspection."""

    with zipfile.ZipFile(BytesIO(content)) as archive:
        shared_strings = _shared_strings(archive)
        paths = _worksheet_paths(archive)
        first_rows, first_is_nonempty = _stream_first_worksheet(
            archive,
            paths[0],
            shared_strings,
            max_data_rows=max_rows,
        )
        nonempty_sheet_count = int(first_is_nonempty)
        for path in paths[1:]:
            nonempty_sheet_count += int(
                _stream_worksheet_is_nonempty(archive, path, shared_strings)
            )
    header_row = next((row for row in first_rows if row.row_number == 1), None)
    headers = header_row.values if header_row is not None else ParsedCells(())
    data_rows = tuple(row for row in first_rows if row.row_number > 1)
    return ParsedWorkbook(
        headers=headers,
        rows=data_rows,
        nonempty_sheet_count=nonempty_sheet_count,
    )


def _parse_xlsx_for_worker(content: bytes, max_rows: int) -> ParsedWorkbook:
    return parse_xlsx(content, max_rows=max_rows)


def _set_xlsx_worker_memory_limit(memory_bytes: int) -> None:
    """Limit parser address-space growth before consuming untrusted XML."""

    if sys.platform == "darwin":
        # dyld reserves hundreds of GiB of sparse virtual address space before
        # this worker starts. Bound additional growth by the configured budget.
        current_vsize_bytes = (
            int(
                subprocess.check_output(  # nosec B603
                    ["/bin/ps", "-o", "vsz=", "-p", str(os.getpid())],
                    text=True,
                ).strip()
            )
            * 1024
        )
        address_space_limit = current_vsize_bytes + memory_bytes
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_limit, address_space_limit),
        )
        return
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def _send_worker_result(sender: PipeConnection, result: tuple[object, ...]) -> None:
    try:
        sender.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _parse_worker(
    sender: PipeConnection,
    content: bytes,
    max_rows: int,
    memory_bytes: int,
    parser: XlsxParser,
) -> None:
    try:
        _set_xlsx_worker_memory_limit(memory_bytes)
        _send_worker_result(sender, ("success", parser(content, max_rows)))
    except MemoryError:
        _send_worker_result(sender, ("resource_limit", memory_bytes))
    except (OSError, ValueError):
        _send_worker_result(sender, ("resource_limit", memory_bytes))
    except XlsxRejected as error:
        _send_worker_result(
            sender,
            (
                "rejected",
                error.code,
                error.message,
                error.detail,
                error.status_code,
                error.format_error,
            ),
        )
    except Exception as error:
        _send_worker_result(sender, ("failure", type(error).__name__, str(error)))
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
    max_rows: int = 5_000,
    memory_bytes: int = DEFAULT_IMPORT_PARSE_MEMORY_BYTES,
    parser: XlsxParser = _parse_xlsx_for_worker,
) -> ParsedWorkbook:
    """Parse in a disposable process so timeout cancellation leaves no worker behind."""

    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parse_worker,
        args=(sender, content, max_rows, memory_bytes, parser),
        daemon=True,
    )
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
        if process.exitcode is not None and process.exitcode < 0:
            raise XlsxRejected(
                "xlsx_parse_resource_limit",
                "XLSX 解析超过内存资源上限。",
                detail={
                    "reason": "parser_memory_limit_exceeded",
                    "max_memory_bytes": memory_bytes,
                    "exit_code": process.exitcode,
                },
                status_code=413,
            ) from None
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
    if result[0] == "rejected":
        raise XlsxRejected(
            str(result[1]),
            str(result[2]),
            detail=dict(result[3]),
            status_code=int(result[4]),
            format_error=bool(result[5]),
        )
    if result[0] == "resource_limit":
        raise XlsxRejected(
            "xlsx_parse_resource_limit",
            "XLSX 解析超过内存资源上限。",
            detail={
                "reason": "parser_memory_limit_exceeded",
                "max_memory_bytes": int(result[1]),
            },
            status_code=413,
        )
    raise XlsxRejected(
        "invalid_xlsx",
        "XLSX 结构无法解析。",
        detail={
            "reason": "xml_or_workbook_parse_failed",
            "error_type": str(result[1]),
        },
        format_error=True,
    )
