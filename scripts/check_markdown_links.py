"""Check that repository-local links in Markdown files resolve."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
SKIPPED_SCHEMES = {"http", "https", "mailto"}


def iter_markdown_files() -> list[Path]:
    """Return tracked documentation candidates in deterministic order."""

    return sorted(path for path in ROOT.rglob("*.md") if ".venv" not in path.parts)


def local_link_target(markdown_file: Path, raw_target: str) -> Path | None:
    """Resolve a Markdown target when it points at a repository-local file."""

    target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme in SKIPPED_SCHEMES or not parsed.path:
        return None
    decoded_path = unquote(parsed.path)
    if decoded_path.startswith("/"):
        return ROOT / decoded_path.lstrip("/")
    return markdown_file.parent / decoded_path


def main() -> int:
    """Print every broken local link and return a failing exit code when found."""

    broken: list[str] = []
    for markdown_file in iter_markdown_files():
        contents = markdown_file.read_text(encoding="utf-8")
        for raw_target in LINK_PATTERN.findall(contents):
            target = local_link_target(markdown_file, raw_target)
            if target is not None and not target.resolve().exists():
                relative_file = markdown_file.relative_to(ROOT)
                broken.append(f"{relative_file}: {raw_target}")
    if broken:
        print("Broken local Markdown links:", file=sys.stderr)
        print("\n".join(broken), file=sys.stderr)
        return 1
    print(f"Checked {len(iter_markdown_files())} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
