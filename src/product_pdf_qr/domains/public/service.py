"""Public-token state resolution and miss-only enumeration limiting."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from product_pdf_qr.database import Database
from product_pdf_qr.domains.storage import StorageService

PublicState = Literal["missing", "disabled", "unuploaded", "available"]


@dataclass(frozen=True, slots=True)
class PublicDocument:
    """The public state projection, with file data only for the available state."""

    state: PublicState
    path: Path | None = None
    original_filename: str | None = None
    size_bytes: int | None = None


class PublicMissLimiter:
    """Track only missing-token probes so valid shared-IP scans are not penalized."""

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._misses: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def is_limited(self, source: str) -> bool:
        """Return whether the source already exceeded the active miss window."""

        async with self._lock:
            misses = self._active_misses(source)
            return len(misses) >= self.limit

    async def register_miss(self, source: str) -> bool:
        """Record one miss and return whether this miss reaches the threshold."""

        async with self._lock:
            misses = self._active_misses(source)
            was_limited = len(misses) >= self.limit
            misses.append(self._clock())
            return not was_limited and len(misses) >= self.limit

    def _active_misses(self, source: str) -> deque[float]:
        now = self._clock()
        cutoff = now - self.window_seconds
        misses = self._misses[source]
        while misses and misses[0] <= cutoff:
            misses.popleft()
        return misses


async def resolve_public_document(
    database: Database,
    storage: StorageService,
    public_token: str,
) -> PublicDocument:
    """Resolve all four public states with disabled checked before current version."""

    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                p.status,
                p.current_version_id,
                v.original_filename,
                f.storage_path,
                f.size_bytes
            FROM products AS p
            LEFT JOIN pdf_versions AS v
              ON v.product_id = p.id
             AND v.id = p.current_version_id
            LEFT JOIN pdf_files AS f ON f.id = v.pdf_file_id
            WHERE p.public_token = %s
            """,
            (public_token,),
        )
        row = await cursor.fetchone()
    if row is None:
        return PublicDocument("missing")
    if str(row["status"]) == "disabled":
        return PublicDocument("disabled")
    if row["current_version_id"] is None:
        return PublicDocument("unuploaded")
    if row["storage_path"] is None or row["original_filename"] is None:
        raise RuntimeError("Current PDF metadata cannot be resolved")
    path = storage.resolve_formal_path(str(row["storage_path"]))
    if not path.is_file():
        raise RuntimeError("Current PDF file is missing")
    return PublicDocument(
        "available",
        path=path,
        original_filename=str(row["original_filename"]),
        size_bytes=cast(int, row["size_bytes"]),
    )
