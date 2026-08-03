from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CommandFailed(RuntimeError):
    """A subprocess failed."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        command = " ".join(args)
        detail = stderr.strip() or "no stderr"
        super().__init__(f"command failed ({returncode}): {command}: {detail}")
        self.args_list = tuple(args)
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        result = CommandResult(completed.stdout, completed.stderr, completed.returncode)
        if check and result.returncode != 0:
            raise CommandFailed(args, result.returncode, result.stderr)
        return result


class GitRepository:
    def __init__(self, path: Path, runner: CommandRunner | None = None) -> None:
        self.path = path.resolve()
        self.runner = runner or CommandRunner()
        top_level = self.git("rev-parse", "--show-toplevel").stdout.strip()
        self.root = Path(top_level).resolve()

    def git(self, *args: str, check: bool = True) -> CommandResult:
        return self.runner.run(("git", "-C", str(self.path), *args), check=check)

    def fetch(self, remote: str = "origin", *refspecs: str) -> None:
        self.git("fetch", "--prune", remote, *refspecs)

    def remote_slug(self, remote: str = "origin") -> str:
        url = self.git("remote", "get-url", remote).stdout.strip()
        if url.startswith("git@github.com:"):
            slug = url.removeprefix("git@github.com:")
        elif "github.com/" in url:
            slug = url.split("github.com/", maxsplit=1)[1]
        else:
            raise ValueError(f"{remote} is not a GitHub remote: {url}")
        return slug.removesuffix(".git").strip("/")

    def common_git_dir(self) -> Path:
        raw = self.git("rev-parse", "--git-common-dir").stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def project_name(self) -> str:
        first_line = self.git("worktree", "list", "--porcelain").stdout.partition("\n")[0]
        if not first_line.startswith("worktree "):
            raise ValueError("git worktree list did not return a primary worktree")
        return Path(first_line.removeprefix("worktree ")).name


def append_json_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
