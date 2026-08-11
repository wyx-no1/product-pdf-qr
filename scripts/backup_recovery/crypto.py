"""Streaming age encryption and authenticated decryption."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, cast

from scripts.backup_recovery.model import SafetyError
from scripts.backup_recovery.remote import Remote


class AgeCipher:
    """Locked age-v1/X25519 authenticated encryption."""

    def __init__(self, recipient: str) -> None:
        if not recipient.startswith("age1") or any(character.isspace() for character in recipient):
            raise SafetyError("invalid age X25519 recipient")
        self.recipient = recipient

    @staticmethod
    def version() -> str:
        """Return the installed tool version and reject drift."""

        result = subprocess.run(["age", "--version"], text=True, capture_output=True, check=False)
        if result.returncode != 0 or "1.3.1" not in result.stdout + result.stderr:
            raise SafetyError("age 1.3.1 is required")
        return "1.3.1"

    def encrypt_file(self, source: Path, remote: Remote, key: str) -> tuple[int, str, int, str]:
        """Encrypt one source object directly to the immutable remote adapter."""

        plaintext_size = source.stat().st_size
        plaintext_digest = hashlib.sha256()
        process = subprocess.Popen(
            ["age", "--encrypt", "--recipient", self.recipient],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None:
            raise SafetyError("age pipe unavailable")
        age_stdin = cast(BinaryIO, process.stdin)
        age_stdout = cast(BinaryIO, process.stdout)

        failure: list[BaseException] = []

        def feed() -> None:
            try:
                with source.open("rb") as stream, age_stdin:
                    while chunk := stream.read(1024 * 1024):
                        plaintext_digest.update(chunk)
                        age_stdin.write(chunk)
            except BaseException as error:
                failure.append(error)
                process.kill()

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        try:
            cipher_size, cipher_digest = remote.upload_stream(age_stdout, key)
        except BaseException:
            process.kill()
            process.wait()
            thread.join()
            raise
        thread.join()
        return_code = process.wait()
        if failure:
            raise SafetyError(f"age source read failed: {failure[0]}")
        if return_code != 0:
            raise SafetyError("age encryption failed")
        return plaintext_size, plaintext_digest.hexdigest(), cipher_size, cipher_digest

    def encrypt_file_to_path(self, source: Path, target: Path) -> tuple[int, str, int, str]:
        """Durably stage ciphertext so split immutable publication can resume."""

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".encrypting",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        plaintext_digest = hashlib.sha256()
        ciphertext_digest = hashlib.sha256()
        plaintext_size = 0
        ciphertext_size = 0
        process = subprocess.Popen(
            ["age", "--encrypt", "--recipient", self.recipient],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None:
            os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise SafetyError("age pipe unavailable")
        age_stdin = cast(BinaryIO, process.stdin)
        failure: list[BaseException] = []

        def feed() -> None:
            nonlocal plaintext_size
            try:
                with source.open("rb") as stream, age_stdin:
                    while chunk := stream.read(1024 * 1024):
                        plaintext_size += len(chunk)
                        plaintext_digest.update(chunk)
                        age_stdin.write(chunk)
            except BaseException as error:
                failure.append(error)
                process.kill()

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while chunk := process.stdout.read(1024 * 1024):
                    output.write(chunk)
                    ciphertext_size += len(chunk)
                    ciphertext_digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            thread.join()
            if failure or process.wait() != 0:
                raise SafetyError("age source read or encryption failed")
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
            if process.poll() is None:
                process.kill()
                process.wait()
            thread.join()
            temporary.unlink(missing_ok=True)
        return (
            plaintext_size,
            plaintext_digest.hexdigest(),
            ciphertext_size,
            ciphertext_digest.hexdigest(),
        )

    def encrypt_command(
        self, command: Sequence[str], remote: Remote, key: str
    ) -> tuple[int, str, int, str]:
        """Stream command output through age without a plaintext persistent file."""

        source = subprocess.Popen(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=None
        )
        if source.stdout is None:
            raise SafetyError("plaintext source pipe unavailable")
        age = subprocess.Popen(
            ["age", "--encrypt", "--recipient", self.recipient],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if age.stdin is None or age.stdout is None:
            source.kill()
            raise SafetyError("age pipe unavailable")
        source_stdout = cast(BinaryIO, source.stdout)
        age_stdin = cast(BinaryIO, age.stdin)
        age_stdout = cast(BinaryIO, age.stdout)
        plaintext_digest = hashlib.sha256()
        plaintext_size = 0
        failure: list[BaseException] = []

        def feed() -> None:
            nonlocal plaintext_size
            try:
                with source_stdout, age_stdin:
                    while chunk := source_stdout.read(1024 * 1024):
                        plaintext_size += len(chunk)
                        plaintext_digest.update(chunk)
                        age_stdin.write(chunk)
            except BaseException as error:
                failure.append(error)
                source.kill()
                age.kill()

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        try:
            cipher_size, cipher_digest = remote.upload_stream(age_stdout, key)
        except BaseException:
            source.kill()
            age.kill()
            source.wait()
            age.wait()
            thread.join()
            raise
        thread.join()
        source_return_code = source.wait()
        age_return_code = age.wait()
        if failure or source_return_code != 0 or age_return_code != 0:
            raise SafetyError("plaintext producer or age encryption failed")
        return plaintext_size, plaintext_digest.hexdigest(), cipher_size, cipher_digest

    def encrypt_bytes(self, payload: bytes, remote: Remote, key: str) -> tuple[int, str]:
        """Encrypt a bounded manifest and publish only ciphertext."""

        process = subprocess.run(
            ["age", "--encrypt", "--recipient", self.recipient],
            input=payload,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise SafetyError("manifest encryption failed")
        return remote.upload_stream(io.BytesIO(process.stdout), key)


def decrypt_file_to_hash(ciphertext: Path, identity: Path) -> tuple[int, str]:
    """Authenticate/decrypt a ciphertext while retaining no plaintext output."""

    process = subprocess.Popen(
        ["age", "--decrypt", "--identity", str(identity), str(ciphertext)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise SafetyError("age decrypt pipe unavailable")
    digest = hashlib.sha256()
    size = 0
    while chunk := process.stdout.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    if process.wait() != 0:
        raise SafetyError("age authentication failed")
    return size, digest.hexdigest()


def decrypt_small(ciphertext: Path, identity: Path, *, limit: int = 16 * 1024 * 1024) -> bytes:
    """Decrypt a bounded manifest/config control object into memory."""

    process = subprocess.run(
        ["age", "--decrypt", "--identity", str(identity), str(ciphertext)],
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise SafetyError("age authentication failed")
    if len(process.stdout) > limit:
        raise SafetyError("decrypted control object exceeds limit")
    return process.stdout


def decrypt_to_process(ciphertext: Path, identity: Path, command: Sequence[str]) -> None:
    """Authenticate and stream plaintext to one restore command."""

    age = subprocess.Popen(
        ["age", "--decrypt", "--identity", str(identity), str(ciphertext)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if age.stdout is None:
        raise SafetyError("age decrypt pipe unavailable")
    target = subprocess.Popen(list(command), stdin=age.stdout)
    age.stdout.close()
    target_return_code = target.wait()
    age_return_code = age.wait()
    if age_return_code != 0 or target_return_code != 0:
        raise SafetyError("authenticated restore stream failed")


def decrypt_to_path(ciphertext: Path, identity: Path, target: Path) -> tuple[int, str]:
    """Restore one file atomically; callers must already have retained the site."""

    temporary = target.with_name(f".{target.name}.restore")
    missing_parents: list[Path] = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing_parents):
        _set_restore_acl(directory, directory=True)
    if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
        raise SafetyError("restore temporary is unsafe")
    if temporary.is_file():
        temporary.unlink()
    process = subprocess.Popen(
        ["age", "--decrypt", "--identity", str(identity), str(ciphertext)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None:
        raise SafetyError("age decrypt pipe unavailable")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as output:
            while chunk := process.stdout.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if process.wait() != 0:
            raise SafetyError("age authentication failed")
        os.chmod(temporary, 0o660)
        _set_restore_acl(temporary, directory=False)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return size, digest.hexdigest()


def _set_restore_acl(path: Path, *, directory: bool) -> None:
    """Give only the app and restore UIDs durable bidirectional access."""

    if (
        os.environ.get("BACKUP_SYNTHETIC") == "1"
        and os.environ.get("RESTORE_SYNTHETIC_BIND_MOUNT") == "1"
    ):
        # Docker Desktop bind mounts do not expose Linux POSIX ACLs. The same
        # rehearsal first exercises this function with production UIDs on a
        # real named volume; only the later host-UID database matrix uses this
        # explicit synthetic compatibility path.
        return
    entries = "u:10001:rwx,u:10002:rwx"
    if directory:
        entries += ",d:u:10001:rwx,d:u:10002:rwx"
    else:
        entries = "u:10001:rw-,u:10002:rw-"
    result = subprocess.run(
        ["setfacl", "-m", entries, str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SafetyError("restored object ACL initialization failed")
