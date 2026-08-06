"""Behavioral tests for the production Compose wrapper control flow."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_fresh_certificate_volume_bootstraps_before_full_start(tmp_path: Path) -> None:
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
        fake_bin / "python3",
        """#!/bin/sh
set -eu
cat >/dev/null
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >>"$FAKE_DOCKER_LOG"

if [ "${1:-}" = "run" ]; then
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
      *"test -s /tmp/active/fullchain.pem"*)
        exit 1
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
    result = subprocess.run(
        [str(production_scripts / "prod-compose.sh"), "up", "--detach", "--wait"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    validation_index = next(
        index for index, call in enumerate(calls) if " config --format json" in call
    )
    preflight_index = next(index for index, call in enumerate(calls) if call.startswith("run --rm"))
    certbot_up_index = next(
        index for index, call in enumerate(calls) if call.endswith(" up --detach certbot")
    )
    empty_check_index = next(
        index for index, call in enumerate(calls) if "test -s /tmp/active/fullchain.pem" in call
    )
    bootstrap_index = next(index for index, call in enumerate(calls) if "openssl req -x509" in call)
    full_up_index = max(
        index for index, call in enumerate(calls) if call.endswith(" up --detach --wait")
    )

    assert (
        validation_index
        < preflight_index
        < certbot_up_index
        < empty_check_index
        < bootstrap_index
        < full_up_index
    )
