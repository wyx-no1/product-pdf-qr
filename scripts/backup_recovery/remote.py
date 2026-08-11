"""Write-only publication surface and local synthetic S3-compatible adapter."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO

from scripts.backup_recovery.model import SafetyError


def _safe_key(key: str) -> None:
    path = Path(key)
    if not key or key.startswith("/") or ".." in path.parts or "\\" in key:
        raise SafetyError("unsafe remote object key")


class Remote:
    """Minimal upload identity: write, list/read/verify; deliberately no delete."""

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def upload_file(self, source: Path, key: str) -> None:
        raise NotImplementedError

    def upload_stream(self, source: BinaryIO, key: str) -> tuple[int, str]:
        raise NotImplementedError

    def download_file(self, key: str, destination: Path) -> None:
        raise NotImplementedError

    def read_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def size_and_sha256(self, key: str) -> tuple[int, str]:
        raise NotImplementedError

    def list_keys(self, prefix: str) -> list[str]:
        raise NotImplementedError


class LocalRemote(Remote):
    """Local isolated adapter with the same immutable completion protocol."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise SafetyError("synthetic remote must be an existing absolute directory")
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        _safe_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise SafetyError("remote key escapes root")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def upload_file(self, source: Path, key: str) -> None:
        with source.open("rb") as stream:
            self.upload_stream(stream, key)

    def upload_stream(self, source: BinaryIO, key: str) -> tuple[int, str]:
        target = self._path(key)
        if target.exists():
            raise SafetyError("immutable remote object already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".uploading",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    size += len(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory_descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        return size, digest.hexdigest()

    def download_file(self, key: str, destination: Path) -> None:
        source = self._path(key)
        if not source.is_file():
            raise SafetyError("remote object does not exist")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def size_and_sha256(self, key: str) -> tuple[int, str]:
        path = self._path(key)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def list_keys(self, prefix: str) -> list[str]:
        _safe_key(prefix)
        parent = self._path(prefix)
        if not parent.exists():
            return []
        return sorted(
            path.relative_to(self.root).as_posix() for path in parent.rglob("*") if path.is_file()
        )


class RcloneRemote(Remote):
    """S3-compatible adapter using one mode-0600 upload-only rclone config."""

    def __init__(self, remote: str, config: Path) -> None:
        if ":" not in remote or remote.startswith(("local:", "/", ".")):
            raise SafetyError("production remote must be an rclone S3 remote")
        if not config.is_file() or config.stat().st_mode & 0o077:
            raise SafetyError("rclone config must exist with mode 0600")
        self.remote = remote.rstrip("/")
        self.config = config

    def _target(self, key: str) -> str:
        _safe_key(key)
        return f"{self.remote}/{key}"

    def _run(
        self, *arguments: str, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["rclone", "--config", str(self.config), *arguments],
            input=input_bytes,
            capture_output=True,
            check=False,
        )

    def exists(self, key: str) -> bool:
        result = self._run("lsjson", self._target(key), "--stat")
        return result.returncode == 0

    def upload_file(self, source: Path, key: str) -> None:
        result = self._run("copyto", "--immutable", str(source), self._target(key))
        if result.returncode != 0:
            raise SafetyError("remote immutable upload failed")

    def upload_stream(self, source: BinaryIO, key: str) -> tuple[int, str]:
        process = subprocess.Popen(
            [
                "rclone",
                "--config",
                str(self.config),
                "rcat",
                "--immutable",
                self._target(key),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None:
            raise SafetyError("remote upload stream unavailable")
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := source.read(1024 * 1024):
                process.stdin.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            process.stdin.close()
            return_code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        if return_code != 0:
            raise SafetyError("remote stream upload failed")
        return size, digest.hexdigest()

    def download_file(self, key: str, destination: Path) -> None:
        result = self._run("copyto", self._target(key), str(destination))
        if result.returncode != 0:
            raise SafetyError("remote download failed")

    def read_bytes(self, key: str) -> bytes:
        process = subprocess.Popen(
            ["rclone", "--config", str(self.config), "cat", self._target(key)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise SafetyError("remote control stream unavailable")
        try:
            output = bytes(process.stdout.read(8 * 1024 * 1024 + 1))
        except BaseException:
            process.kill()
            process.wait()
            raise
        if len(output) > 8 * 1024 * 1024:
            process.kill()
            process.wait()
            raise SafetyError("remote control object exceeds limit")
        process.stdout.close()
        process.wait()
        if process.returncode != 0:
            raise SafetyError("remote read failed")
        return output

    def size_and_sha256(self, key: str) -> tuple[int, str]:
        process = subprocess.Popen(
            ["rclone", "--config", str(self.config), "cat", self._target(key)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if process.stdout is None:
            raise SafetyError("remote verification stream unavailable")
        digest = hashlib.sha256()
        size = 0
        try:
            while chunk := process.stdout.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        except BaseException:
            process.kill()
            process.wait()
            raise
        process.stdout.close()
        if process.wait() != 0:
            raise SafetyError("remote verification read failed")
        return size, digest.hexdigest()

    def list_keys(self, prefix: str) -> list[str]:
        result = self._run("lsf", self._target(prefix), "--recursive", "--files-only")
        if result.returncode != 0:
            raise SafetyError("remote list failed")
        return [f"{prefix.rstrip('/')}/{line}" for line in result.stdout.decode().splitlines()]


def remote_from_environment(value: str, *, synthetic: bool, config: Path | None) -> Remote:
    """Refuse a local adapter unless the caller explicitly marks synthetic scope."""

    if value.startswith("local:"):
        if not synthetic:
            raise SafetyError("local remote is allowed only for synthetic rehearsal")
        return LocalRemote(Path(value.removeprefix("local:")))
    if config is None:
        raise SafetyError("rclone config is required")
    return RcloneRemote(value, config)
