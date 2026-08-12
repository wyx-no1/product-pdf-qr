"""Machine-readable entry points for release preparation and rollback."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.deploy_rollback.engine import RollbackEngine
from scripts.deploy_rollback.host import ShellHostAdapter
from scripts.deploy_rollback.model import (
    NEEDS_ROLLBACK_DECISION,
    AuditLog,
    PublicationState,
    ReleaseStore,
    RollbackClock,
    RollbackDecisionRequired,
    RollbackSafetyError,
    atomic_write,
    canonical_json,
    choose_rollback_path,
    format_time,
    parse_time,
    read_json_file,
    release_identity,
    validate_execution_environment,
    validate_lossy_authorization,
    validate_release_record,
    validate_watermark,
)


def _json(path: Path) -> dict[str, Any]:
    return read_json_file(path)


def _state(options: argparse.Namespace, record: dict[str, Any]) -> PublicationState:
    return PublicationState(
        options.state,
        release_id=str(record["release_id"]),
        environment_id=str(record["environment_id"]),
    )


def _clock(options: argparse.Namespace, record: dict[str, Any]) -> RollbackClock:
    return RollbackClock(
        options.rto_state,
        operation_id=options.operation_id,
        release_id=str(record["release_id"]),
    )


def _record(options: argparse.Namespace) -> dict[str, Any]:
    return ReleaseStore(options.store).load(options.release_id)


def _assert_shared_lease() -> None:
    """Prove this CLI child is running under the PR2A owner shell."""

    owner_value = os.environ.get("PR2B_LEASE_OWNER_PID", "")
    lock_directory = Path(os.environ.get("PR2A_LOCK_DIRECTORY", "/run/lock/product-pdf-qr-pr2a"))
    try:
        expected_owner = int(owner_value)
        observed_owner = int((lock_directory / "owner").read_text(encoding="ascii").strip())
    except (OSError, ValueError) as error:
        raise RollbackSafetyError("shared PR2A owner lease is required") from error
    try:
        owner_process_group = os.getpgid(expected_owner)
    except ProcessLookupError as error:
        raise RollbackSafetyError("shared PR2A owner process is not alive") from error
    if (
        expected_owner <= 1
        or observed_owner != expected_owner
        or owner_process_group != os.getpgrp()
    ):
        raise RollbackSafetyError("shared PR2A owner lease does not belong to this operation")


def _add_record_lookup(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--release-id", required=True)


def _add_operation(parser: argparse.ArgumentParser) -> None:
    _add_record_lookup(parser)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--rto-state", type=Path, required=True)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="deploy-rollback")
    commands = root.add_subparsers(dest="command", required=True)

    seal = commands.add_parser("seal-release")
    seal.add_argument("--record", type=Path, required=True)
    seal.add_argument("--store", type=Path, required=True)

    validate = commands.add_parser("validate-release")
    _add_record_lookup(validate)

    environment = commands.add_parser("validate-environment")
    _add_record_lookup(environment)
    environment.add_argument("--operation-id", required=True)
    environment.add_argument("--operator", required=True)
    environment.add_argument("--environment-marker", type=Path, required=True)
    environment.add_argument("--environment-confirmation", required=True)

    artifacts = commands.add_parser("verify-artifacts")
    _add_record_lookup(artifacts)
    artifacts.add_argument("--operation-id", required=True)
    artifacts.add_argument("--operator", required=True)
    artifacts.add_argument("--environment-marker", type=Path, required=True)
    artifacts.add_argument("--environment-confirmation", required=True)
    artifacts.add_argument("--repository-root", type=Path, required=True)

    prepare = commands.add_parser("prepare-publication")
    _add_record_lookup(prepare)
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--watermark", type=Path, required=True)

    advance = commands.add_parser("advance-publication")
    _add_record_lookup(advance)
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument(
        "--stage",
        choices=("migrated", "isolated_validated", "public_cutover"),
        required=True,
    )

    proxy = commands.add_parser("authorize-proxy-start")
    _add_record_lookup(proxy)
    proxy.add_argument("--state", type=Path, required=True)

    select = commands.add_parser("select-path")
    _add_record_lookup(select)
    select.add_argument("--state", type=Path, required=True)
    select.add_argument("--watermark", type=Path, required=True)
    select.add_argument("--proxy-continuously-isolated", choices=("yes", "no"), required=True)

    declare = commands.add_parser("declare-rollback")
    _add_operation(declare)

    inspect = commands.add_parser("inspect-action")
    _add_operation(inspect)
    inspect.add_argument("--state", type=Path, required=True)
    inspect.add_argument("--watermark", type=Path, required=True)
    inspect.add_argument("--proxy-continuously-isolated", choices=("yes", "no"), required=True)

    execute = commands.add_parser("execute")
    _add_operation(execute)
    execute.add_argument("--state", type=Path, required=True)
    execute.add_argument("--watermark", type=Path, required=True)
    execute.add_argument("--proxy-continuously-isolated", choices=("yes", "no"), required=True)
    execute.add_argument("--operator", required=True)
    execute.add_argument("--audit", type=Path, required=True)
    execute.add_argument("--runtime-identity", type=Path, required=True)
    execute.add_argument("--repository-root", type=Path, required=True)
    execute.add_argument("--environment-marker", type=Path, required=True)
    execute.add_argument("--environment-confirmation", required=True)
    execute.add_argument("--result", type=Path, required=True)
    execute.add_argument(
        "--expected-action",
        choices=(
            "APP_ONLY_SWITCH",
            "INVOKE_UNMODIFIED_PR2A_RESTORE",
            "NEEDS_ROLLBACK_DECISION",
        ),
        required=True,
    )

    stable = commands.add_parser("verify-stable-checkout")
    _add_record_lookup(stable)
    stable.add_argument("--checkout", type=Path, required=True)
    stable.add_argument("--production-env", type=Path, required=True)
    stable.add_argument("--backup-env", type=Path, required=True)

    verify_pr2a = commands.add_parser("verify-pr2a-result")
    _add_operation(verify_pr2a)
    verify_pr2a.add_argument("--state", type=Path, required=True)
    verify_pr2a.add_argument("--watermark", type=Path, required=True)
    verify_pr2a.add_argument("--audit", type=Path, required=True)
    verify_pr2a.add_argument("--operator", required=True)
    verify_pr2a.add_argument("--authorized-data-loss", action="store_true")
    verify_pr2a.add_argument("--authorization-reference")

    readiness = commands.add_parser("verify-external-readiness")
    _add_record_lookup(readiness)
    readiness.add_argument("--operation-id", required=True)
    readiness.add_argument("--operator", required=True)
    readiness.add_argument("--environment-marker", type=Path, required=True)
    readiness.add_argument("--environment-confirmation", required=True)
    readiness.add_argument("--repository-root", type=Path, required=True)

    for name in ("fence-engage", "fence-assert", "fence-publish"):
        fence = commands.add_parser(name)
        _add_record_lookup(fence)
        fence.add_argument("--operation-id", required=True)
        fence.add_argument("--operator", required=True)
        fence.add_argument("--environment-marker", type=Path, required=True)
        fence.add_argument("--environment-confirmation", required=True)
        fence.add_argument("--fence-state", type=Path, required=True)

    complete = commands.add_parser("complete-pr2a")
    _add_operation(complete)
    complete.add_argument("--state", type=Path, required=True)
    complete.add_argument("--watermark", type=Path, required=True)
    complete.add_argument("--external-ready-at", required=True)
    complete.add_argument("--audit", type=Path, required=True)
    complete.add_argument("--operator", required=True)
    complete.add_argument("--authorized-data-loss", action="store_true")
    complete.add_argument("--authorization-reference")

    lossy = commands.add_parser("authorize-lossy-pr2a")
    _add_operation(lossy)
    lossy.add_argument("--authorization", type=Path, required=True)
    lossy.add_argument("--operator", required=True)
    lossy.add_argument("--environment-id", required=True)
    lossy.add_argument("--environment-marker", type=Path, required=True)
    lossy.add_argument("--environment-confirmation", required=True)
    lossy.add_argument("--challenge-file", type=Path, required=True)
    lossy.add_argument("--onsite-retention-sha256", required=True)
    lossy.add_argument("--used-challenges", type=Path, required=True)
    lossy.add_argument("--audit", type=Path, required=True)
    lossy.add_argument("--preflight-only", action="store_true")

    verify_audit = commands.add_parser("verify-audit")
    verify_audit.add_argument("--audit", type=Path, required=True)
    capture = commands.add_parser("capture-watermark")
    capture.add_argument("--repository-root", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    return root


def _env_value(path: Path, name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RollbackSafetyError(f"identity environment is unavailable: {path}") from error
    matches = [line.split("=", maxsplit=1)[1] for line in lines if line.startswith(f"{name}=")]
    if len(matches) != 1 or not matches[0]:
        raise RollbackSafetyError(f"{name} must occur exactly once in identity environment")
    return matches[0]


def _verify_stable_checkout(
    record: dict[str, Any],
    checkout: Path,
    production_env: Path,
    backup_env: Path,
) -> dict[str, Any]:
    if not checkout.is_absolute() or checkout == Path("/") or checkout.is_symlink():
        raise RollbackSafetyError("stable checkout must be a bounded absolute directory")
    result = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=normal"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout:
        raise RollbackSafetyError("stable checkout must be clean")
    result = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    stable = record["stable"]
    if result.returncode != 0 or result.stdout.strip() != stable["commit"]:
        raise RollbackSafetyError("stable checkout commit does not match backup identity")
    for name, artifact in stable["recovery_config"].items():
        target = checkout / name
        if target.is_symlink() or not target.is_file():
            raise RollbackSafetyError("stable recovery config is unavailable")
        import hashlib

        if hashlib.sha256(target.read_bytes()).hexdigest() != artifact["sha256"]:
            raise RollbackSafetyError("stable recovery config does not match backup identity")
    images = stable["images"]
    expected = {
        "APP_IMAGE": images["app"],
        "DB_IMAGE": images["db"],
        "PROXY_IMAGE": images["proxy"],
        "CERTBOT_IMAGE": images["certbot"],
    }
    if images["migrate"] != images["app"]:
        raise RollbackSafetyError("PR1 migrate must use the exact app image")
    for name, value in expected.items():
        if _env_value(production_env, name) != value:
            raise RollbackSafetyError(f"stable {name} does not match backup identity")
    if _env_value(backup_env, "BACKUP_IMAGE") != images["pr2a"]:
        raise RollbackSafetyError("stable PR2A image does not match backup identity")
    return {
        "status": "stable_identity_verified",
        "release_identity": release_identity(record),
        "backup_id": record["pre_release_backup"]["backup_id"],
        "restore_entrypoint": str(checkout / "scripts/backup_recovery/restore-run.sh"),
    }


def _fence_result(
    environment: dict[str, Any],
    *,
    action: str,
    record: dict[str, Any],
    operation_id: str,
) -> dict[str, Any]:
    command = environment["publication_fence_command"]
    result = subprocess.run(
        [
            *command,
            action,
            "--release-id",
            str(record["release_id"]),
            "--operation-id",
            operation_id,
            "--environment-id",
            str(record["environment_id"]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RollbackSafetyError(f"publication fence {action} failed")
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RollbackSafetyError("publication fence returned invalid JSON") from error
    common = {
        "schema_version": 1,
        "release_id": record["release_id"],
        "operation_id": operation_id,
        "environment_id": record["environment_id"],
    }
    if not isinstance(observed, dict) or any(
        observed.get(key) != value for key, value in common.items()
    ):
        raise RollbackSafetyError("publication fence evidence is cross-bound")
    return observed


def _assert_fence_evidence(
    observed: dict[str, Any],
    *,
    engaged: bool,
    fence_id: str | None = None,
) -> str:
    expected_fields = {
        "schema_version",
        "release_id",
        "operation_id",
        "environment_id",
        "fence_id",
        "engaged",
        "customer_traffic_blocked",
        "readiness_probe_allowed",
    }
    if set(observed) != expected_fields:
        raise RollbackSafetyError("publication fence evidence schema mismatch")
    observed_fence_id = observed["fence_id"]
    if (
        not isinstance(observed_fence_id, str)
        or not observed_fence_id
        or (fence_id is not None and observed_fence_id != fence_id)
        or observed["engaged"] is not engaged
        or observed["customer_traffic_blocked"] is not engaged
        or observed["readiness_probe_allowed"] is not True
    ):
        raise RollbackSafetyError("publication fence is not in the required state")
    return observed_fence_id


def _fence_environment(options: argparse.Namespace, record: dict[str, Any]) -> dict[str, Any]:
    return validate_execution_environment(
        options.environment_marker,
        record=record,
        operation_id=options.operation_id,
        operator=options.operator,
        confirmation=options.environment_confirmation,
    )


def run(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    result: Any
    if options.command == "seal-release":
        document = _json(options.record)
        identity = ReleaseStore(options.store).seal(document)
        result = {"status": "sealed", "release_identity": identity}
    elif options.command == "verify-audit":
        events = AuditLog(options.audit).verify()
        result = {"status": "ok", "events": len(events)}
    elif options.command == "capture-watermark":
        watermark = ShellHostAdapter(repository_root=options.repository_root).current_watermark()
        atomic_write(options.output, canonical_json(watermark))
        result = {"status": "captured", "watermark_sha256": validate_watermark(watermark)}
    else:
        record = _record(options)
        if options.command == "validate-release":
            result = {
                "status": "ok",
                "release_identity": validate_release_record(record),
            }
        elif options.command == "validate-environment":
            environment = validate_execution_environment(
                options.environment_marker,
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                confirmation=options.environment_confirmation,
            )
            result = {
                "status": "environment_verified",
                "docker_context": environment["docker_context"],
            }
        elif options.command == "verify-artifacts":
            _assert_shared_lease()
            environment = validate_execution_environment(
                options.environment_marker,
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                confirmation=options.environment_confirmation,
            )
            ShellHostAdapter(
                repository_root=options.repository_root,
                docker_context=str(environment["docker_context"]),
            ).retain_exact_artifacts(record)
            result = {
                "status": "exact_artifacts_retained",
                "release_identity": release_identity(record),
            }
        elif options.command == "prepare-publication":
            _assert_shared_lease()
            initial_watermark = _json(options.watermark)
            if (
                validate_watermark(initial_watermark)
                != record["pre_release_backup"]["g19_watermark_sha256"]
            ):
                raise RollbackSafetyError("publication W0 does not match the immediate PR2A point")
            state = _state(options, record).prepare(initial_watermark, now=datetime.now(tz=UTC))
            result = {"status": state["stage"], "release_identity": release_identity(record)}
        elif options.command == "advance-publication":
            _assert_shared_lease()
            state = _state(options, record).advance(options.stage, now=datetime.now(tz=UTC))
            result = {"status": state["stage"], "release_identity": release_identity(record)}
        elif options.command == "authorize-proxy-start":
            _assert_shared_lease()
            _state(options, record).authorize_proxy_start()
            result = {"status": "authorized", "stage": "public_cutover"}
        elif options.command == "select-path":
            path = choose_rollback_path(
                _state(options, record).read,
                _json(options.watermark),
                proxy_continuously_isolated=options.proxy_continuously_isolated == "yes",
            )
            result = {"status": "selected", "path": path}
        elif options.command == "declare-rollback":
            state = _clock(options, record).declare()
            result = {
                "status": "declared",
                "operation_id": state["operation_id"],
                "started_at": state["started_at"],
            }
        elif options.command == "inspect-action":
            _clock(options, record).declare()
            path = choose_rollback_path(
                _state(options, record).read,
                _json(options.watermark),
                proxy_continuously_isolated=options.proxy_continuously_isolated == "yes",
            )
            if (
                path == "preserve_forward_data"
                and record["compatibility"]["verdict"] != "compatible"
            ):
                action = "NEEDS_ROLLBACK_DECISION"
            elif path == "pre_public_restore":
                action = "INVOKE_UNMODIFIED_PR2A_RESTORE"
            else:
                action = "APP_ONLY_SWITCH"
            result = {"status": "inspected", "path": path, "action": action}
        elif options.command == "execute":
            environment = validate_execution_environment(
                options.environment_marker,
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                confirmation=options.environment_confirmation,
            )
            if options.expected_action == "APP_ONLY_SWITCH":
                _assert_shared_lease()
            engine = RollbackEngine(
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                publication_state=_state(options, record),
                declared_watermark=_json(options.watermark),
                proxy_continuously_isolated=options.proxy_continuously_isolated == "yes",
                clock=_clock(options, record),
                audit=AuditLog(options.audit),
                runtime_identity_path=options.runtime_identity,
                host=ShellHostAdapter(
                    repository_root=options.repository_root,
                    docker_context=str(environment["docker_context"]),
                ),
                expected_action=options.expected_action,
            )
            try:
                execution = engine.run()
            except RollbackDecisionRequired:
                result = {
                    "status": "NEEDS_ROLLBACK_DECISION",
                    "operation_id": options.operation_id,
                    "release_id": record["release_id"],
                }
                atomic_write(options.result, canonical_json(result))
                print(json.dumps(result, sort_keys=True))
                return NEEDS_ROLLBACK_DECISION
            result = {
                "status": execution.outcome,
                "operation_id": execution.operation_id,
                "release_id": execution.release_id,
                "path": execution.path,
                "elapsed_seconds": execution.elapsed_seconds,
                "rto_passed": execution.rto_passed,
                "backup_id": execution.backup_id,
            }
            atomic_write(options.result, canonical_json(result))
        elif options.command == "verify-stable-checkout":
            result = _verify_stable_checkout(
                record,
                options.checkout,
                options.production_env,
                options.backup_env,
            )
        elif options.command == "fence-engage":
            _assert_shared_lease()
            environment = _fence_environment(options, record)
            observed = _fence_result(
                environment,
                action="engage",
                record=record,
                operation_id=options.operation_id,
            )
            fence_id = _assert_fence_evidence(observed, engaged=True)
            atomic_write(
                options.fence_state,
                canonical_json(
                    {
                        "schema_version": 1,
                        "release_id": record["release_id"],
                        "release_identity": release_identity(record),
                        "operation_id": options.operation_id,
                        "environment_id": record["environment_id"],
                        "fence_id": fence_id,
                        "engaged_at": format_time(datetime.now(tz=UTC)),
                    }
                ),
            )
            result = {"status": "publication_fenced", "fence_id": fence_id}
        elif options.command == "fence-assert":
            _assert_shared_lease()
            state = read_json_file(options.fence_state)
            if (
                state.get("release_id") != record["release_id"]
                or state.get("release_identity") != release_identity(record)
                or state.get("operation_id") != options.operation_id
                or state.get("environment_id") != record["environment_id"]
            ):
                raise RollbackSafetyError("publication fence state is cross-bound")
            environment = _fence_environment(options, record)
            observed = _fence_result(
                environment,
                action="status",
                record=record,
                operation_id=options.operation_id,
            )
            fence_id = _assert_fence_evidence(
                observed,
                engaged=True,
                fence_id=str(state.get("fence_id", "")),
            )
            result = {"status": "publication_fenced", "fence_id": fence_id}
        elif options.command == "fence-publish":
            _assert_shared_lease()
            state = read_json_file(options.fence_state)
            if (
                state.get("release_id") != record["release_id"]
                or state.get("release_identity") != release_identity(record)
                or state.get("operation_id") != options.operation_id
                or state.get("environment_id") != record["environment_id"]
            ):
                raise RollbackSafetyError("publication fence state is cross-bound")
            environment = _fence_environment(options, record)
            observed = _fence_result(
                environment,
                action="publish",
                record=record,
                operation_id=options.operation_id,
            )
            expected_fields = {
                "schema_version",
                "release_id",
                "operation_id",
                "environment_id",
                "fence_id",
                "engaged",
                "customer_traffic_blocked",
                "readiness_probe_allowed",
                "external_ready",
                "published_at",
            }
            if (
                set(observed) != expected_fields
                or observed.get("fence_id") != state.get("fence_id")
                or observed.get("engaged") is not False
                or observed.get("customer_traffic_blocked") is not False
                or observed.get("readiness_probe_allowed") is not True
                or observed.get("external_ready") is not True
            ):
                raise RollbackSafetyError("publication fence did not atomically publish readiness")
            published_at = parse_time(str(observed.get("published_at")), field="published_at")
            result = {
                "status": "published",
                "fence_id": state["fence_id"],
                "published_at": format_time(published_at),
            }
        elif options.command == "verify-pr2a-result":
            _assert_shared_lease()
            expected = _state(options, record).read()["baseline_watermark"]
            observed = _json(options.watermark)
            observed_sha256 = validate_watermark(observed)
            if canonical_json(expected) != canonical_json(observed):
                raise RollbackSafetyError("PR2A result differs from complete pre-release watermark")
            if options.authorized_data_loss and not options.authorization_reference:
                raise RollbackSafetyError(
                    "authorized data loss verification needs its approval reference"
                )
            AuditLog(options.audit).append(
                {
                    "schema_version": 1,
                    "release_id": record["release_id"],
                    "release_identity": release_identity(record),
                    "operation_id": options.operation_id,
                    "environment_id": record["environment_id"],
                    "operator": options.operator,
                    "at": format_time(datetime.now(tz=UTC)),
                    "path": (
                        "preserve_forward_data"
                        if options.authorized_data_loss
                        else "pre_public_restore"
                    ),
                    "stage": "pr2a_post_restore_isolated_verification",
                    "result": "post_restore_watermark_verified",
                    "backup_id": record["pre_release_backup"]["backup_id"],
                    "watermark_sha256": observed_sha256,
                    "human_authorization_reference": (options.authorization_reference or "none"),
                }
            )
            result = {
                "status": "post_restore_watermark_verified",
                "watermark_sha256": observed_sha256,
            }
        elif options.command == "verify-external-readiness":
            _assert_shared_lease()
            environment = validate_execution_environment(
                options.environment_marker,
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                confirmation=options.environment_confirmation,
            )
            if not ShellHostAdapter(
                repository_root=options.repository_root,
                docker_context=str(environment["docker_context"]),
            ).external_readiness():
                raise RollbackSafetyError("external readiness failed")
            result = {
                "status": "external_ready",
                "external_ready_at": format_time(datetime.now(tz=UTC)),
            }
        elif options.command == "complete-pr2a":
            _assert_shared_lease()
            expected = _state(options, record).read()["baseline_watermark"]
            observed = _json(options.watermark)
            observed_sha256 = validate_watermark(observed)
            if canonical_json(expected) != canonical_json(observed):
                raise RollbackSafetyError("PR2A result differs from complete pre-release watermark")
            expected_path = (
                "preserve_forward_data" if options.authorized_data_loss else "pre_public_restore"
            )
            verification_exists = any(
                event.get("release_id") == record["release_id"]
                and event.get("operation_id") == options.operation_id
                and event.get("path") == expected_path
                and event.get("result") == "post_restore_watermark_verified"
                and event.get("watermark_sha256") == observed_sha256
                and (
                    not options.authorized_data_loss
                    or event.get("human_authorization_reference") == options.authorization_reference
                )
                for event in AuditLog(options.audit).verify()
            )
            if not verification_exists:
                raise RollbackSafetyError(
                    "fresh isolated post-restore watermark verification is required"
                )
            external_ready_at = parse_time(
                options.external_ready_at,
                field="external_ready_at",
            )
            elapsed, passed = _clock(options, record).complete_after_external_readiness(
                external_ready_at=external_ready_at
            )
            if options.authorized_data_loss and not options.authorization_reference:
                raise RollbackSafetyError(
                    "authorized data loss completion needs its approval reference"
                )
            completion_result = (
                "COMPLETED_AUTHORIZED_DATA_LOSS" if options.authorized_data_loss else "completed"
            )
            AuditLog(options.audit).append(
                {
                    "schema_version": 1,
                    "release_id": record["release_id"],
                    "release_identity": release_identity(record),
                    "operation_id": options.operation_id,
                    "environment_id": record["environment_id"],
                    "operator": options.operator,
                    "at": format_time(datetime.now(tz=UTC)),
                    "path": (
                        "preserve_forward_data"
                        if options.authorized_data_loss
                        else "pre_public_restore"
                    ),
                    "stage": "pr2a_external_readiness",
                    "result": completion_result if passed else "RTO_EXCEEDED",
                    "backup_id": record["pre_release_backup"]["backup_id"],
                    "watermark_sha256": observed_sha256,
                    "human_authorization_reference": (options.authorization_reference or "none"),
                }
            )
            if not passed:
                raise RollbackSafetyError("G-19 RTO exceeded after safe PR2A recovery")
            result = {
                "status": completion_result,
                "elapsed_seconds": elapsed,
                "rto_passed": True,
            }
        elif options.command == "authorize-lossy-pr2a":
            _assert_shared_lease()
            validate_execution_environment(
                options.environment_marker,
                record=record,
                operation_id=options.operation_id,
                operator=options.operator,
                confirmation=options.environment_confirmation,
            )
            authorization = _json(options.authorization)
            decision_exists = any(
                event.get("release_id") == record["release_id"]
                and event.get("operation_id") == options.operation_id
                and event.get("result") == "NEEDS_ROLLBACK_DECISION"
                for event in AuditLog(options.audit).verify()
            )
            if not decision_exists:
                raise RollbackSafetyError(
                    "lossy recovery requires its persisted human decision node"
                )
            challenge_path = options.challenge_file
            if challenge_path.is_symlink() or not challenge_path.is_file():
                raise RollbackSafetyError("one-time challenge file is unavailable")
            mode = challenge_path.stat().st_mode
            if mode & 0o077:
                raise RollbackSafetyError("one-time challenge file must be mode 0600")
            challenge = challenge_path.read_text(encoding="utf-8").strip()
            backup_id = validate_lossy_authorization(
                authorization,
                record=record,
                operation_id=options.operation_id,
                environment_id=options.environment_id,
                operator=options.operator,
                supplied_challenge=challenge,
                onsite_retention_sha256=options.onsite_retention_sha256,
                used_challenges=options.used_challenges,
                now=datetime.now(tz=UTC),
                consume=not options.preflight_only,
            )
            if options.preflight_only:
                result = {
                    "status": "LOSSY_PREFLIGHT_PASSED_NO_TARGET_CHANGE",
                    "backup_id": backup_id,
                    "operation_id": options.operation_id,
                }
                print(json.dumps(result, sort_keys=True))
                return 0
            AuditLog(options.audit).append(
                {
                    "schema_version": 1,
                    "release_id": record["release_id"],
                    "release_identity": release_identity(record),
                    "operation_id": options.operation_id,
                    "environment_id": options.environment_id,
                    "operator": options.operator,
                    "at": format_time(datetime.now(tz=UTC)),
                    "path": "preserve_forward_data",
                    "stage": "authorized_lossy_pr2a_handoff",
                    "result": "AUTHORIZED_DATA_LOSS_NOT_ORDINARY_ROLLBACK",
                    "backup_id": backup_id,
                    "human_authorization_reference": authorization["authorization_record"],
                    "onsite_retention_sha256": options.onsite_retention_sha256,
                }
            )
            result = {
                "status": "AUTHORIZED_PR2A_HANDOFF",
                "backup_id": backup_id,
                "operation_id": options.operation_id,
            }
        else:
            raise RollbackSafetyError("unknown command")
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> None:
    try:
        status = run()
    except (RollbackSafetyError, OSError, ValueError) as error:
        print(
            json.dumps({"status": "refused", "error": str(error)}, sort_keys=True), file=sys.stderr
        )
        raise SystemExit(2) from error
    raise SystemExit(status)


if __name__ == "__main__":
    main()
