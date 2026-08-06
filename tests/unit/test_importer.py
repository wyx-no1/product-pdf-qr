"""G-03 Excel import parser, validation, security, token, and page tests."""

from __future__ import annotations

import asyncio
import io
import math
import multiprocessing
import resource
import subprocess
import sys
import zipfile
from collections import Counter
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, cast
from uuid import UUID
from xml.etree import ElementTree

import pytest
from fastapi import UploadFile
from pydantic import ValidationError
from starlette.datastructures import Headers

from product_pdf_qr.config import (
    DEFAULT_IMPORT_MAX_COMPRESSION_RATIO,
    DEFAULT_IMPORT_MAX_DECOMPRESSED_BYTES,
    DEFAULT_IMPORT_MAX_ROWS,
    DEFAULT_IMPORT_MAX_UPLOAD_BYTES,
    DEFAULT_IMPORT_PARSE_MEMORY_BYTES,
    DEFAULT_IMPORT_PARSE_TIMEOUT_SECONDS,
    Settings,
)
from product_pdf_qr.domains.auth import AuthenticatedAdmin
from product_pdf_qr.domains.importer.parser import (
    ContainerMetrics,
    ParsedCells,
    ParsedRow,
    ParsedWorkbook,
    XlsxRejected,
    _cell_text,
    _column_index,
    _parse_worker,
    _rows,
    _set_xlsx_worker_memory_limit,
    _wait_for_process,
    inspect_xlsx_container,
    parse_xlsx,
    parse_xlsx_with_timeout,
)
from product_pdf_qr.domains.importer.router import import_products_endpoint
from product_pdf_qr.domains.importer.service import (
    ImportCandidate,
    ImportFormatErrors,
    ImportResult,
    ImportRowError,
    import_products,
    read_upload_with_limit,
    validate_workbook,
)
from product_pdf_qr.domains.product import Product, generate_public_token
from product_pdf_qr.errors import AppError
from tests.unit.test_business_services import (
    ScriptedConnection,
    ScriptedDatabase,
    as_database,
)
from tests.xlsx_helpers import (
    build_sparse_wide_xlsx,
    build_structurally_invalid_xlsx,
    build_xlsx,
)

DATABASE_URL = "postgresql://app_rw:synthetic@127.0.0.1:5432/test"


def memory_exhausting_xlsx_parser(
    _content: bytes,
    _max_rows: int,
) -> ParsedWorkbook:
    """Attempt growth well beyond the worker budget to exercise RLIMIT_AS."""

    bytearray(256 * 1024 * 1024)
    raise AssertionError("allocation unexpectedly escaped the address-space limit")


def workbook(
    headers: tuple[str, ...],
    rows: Sequence[tuple[int, tuple[str, ...]]],
    *,
    sheets: int = 1,
) -> ParsedWorkbook:
    return ParsedWorkbook(
        headers=ParsedCells.from_dense(headers),
        rows=tuple(
            ParsedRow(
                row_number=row,
                values=ParsedCells.from_dense(values),
                has_nonblank_cells=any(value.strip() for value in values),
            )
            for row, values in rows
        ),
        nonempty_sheet_count=sheets,
    )


def format_errors(value: ParsedWorkbook) -> tuple[ImportRowError, ...]:
    with pytest.raises(ImportFormatErrors) as captured:
        validate_workbook(value, max_rows=5_000)
    return captured.value.errors


def test_tc04_code_boundaries_one_and_sixty_four() -> None:
    candidates, duplicates, _notices = validate_workbook(
        workbook(("编码",), [(2, ("A",)), (3, ("z" * 64,))]),
        max_rows=5_000,
    )
    assert [candidate.code for candidate in candidates] == ["A", "Z" * 64]
    assert duplicates == 0


@pytest.mark.parametrize(
    ("raw", "reason_fragment"),
    [
        ("A" * 65, "超过 64"),
        ("A中001", "非法字符"),
        ("A 001", "内部空格"),
        ("A#001", "非法字符"),
    ],
)
def test_tc05_tc06_tc07_tc08_invalid_code_classes(
    raw: str,
    reason_fragment: str,
) -> None:
    errors = format_errors(workbook(("编码",), [(2, ("VALID",)), (3, (raw,))]))
    assert len(errors) == 1
    assert errors[0].row == 3
    assert reason_fragment in errors[0].reason


def test_tc09_name_121_characters() -> None:
    errors = format_errors(
        workbook(("编码", "名称"), [(2, ("VALID", "ok")), (3, ("BADNAME", "名" * 121))])
    )
    assert [(error.row, error.reason) for error in errors] == [(3, "产品名称超过 120 字符。")]


def test_phase_one_validates_name_before_classifying_file_duplicate() -> None:
    errors = format_errors(
        workbook(
            ("编码", "名称"),
            [(2, ("A001", "首名")), (3, (" a001 ", "名" * 121))],
        )
    )
    assert [(error.row, error.reason) for error in errors] == [(3, "产品名称超过 120 字符。")]


def test_tc10_blank_code_in_nonblank_row() -> None:
    errors = format_errors(workbook(("编码", "名称"), [(2, ("", "仅名称")), (3, ("OK", "合法"))]))
    assert [(error.row, error.reason) for error in errors] == [(2, "产品编码为空。")]


def test_tc11_fully_blank_rows_are_ignored() -> None:
    rows = [(2, ("A001", "首个"))]
    rows.extend((row, (" ", "\t")) for row in range(3, 8))
    rows.extend((row, (f"A{row:03}", "")) for row in range(8, 17))
    candidates, duplicates, _notices = validate_workbook(
        workbook(("编码", "名称"), rows),
        max_rows=5_000,
    )
    assert len(candidates) == 10
    assert duplicates == 0


@pytest.mark.parametrize(
    ("headers", "expected_name"),
    [
        (("编码", "名称"), "输入名"),
        (("产品编码", "产品名称"), "输入名"),
        ((" 编码 ", " 名称 "), "输入名"),
        (("编码",), "A001"),
    ],
)
def test_tc12_header_alias_trim_casefold_and_optional_name(
    headers: tuple[str, ...],
    expected_name: str,
) -> None:
    values = (" a001 ", "输入名") if len(headers) == 2 else (" a001 ",)
    candidates, _duplicates, _notices = validate_workbook(
        workbook(headers, [(2, values)]),
        max_rows=5_000,
    )
    assert [(candidate.code, candidate.name) for candidate in candidates] == [
        ("A001", expected_name)
    ]


def test_tc13_missing_code_header_lists_original_headers() -> None:
    headers = (" SKU ", "自定义 名称", "备注#")
    with pytest.raises(XlsxRejected) as captured:
        validate_workbook(workbook(headers, [(2, ("A", "B", "C"))]), max_rows=5_000)
    assert captured.value.code == "missing_code_column"
    assert captured.value.detail["actual_headers"] == list(headers)
    assert all(header in captured.value.message for header in headers)


@pytest.mark.parametrize(
    "headers",
    [
        ("编码", "产品编码"),
        ("编码", " 编码 "),
    ],
)
def test_tc14_tc15_ambiguous_code_headers(headers: tuple[str, ...]) -> None:
    with pytest.raises(XlsxRejected) as captured:
        validate_workbook(workbook(headers, [(2, ("A", "B"))]), max_rows=5_000)
    assert captured.value.code == "ambiguous_code_column"
    assert "列名歧义" in captured.value.message


@pytest.mark.anyio
async def test_tc16_upload_over_ten_mb_stops_at_first_excess_byte() -> None:
    limit = 10 * 1024 * 1024
    upload = UploadFile(filename="large.xlsx", file=io.BytesIO(b"x" * (limit + 100)))
    with pytest.raises(XlsxRejected) as captured:
        await read_upload_with_limit(upload, limit)
    assert captured.value.code == "xlsx_too_large"
    assert captured.value.detail == {
        "reason": "upload_size_exceeded",
        "actual_bytes": limit + 1,
        "max_upload_bytes": limit,
    }


def test_tc17_5001_nonblank_rows_rejected() -> None:
    rows = [(row, (f"A{row}",)) for row in range(2, 5_003)]
    with pytest.raises(XlsxRejected) as captured:
        validate_workbook(workbook(("编码",), rows), max_rows=5_000)
    assert captured.value.detail["actual_rows"] == 5_001
    assert captured.value.detail["max_rows"] == 5_000


def test_sparse_xfd_rows_are_bounded_during_parsing() -> None:
    single_row = parse_xlsx(build_sparse_wide_xlsx(1), max_rows=5_000)
    assert single_row.rows[0].values.entries == ()
    assert single_row.rows[0].has_nonblank_cells is True

    attack = build_sparse_wide_xlsx(50_000)
    assert len(attack) < 10 * 1024 * 1024
    metrics = inspect_xlsx_container(
        attack,
        max_decompressed_bytes=50 * 1024 * 1024,
        max_compression_ratio=100,
    )
    assert metrics.decompressed_bytes < 50 * 1024 * 1024
    assert metrics.compression_ratio <= 100
    with pytest.raises(XlsxRejected) as captured:
        parse_xlsx(attack, max_rows=5_000)
    assert captured.value.code == "xlsx_row_limit_exceeded"
    assert captured.value.detail == {
        "reason": "row_limit_exceeded",
        "actual_rows": 5_001,
        "max_rows": 5_000,
    }


def test_tc18_non_zip_disguised_as_xlsx_rejected_before_xml_parser() -> None:
    with pytest.raises(XlsxRejected) as captured:
        inspect_xlsx_container(
            b"not a zip",
            max_decompressed_bytes=50 * 1024 * 1024,
            max_compression_ratio=100,
        )
    assert captured.value.code == "invalid_xlsx_signature"


def test_tc19_decompressed_size_bound_checked_before_xml_parse() -> None:
    content = build_xlsx(
        [[(1, ("编码",)), (2, ("A001",))]],
        compression=zipfile.ZIP_STORED,
        extra_entries={"xl/media/padding.bin": b"x" * 4_096},
    )
    with pytest.raises(XlsxRejected) as captured:
        inspect_xlsx_container(
            content,
            max_decompressed_bytes=1_000,
            max_compression_ratio=100,
        )
    assert captured.value.code == "xlsx_decompressed_too_large"
    assert cast(int, captured.value.detail["actual_decompressed_bytes"]) > 1_000


def test_tc20_compression_ratio_bound_checked_before_xml_parse() -> None:
    content = build_xlsx(
        [[(1, ("编码",)), (2, ("A001",))]],
        extra_entries={"xl/media/padding.bin": b"A" * (1024 * 1024)},
    )
    with pytest.raises(XlsxRejected) as captured:
        inspect_xlsx_container(
            content,
            max_decompressed_bytes=2 * 1024 * 1024,
            max_compression_ratio=100,
        )
    assert captured.value.code == "xlsx_compression_ratio_too_high"
    assert cast(float, captured.value.detail["actual_compression_ratio"]) > 100


def test_tc21_dtd_and_external_entity_are_rejected_without_request_side_effect() -> None:
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY ext SYSTEM '
        b'"http://127.0.0.1:9/secret">]><x>&ext;</x>'
    )
    content = build_xlsx(
        [[(1, ("编码",)), (2, ("A001",))]],
        extra_entries={"xl/externalLinks/externalLink1.xml": payload},
    )
    with pytest.raises(XlsxRejected) as captured:
        inspect_xlsx_container(
            content,
            max_decompressed_bytes=50 * 1024 * 1024,
            max_compression_ratio=100,
        )
    assert captured.value.code == "unsafe_xlsx_xml"
    assert captured.value.detail["reason"] == "dtd_or_entity_declaration"


class FakeSlowProcess:
    """Controllable process double proving timeout termination and reaping."""

    def __init__(self) -> None:
        self.alive = True
        self.terminated = False
        self.joins: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)
        if self.terminated:
            self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True


def test_tc22_parse_timeout_terminates_and_reaps_worker() -> None:
    process = FakeSlowProcess()
    assert not _wait_for_process(cast(BaseProcess, process), 30)
    assert process.terminated
    assert process.joins == [30, None]
    assert not process.alive


def test_tc23_import_config_defaults_overrides_and_invalid_values() -> None:
    defaults = Settings.model_validate({"database_url": DATABASE_URL})
    assert defaults.import_max_upload_bytes == DEFAULT_IMPORT_MAX_UPLOAD_BYTES
    assert defaults.import_max_decompressed_bytes == DEFAULT_IMPORT_MAX_DECOMPRESSED_BYTES
    assert defaults.import_max_compression_ratio == DEFAULT_IMPORT_MAX_COMPRESSION_RATIO
    assert defaults.import_parse_timeout_seconds == DEFAULT_IMPORT_PARSE_TIMEOUT_SECONDS
    assert defaults.import_max_rows == DEFAULT_IMPORT_MAX_ROWS
    overrides = Settings.model_validate(
        {
            "database_url": DATABASE_URL,
            "import_max_upload_bytes": 1,
            "import_max_decompressed_bytes": 2,
            "import_max_compression_ratio": 3,
            "import_parse_timeout_seconds": 4,
            "import_max_rows": 5,
        }
    )
    assert (
        overrides.import_max_upload_bytes,
        overrides.import_max_decompressed_bytes,
        overrides.import_max_compression_ratio,
        overrides.import_parse_timeout_seconds,
        overrides.import_max_rows,
    ) == (1, 2, 3, 4, 5)
    for field in (
        "import_max_upload_bytes",
        "import_max_decompressed_bytes",
        "import_max_compression_ratio",
        "import_parse_timeout_seconds",
        "import_max_rows",
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({"database_url": DATABASE_URL, field: 0})


def _rank(values: list[str]) -> list[int]:
    sorted_values = sorted((value, index) for index, value in enumerate(values))
    ranks = [0] * len(values)
    for rank, (_value, index) in enumerate(sorted_values):
        ranks[index] = rank
    return ranks


def _spearman_with_sequence(values: list[str]) -> float:
    ranks = _rank(values)
    count = len(ranks)
    squared_difference = sum((index - rank) ** 2 for index, rank in enumerate(ranks))
    return 1 - (6 * squared_difference) / (count * (count**2 - 1))


def _lcp(left: str, right: str) -> int:
    return next(
        (index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b),
        len(left),
    )


def test_tc26_tc27_tc28_tc29_5000_token_security_statistics() -> None:
    first = [generate_public_token() for _index in range(5_000)]
    second = [generate_public_token() for _index in range(5_000)]
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert len(set(first)) == 5_000
    assert all(len(token) == 26 and set(token) <= alphabet for token in first)
    assert sum(left == right for left, right in zip(first, second, strict=True)) == 0
    threshold = 5 / math.sqrt(4_999)
    assert abs(_spearman_with_sequence(first)) <= threshold
    sequences = [first]
    sequences.extend([token[:length] for token in first] for length in range(1, 7))
    for sequence in sequences:
        assert not all(left < right for left, right in pairwise(sequence))
        assert not all(left > right for left, right in pairwise(sequence))
    adjacent_lcps = [_lcp(left, right) for left, right in pairwise(first)]
    assert max(adjacent_lcps) <= 5
    pair_count = math.comb(5_000, 2)
    measured = {
        length: sum(
            count * (count - 1) // 2
            for count in Counter(token[:length] for token in first).values()
        )
        for length in range(1, 7)
    }
    expected = {length: pair_count / (32**length) for length in range(1, 7)}
    assert expected[3] == pytest.approx(381.3934, rel=1e-4)
    assert expected[5] < 1
    assert expected[6] == pytest.approx(0.01164, rel=1e-3)
    assert all(measured[length] >= 0 for length in range(1, 7))


def test_tc39_tc40_only_first_nonempty_sheet_is_parsed_and_empty_sheets_not_counted() -> None:
    three_nonempty = parse_xlsx(
        build_xlsx(
            [
                [(1, ("编码",)), (2, ("FIRST",))],
                [(1, ("编码",)), (2, ("SECOND",))],
                [(1, ("编码",)), (2, ("THIRD",))],
            ]
        )
    )
    assert three_nonempty.nonempty_sheet_count == 3
    assert [row.values.populated_values() for row in three_nonempty.rows] == [("FIRST",)]
    extra_blank = parse_xlsx(
        build_xlsx(
            [
                [(1, ("编码",)), (2, ("FIRST",))],
                [],
                [(1, (" ",)), (2, ("\t",))],
            ]
        )
    )
    assert extra_blank.nonempty_sheet_count == 1


@pytest.mark.anyio
async def test_timeout_parser_success_returns_complete_workbook() -> None:
    parsed = await parse_xlsx_with_timeout(
        build_xlsx([[(1, ("编码",)), (2, ("A001",))]]),
        timeout_seconds=5,
    )
    assert parsed.headers.populated_values() == ("编码",)
    assert parsed.rows == (
        ParsedRow(
            row_number=2,
            values=ParsedCells.from_dense(("A001",)),
            has_nonblank_cells=True,
        ),
    )


def test_xlsx_worker_memory_limit_uses_absolute_linux_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, values: limits.append((resource_id, values)),
    )

    _set_xlsx_worker_memory_limit(512)

    assert limits == [(resource.RLIMIT_AS, (512, 512))]


def test_xlsx_worker_memory_limit_bounds_growth_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "1000\n")
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, values: limits.append((resource_id, values)),
    )

    _set_xlsx_worker_memory_limit(512)

    assert limits == [(resource.RLIMIT_AS, (1000 * 1024 + 512, 1000 * 1024 + 512))]


def test_xlsx_worker_reports_only_bounded_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.parser._set_xlsx_worker_memory_limit",
        lambda _memory_bytes: None,
    )

    def run(parser: Any) -> tuple[object, ...]:
        receiver, sender = multiprocessing.get_context("spawn").Pipe(duplex=False)
        _parse_worker(sender, b"content", 5_000, 512, parser)
        result = cast(tuple[object, ...], receiver.recv())
        receiver.close()
        return result

    expected = workbook(("编码",), [(2, ("A",))])
    assert run(lambda _content, _max_rows: expected) == ("success", expected)

    def memory_error(_content: bytes, _max_rows: int) -> ParsedWorkbook:
        raise MemoryError

    assert run(memory_error) == ("resource_limit", 512)

    def rejected(_content: bytes, _max_rows: int) -> ParsedWorkbook:
        raise XlsxRejected(
            "bounded",
            "bounded rejection",
            detail={"reason": "bounded"},
            status_code=413,
        )

    assert run(rejected) == (
        "rejected",
        "bounded",
        "bounded rejection",
        {"reason": "bounded"},
        413,
        False,
    )

    def invalid(_content: bytes, _max_rows: int) -> ParsedWorkbook:
        raise RuntimeError("invalid")

    assert run(invalid) == ("failure", "RuntimeError", "invalid")

    def malformed(_content: bytes, _max_rows: int) -> ParsedWorkbook:
        raise ValueError("bad workbook")

    assert run(malformed) == ("failure", "ValueError", "bad workbook")


@pytest.mark.anyio
async def test_xlsx_worker_memory_ceiling_returns_bounded_rejection() -> None:
    memory_budget = 64 * 1024 * 1024
    with pytest.raises(XlsxRejected) as captured:
        await parse_xlsx_with_timeout(
            build_xlsx([[(1, ("编码",)), (2, ("A001",))]]),
            timeout_seconds=5,
            memory_bytes=memory_budget,
            parser=memory_exhausting_xlsx_parser,
        )

    assert captured.value.code == "xlsx_parse_resource_limit"
    assert captured.value.status_code == 413
    assert captured.value.detail == {
        "reason": "parser_memory_limit_exceeded",
        "max_memory_bytes": memory_budget,
    }


def test_parser_cell_variants_coordinates_and_invalid_rows() -> None:
    inline = ElementTree.fromstring(
        '<c xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        't="inlineStr"><is><t>inline</t></is></c>'
    )
    shared = ElementTree.fromstring(
        '<c xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" t="s"><v>0</v></c>'
    )
    boolean = ElementTree.fromstring(
        '<c xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" t="b"><v>1</v></c>'
    )
    empty = ElementTree.fromstring(
        '<c xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
    )
    assert _cell_text(cast(Any, inline), ()) == "inline"
    assert _cell_text(cast(Any, shared), ("shared",)) == "shared"
    assert _cell_text(cast(Any, boolean), ()) == "TRUE"
    assert _cell_text(cast(Any, empty), ()) == ""
    with pytest.raises(ValueError, match="Shared string index"):
        _cell_text(cast(Any, shared), ())
    assert _column_index("AA7", 9) == 26
    assert _column_index("invalid", 9) == 9
    invalid_row = ElementTree.fromstring(
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="bad"><c r="A1" t="inlineStr"><is><t>A</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    with pytest.raises(ValueError, match="row number"):
        _rows(cast(Any, invalid_row), ())


@pytest.mark.anyio
async def test_parser_worker_failure_and_timeout_are_distinct_and_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(XlsxRejected) as invalid:
        await parse_xlsx_with_timeout(b"PK\x03\x04broken", timeout_seconds=5)
    assert invalid.value.code == "invalid_xlsx"
    assert invalid.value.detail["reason"] == "xml_or_workbook_parse_failed"

    async def immediate_timeout(
        awaitable: Coroutine[Any, Any, object],
        *,
        timeout: float,
    ) -> object:
        assert timeout == 0.01
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", immediate_timeout)
    with pytest.raises(XlsxRejected) as timed_out:
        await parse_xlsx_with_timeout(
            build_xlsx([[(1, ("编码",)), (2, ("A",))]]),
            timeout_seconds=0.01,
        )
    assert timed_out.value.code == "xlsx_parse_timeout"
    assert timed_out.value.detail["max_parse_seconds"] == 0.01


@pytest.mark.anyio
async def test_structurally_invalid_zip_is_not_a_resource_limit() -> None:
    with pytest.raises(XlsxRejected) as captured:
        await parse_xlsx_with_timeout(
            build_structurally_invalid_xlsx(),
            timeout_seconds=5,
        )

    assert captured.value.code == "invalid_xlsx"
    assert captured.value.status_code == 422
    assert captured.value.format_error is True
    assert captured.value.detail == {
        "reason": "xml_or_workbook_parse_failed",
        "error_type": "ValueError",
    }


def test_valid_signature_with_broken_zip_is_rejected_as_container() -> None:
    with pytest.raises(XlsxRejected) as captured:
        inspect_xlsx_container(
            b"PK\x03\x04broken",
            max_decompressed_bytes=50 * 1024 * 1024,
            max_compression_ratio=100,
        )
    assert captured.value.code == "invalid_xlsx_container"
    assert captured.value.detail["error_type"] == "BadZipFile"


def synthetic_import_upload() -> UploadFile:
    return UploadFile(
        filename="products.xlsx",
        file=io.BytesIO(b"synthetic"),
        headers=Headers({"content-type": "application/octet-stream"}),
    )


def import_settings() -> Settings:
    return Settings.model_validate({"database_url": DATABASE_URL})


def patch_valid_phase_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def read_upload(_upload: UploadFile, _max_bytes: int) -> bytes:
        return b"synthetic-xlsx"

    async def parse_upload(
        _content: bytes,
        *,
        timeout_seconds: float,
        max_rows: int,
        memory_bytes: int,
    ) -> ParsedWorkbook:
        assert timeout_seconds == 30
        assert max_rows == 5_000
        assert memory_bytes == DEFAULT_IMPORT_PARSE_MEMORY_BYTES
        return workbook(("编码",), [(2, ("A",))])

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.read_upload_with_limit",
        read_upload,
    )
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.inspect_xlsx_container",
        lambda *_args, **_kwargs: ContainerMetrics(100, 200, 2),
    )
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.parse_xlsx_with_timeout",
        parse_upload,
    )


@pytest.mark.anyio
async def test_import_service_success_and_database_duplicate_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_valid_phase_one(monkeypatch)
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.validate_workbook",
        lambda *_args, **_kwargs: (
            (
                ImportCandidate(2, "A", "Alpha"),
                ImportCandidate(3, "DUP", "Duplicate"),
                ImportCandidate(4, "B", "Beta"),
            ),
            1,
            ("本文件含 2 个工作表，仅导入第 1 个",),  # noqa: RUF001
        ),
    )
    calls: list[str] = []

    async def create_candidate(
        _connection: object,
        raw_code: str,
        raw_name: str,
        *,
        actor_id: int,
        request_id: UUID | None = None,
        audit_action: str | None = None,
    ) -> Product:
        del raw_name, actor_id, request_id, audit_action
        calls.append(raw_code)
        if raw_code == "DUP":
            raise AppError("duplicate_product_code", "duplicate", 409)
        return Product(
            id=len(calls),
            code=raw_code,
            name=raw_code,
            public_token="A" * 26,
            status="active",
            current_version_id=None,
        )

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.create_product_in_transaction",
        create_candidate,
    )
    connection = ScriptedConnection([None])
    result = await import_products(
        as_database(ScriptedDatabase(connection)),
        synthetic_import_upload(),
        import_settings(),
        actor_id=9,
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
    )
    assert result == ImportResult(
        success_count=2,
        duplicate_count=2,
        format_error_count=0,
        notices=("本文件含 2 个工作表，仅导入第 1 个",),  # noqa: RUF001
    )
    assert calls == ["A", "DUP", "B"]
    assert "INSERT INTO audit_events" in connection.queries[0]


@pytest.mark.anyio
async def test_import_service_format_security_and_system_failures_are_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_valid_phase_one(monkeypatch)
    format_error = ImportRowError(row=3, reason="坏编码", kind="format")
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.validate_workbook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ImportFormatErrors((format_error,))),
    )
    format_result = await import_products(
        as_database(ScriptedDatabase(ScriptedConnection([None]))),
        synthetic_import_upload(),
        import_settings(),
        actor_id=9,
        request_id=UUID("11111111-1111-1111-1111-111111111111"),
    )
    assert format_result.status == "failure"
    assert format_result.format_error_count == 1
    assert format_result.errors == (format_error,)

    async def security_rejection(_upload: UploadFile, _max_bytes: int) -> bytes:
        raise XlsxRejected(
            "xlsx_too_large",
            "too large",
            detail={"reason": "upload_size_exceeded", "actual_bytes": 11},
            status_code=413,
        )

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.read_upload_with_limit",
        security_rejection,
    )
    security_result = await import_products(
        as_database(ScriptedDatabase(ScriptedConnection([None]))),
        synthetic_import_upload(),
        import_settings(),
        actor_id=9,
        request_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    assert security_result.error_code == "xlsx_too_large"
    assert security_result.http_status == 413
    assert security_result.errors[0].kind == "security"

    patch_valid_phase_one(monkeypatch)
    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.validate_workbook",
        lambda *_args, **_kwargs: ((ImportCandidate(2, "A", "Alpha"),), 0, ()),
    )

    async def token_failure(*_args: object, **_kwargs: object) -> Product:
        raise AppError("token_generation_failed", "token failure", 503)

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.service.create_product_in_transaction",
        token_failure,
    )
    system_result = await import_products(
        as_database(
            ScriptedDatabase(
                ScriptedConnection([]),
                ScriptedConnection([None]),
            )
        ),
        synthetic_import_upload(),
        import_settings(),
        actor_id=9,
        request_id=UUID("33333333-3333-3333-3333-333333333333"),
    )
    assert system_result.error_code == "token_retry_exhausted"
    assert system_result.format_error_count == 0
    assert system_result.errors[0].kind == "system"


@pytest.mark.anyio
async def test_import_router_serializes_complete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ImportResult(
        success_count=1,
        duplicate_count=2,
        format_error_count=1,
        errors=(ImportRowError(row=7, reason="坏行", kind="format"),),
        notices=("提示",),
        status="failure",
        error_code="synthetic",
        http_status=422,
    )

    async def fake_import(*_args: object, **_kwargs: object) -> ImportResult:
        return expected

    monkeypatch.setattr(
        "product_pdf_qr.domains.importer.router.import_products",
        fake_import,
    )
    admin = AuthenticatedAdmin(
        id=9,
        username="unit-admin",
        must_change_password=False,
        session_id=UUID("11111111-1111-1111-1111-111111111111"),
        session_expires_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    response = await import_products_endpoint(
        synthetic_import_upload(),
        admin,
        as_database(ScriptedDatabase()),
        import_settings(),
    )
    assert response.status_code == 422
    assert b'"success_count":1' in response.body
    assert b'"row":7' in response.body
    assert b'"error_code":"synthetic"' in response.body


def test_tc36_tc37_tc38_admin_import_page_contract_is_complete() -> None:
    page = Path("src/product_pdf_qr/templates/import.html").read_text()
    assert 'action="/admin/imports"' in page
    assert 'method="post"' in page
    assert 'enctype="multipart/form-data"' in page
    assert 'name="csrf_token"' in page
    assert 'accept=".xlsx,' in page
    assert all(label in page for label in ("成功新增", "重复跳过", "格式错误", "错误清单"))
    assert "{% for error in result.errors %}" in page
    assert "data-import-error" in page
    assert "slice(" not in page
