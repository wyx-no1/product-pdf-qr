"""Behavioral tests for the production Compose wrapper control flow."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    *,
    certificate_state: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    repository = tmp_path / "repository"
    production_scripts = repository / "scripts" / "production"
    production_scripts.mkdir(parents=True)
    for name in ("prod-compose.sh", "bootstrap-certificate.sh"):
        shutil.copy2(Path("scripts/production") / name, production_scripts / name)

    (repository / "compose.prod.yaml").write_text("services: {}\n", encoding="utf-8")
    environment_file = repository / ".env.prod"
    environment_file.write_text(
        f"APP_IMAGE=registry.test/app:v1@sha256:{'0' * 64}\n",
        encoding="utf-8",
    )
    environment_file.chmod(0o600)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
call="$(printf '%s' "$*" | tr '\\n' ' ')"
printf '<call>%s</call>\\n' "$call" >>"$FAKE_DOCKER_LOG"

if [ "${1:-}" = "run" ]; then
  case "$*" in
    *"/validate_compose.py"*)
      cat >/dev/null
      ;;
  esac
  exit 0
fi
[ "${1:-}" = "compose" ] || exit 1
shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file|-f)
      shift 2
      ;;
    *)
      break
      ;;
  esac
done
command="${1:-}"
shift || true
case "$command" in
  config)
    printf '{}\\n'
    ;;
  ps)
    printf 'certbot\\n'
    ;;
  exec)
    case "$*" in
      *"test ! -e /tmp/active"*)
        [ "$FAKE_CERTIFICATE_STATE" = "fresh" ]
        ;;
      *"openssl x509 -in "*"-checkend 86400"*)
        case "$FAKE_CERTIFICATE_STATE" in
          fresh|valid)
            ;;
          *)
            exit 1
            ;;
        esac
        ;;
    esac
    ;;
  up|start|restart)
    ;;
  *)
    exit 1
    ;;
esac
""",
    )

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["FAKE_DOCKER_LOG"] = str(docker_log)
    environment["FAKE_CERTIFICATE_STATE"] = certificate_state
    result = subprocess.run(
        [str(production_scripts / "prod-compose.sh"), "up", "--detach", "--wait"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, docker_log.read_text(encoding="utf-8")


def test_fresh_certificate_volume_bootstraps_before_full_start(tmp_path: Path) -> None:
    result, calls = _run_wrapper(tmp_path, certificate_state="fresh")

    assert result.returncode == 0, result.stderr
    parsed_calls = re.findall(r"<call>(.*?)</call>", calls)

    def find_call(fragment: str, start: int = 0, *, prefix: str = "") -> int:
        return next(
            index
            for index, call in enumerate(parsed_calls[start:], start=start)
            if fragment in call and call.startswith(prefix)
        )

    validation_index = find_call(" config --format json")
    validator_runtime_index = find_call("/validate_compose.py", prefix="run --rm --interactive")
    preflight_index = find_call("--env-file", prefix="run --rm --network none")
    certbot_up_index = find_call(" up --detach certbot", preflight_index + 1)
    empty_check_index = find_call("test ! -e /tmp/active", certbot_up_index + 1)
    bootstrap_index = find_call("openssl req -x509", empty_check_index + 1)
    certificate_validation_index = find_call("-checkend 86400", bootstrap_index + 1)
    full_up_index = find_call(" up --detach --wait", certificate_validation_index + 1)

    assert (
        max(validation_index, validator_runtime_index)
        < preflight_index
        < certbot_up_index
        < empty_check_index
        < bootstrap_index
        < certificate_validation_index
        < full_up_index
    )


@pytest.mark.parametrize(
    "certificate_state",
    ("expired", "wrong-domain", "key-mismatch", "incomplete"),
)
def test_invalid_existing_certificate_fails_closed_without_bootstrap(
    tmp_path: Path,
    certificate_state: str,
) -> None:
    result, calls = _run_wrapper(tmp_path, certificate_state=certificate_state)

    assert result.returncode != 0
    assert "active certificate is invalid" in result.stderr
    assert "test ! -e /tmp/active" in calls
    assert "-checkend 86400" in calls
    assert "openssl req -x509" not in calls
    assert " up --detach --wait" not in calls
