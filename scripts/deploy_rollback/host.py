"""Host adapter for the fixed app-only rollback sequence.

Every externally supplied command is a JSON argv array and is executed without a
shell.  The adapter has no API for SQL, Alembic, db services, or business volumes.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.deploy_rollback.model import (
    ALLOWED_APP_ENVIRONMENT,
    RUNNING_IMAGES,
    RollbackSafetyError,
    validate_release_record,
    validate_watermark,
)


def _command_from_environment(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RollbackSafetyError(f"{name} must be a JSON argv array") from error
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise RollbackSafetyError(f"{name} must be a non-empty JSON argv array")
    return value


class ShellHostAdapter:
    """Execute the PR1 app/proxy controls while preserving every data component."""

    def __init__(self, *, repository_root: Path, docker_context: str | None = None):
        if (
            not repository_root.is_absolute()
            or repository_root == Path("/")
            or not (repository_root / "scripts/production/prod-compose.sh").is_file()
        ):
            raise RollbackSafetyError("repository root is invalid")
        self.repository_root = repository_root
        self.production = repository_root / "scripts/production/prod-compose.sh"
        if docker_context in {"default", "desktop-linux"}:
            raise RollbackSafetyError("default Docker context is forbidden")
        self.docker_context = docker_context
        self._active_environment: dict[str, str] = {}

    def _run(
        self,
        arguments: list[str],
        *,
        environment: Mapping[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        process_environment = dict(os.environ)
        if self.docker_context is not None:
            process_environment["DOCKER_CONTEXT"] = self.docker_context
        if environment is not None:
            process_environment.update(environment)
        result = subprocess.run(
            arguments,
            cwd=self.repository_root,
            env=process_environment,
            check=False,
            capture_output=capture,
            text=True,
        )
        if result.returncode != 0:
            raise RollbackSafetyError(f"bounded host command failed: {Path(arguments[0]).name}")
        return result

    def retain_exact_artifacts(self, record: Mapping[str, Any]) -> None:
        validate_release_record(record)
        for version in ("stable", "candidate"):
            artifacts = record[version]
            for component in sorted(RUNNING_IMAGES):
                reference = artifacts["images"][component]
                expected_id = artifacts["image_evidence"][component]["image_id_digest"]
                result = self._run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
                    capture=True,
                )
                if result.stdout.strip() != f"sha256:{expected_id}":
                    raise RollbackSafetyError(
                        f"{version}.{component} local image ID differs from sealed evidence"
                    )

    def stop_proxy(self) -> None:
        self._run([str(self.production), "stop", "--timeout", "60", "proxy"])

    def stop_app(self) -> None:
        self._run([str(self.production), "stop", "--timeout", "60", "app"])

    def _environment(self, identity: Mapping[str, Any]) -> dict[str, str]:
        configs = identity["app_config"]
        combined: dict[str, str] = {"APP_IMAGE": str(identity["app_image"])}
        for artifact in configs.values():
            decoded = base64.b64decode(artifact["content_b64"], validate=True)
            value = json.loads(decoded)
            if not isinstance(value, dict) or set(value) - ALLOWED_APP_ENVIRONMENT:
                raise RollbackSafetyError("runtime config is outside the non-secret allowlist")
            for key, item in value.items():
                if not isinstance(item, str) or not item:
                    raise RollbackSafetyError("runtime config value is invalid")
                previous = combined.get(key)
                if previous is not None and previous != item:
                    raise RollbackSafetyError("runtime config artifacts conflict")
                combined[key] = item
        return combined

    def start_app(self, identity: Mapping[str, Any]) -> None:
        self._active_environment = self._environment(identity)
        self._run(
            [
                str(self.production),
                "up",
                "--detach",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "app",
            ],
            environment=self._active_environment,
        )

    def proxy_is_stopped(self) -> bool:
        result = self._run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env.prod",
                "-f",
                "compose.prod.yaml",
                "ps",
                "--status",
                "running",
                "--quiet",
                "proxy",
            ],
            environment=self._active_environment,
            capture=True,
        )
        return not result.stdout.strip()

    def nonmutating_old_app_validation(self) -> bool:
        try:
            self._run(_command_from_environment("PR2B_NONMUTATING_VALIDATION_COMMAND_JSON"))
        except RollbackSafetyError:
            return False
        return True

    def candidate_validation(self) -> bool:
        try:
            self._run(_command_from_environment("PR2B_CANDIDATE_VALIDATION_COMMAND_JSON"))
        except RollbackSafetyError:
            return False
        return True

    def authorize_proxy(self, evidence: Mapping[str, Any]) -> None:
        path_value = os.environ.get("PR2B_PROXY_AUTHORIZATION_PATH", "")
        path = Path(path_value)
        if not path.is_absolute() or path == Path("/"):
            raise RollbackSafetyError("proxy authorization path must be bounded and absolute")
        from scripts.deploy_rollback.model import atomic_write, canonical_json

        atomic_write(path, canonical_json(dict(evidence)))

    def start_proxy(self) -> None:
        self._run(
            [str(self.production), "up", "--detach", "--no-deps", "--wait", "proxy"],
            environment=self._active_environment,
        )

    def external_readiness(self) -> bool:
        try:
            self._run(_command_from_environment("PR2B_EXTERNAL_READINESS_COMMAND_JSON"))
        except RollbackSafetyError:
            return False
        return True

    def current_watermark(self) -> Mapping[str, Any]:
        result = self._run(
            _command_from_environment("PR2B_WATERMARK_COMMAND_JSON"),
            capture=True,
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RollbackSafetyError("watermark command did not emit JSON") from error
        if not isinstance(value, dict):
            raise RollbackSafetyError("watermark command must emit a JSON object")
        validate_watermark(value)
        return value
