"""Implementation-level safety tests for Issue #40's unchanged G-03 design."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.deploy_rollback.engine import RollbackEngine
from scripts.deploy_rollback.model import (
    NEEDS_ROLLBACK_DECISION,
    REQUIRED_RECOVERY_CONFIG,
    RTO_LIMIT_SECONDS,
    AuditLog,
    PublicationState,
    ReleaseStore,
    RollbackClock,
    RollbackDecisionRequired,
    RollbackSafetyError,
    build_runtime_identity,
    canonical_json,
    choose_rollback_path,
    digest_json,
    format_time,
    release_identity,
    switch_runtime_identity,
    validate_compatibility_validation_plan,
    validate_execution_environment,
    validate_lossy_authorization,
    validate_release_record,
    validate_watermark,
)
from scripts.deploy_rollback.watermark import build_watermark, build_watermark_from_pg_environment

ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)
BACKUP_ID = f"20260812T033000Z-{'b' * 32}"
RELATIONS = {
    "admins",
    "products",
    "pdf_files",
    "pdf_versions",
    "admin_sessions",
    "audit_events",
}
ACTIONS = {
    "login",
    "create",
    "import",
    "upload",
    "history_restore",
    "enable",
    "disable",
    "no_upload",
    "public_read",
    "audit_append",
    "constraints",
    "defaults",
    "enums",
    "triggers",
    "permissions",
}


def _artifact(content: bytes, retained_until: datetime) -> dict[str, Any]:
    return {
        "content_b64": base64.b64encode(content).decode(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "retained_until": format_time(retained_until),
    }


def _artifact_set(
    marker: str,
    *,
    commit: str,
    revision: str,
    migration_sha: str,
    retained_until: datetime,
) -> dict[str, Any]:
    digests = {
        component: hashlib.sha256(f"{marker}:{component}".encode()).hexdigest()
        for component in ("app", "migrate", "proxy", "db", "certbot", "pr2a")
    }
    images = {
        component: f"registry.invalid/{component}:{marker}@sha256:{digest}"
        for component, digest in digests.items()
    }
    runtime = canonical_json({"APP_PORT": "8000", "PUBLIC_DOMAIN": "synthetic.invalid"})
    recovery = {
        name: _artifact(f"{marker}:{name}\n".encode(), retained_until)
        for name in sorted(REQUIRED_RECOVERY_CONFIG)
    }
    return {
        "commit": commit,
        "alembic_revision": revision,
        "migration_sha": migration_sha,
        "images": images,
        "image_evidence": {
            component: {
                "registry_digest": digest,
                "image_id_digest": hashlib.sha256(f"id:{marker}:{component}".encode()).hexdigest(),
                "prefetched": True,
                "retained_until": format_time(retained_until),
            }
            for component, digest in digests.items()
        },
        "recovery_config": recovery,
        "app_config": {"app-runtime.json": _artifact(runtime, retained_until)},
        "secret_references": {
            "database": f"vault://synthetic/database/{marker}",
            "session": f"vault://synthetic/session/{marker}",
            "acme": f"vault://synthetic/acme/{marker}",
        },
    }


def release_record(*, compatible: bool = True, migrated: bool = True) -> dict[str, Any]:
    retained_until = NOW + timedelta(days=30)
    stable = _artifact_set(
        "stable",
        commit="1" * 40,
        revision="rev_stable",
        migration_sha="2" * 40,
        retained_until=retained_until,
    )
    candidate = _artifact_set(
        "candidate",
        commit="3" * 40,
        revision="rev_candidate" if migrated else "rev_stable",
        migration_sha="4" * 40 if migrated else "2" * 40,
        retained_until=retained_until,
    )
    approval = {
        "approval_id": "approval-1",
        "approver": "release-owner",
        "approved_at": format_time(NOW - timedelta(minutes=2)),
    }
    record: dict[str, Any] = {
        "schema_version": 1,
        "release_id": "release-40",
        "environment_id": "synthetic-pr2b",
        "declared_at": format_time(NOW),
        "rollback_window_ends_at": format_time(retained_until),
        "stable": stable,
        "candidate": candidate,
        "pre_release_backup": {
            "backup_id": BACKUP_ID,
            "source_commit": stable["commit"],
            "images": stable["images"],
            "config_sha256": {
                name: artifact["sha256"]
                for name, artifact in sorted(stable["recovery_config"].items())
            },
            "alembic_revision": stable["alembic_revision"],
            "frozen_at": format_time(NOW - timedelta(minutes=32)),
            "completed_at": format_time(NOW - timedelta(minutes=30)),
            "encrypted": True,
            "manifest_authenticated": True,
            "completion_last": True,
            "remote_verified": True,
            "preflight_retrievable": True,
            "g19_watermark_sha256": validate_watermark(watermark()),
        },
        "compatibility": {
            "verdict": "compatible" if compatible else "incompatible",
            "migration_owner": "database-owner",
            "decided_at": format_time(NOW - timedelta(minutes=3)),
            "release_identity": "",
            "approval": deepcopy(approval),
            "full_read_write_actions": {action: compatible for action in sorted(ACTIONS)},
            "g19_rehearsal": {
                "passed": True,
                "run_id": "g19-release-40",
                "release_identity": "",
            },
        },
        "release_approval": deepcopy(approval),
        "stable_isolation_smoke": {
            "passed": True,
            "run_id": "stable-smoke-40",
            "release_identity": "",
        },
        "pre_publication_plan": {
            "roll_forward": None,
            "lossy_recovery": None,
        },
    }
    identity = release_identity(record)
    record["compatibility"]["release_identity"] = identity
    record["compatibility"]["g19_rehearsal"]["release_identity"] = identity
    record["stable_isolation_smoke"]["release_identity"] = identity
    if not compatible:
        record["pre_publication_plan"]["roll_forward"] = {
            "rehearsed": True,
            "run_id": "roll-forward-40",
            "release_identity": identity,
            "approval": deepcopy(approval),
        }
    return record


def watermark(marker: str = "w0") -> dict[str, Any]:
    return {
        "relations": {
            relation: hashlib.sha256(f"{marker}:{relation}".encode()).hexdigest()
            for relation in sorted(RELATIONS)
        },
        "files": [
            {
                "path": "products/p-1/history/v1.pdf",
                "size": 8,
                "sha256": hashlib.sha256(f"{marker}:file".encode()).hexdigest(),
            }
        ],
        "audit_projection": hashlib.sha256(f"{marker}:audit".encode()).hexdigest(),
    }


def validation_watermark(baseline: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(baseline)
    for relation in ("products", "pdf_files", "pdf_versions", "audit_events"):
        result["relations"][relation] = hashlib.sha256(
            f"validation:{relation}:{result['relations'][relation]}".encode()
        ).hexdigest()
    result["audit_projection"] = result["relations"]["audit_events"]
    result["files"].append(
        {
            "path": "synthetic-validation/created-upload.pdf",
            "size": 20,
            "sha256": hashlib.sha256(b"synthetic-validation").hexdigest(),
        }
    )
    result["files"].sort(key=lambda item: item["path"])
    validate_watermark(result)
    return result


def validation_plan(
    record: dict[str, Any],
    baseline: dict[str, Any],
    expected_after: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "release_id": record["release_id"],
        "release_identity": release_identity(record),
        "operation_id": "operation-40",
        "baseline_watermark_sha256": validate_watermark(baseline),
        "expected_after_watermark": expected_after,
        "delta_id": "synthetic-validation-delta-40",
        "full_read_write_actions": {action: True for action in sorted(ACTIONS)},
        "preservation_assertions": {
            "all_preexisting_relation_rows_retained": True,
            "all_preexisting_files_retained": True,
            "all_preexisting_audit_events_retained": True,
            "schema_db_image_volumes_secrets_unchanged": True,
        },
    }


def publication(
    tmp_path: Path,
    record: dict[str, Any],
    *,
    public: bool,
    baseline: dict[str, Any] | None = None,
) -> PublicationState:
    state = PublicationState(
        tmp_path / "publication.json",
        release_id=record["release_id"],
        environment_id=record["environment_id"],
    )
    state.prepare(baseline or watermark(), now=NOW)
    state.advance("migrated", now=NOW + timedelta(seconds=1))
    state.advance("isolated_validated", now=NOW + timedelta(seconds=2))
    if public:
        state.advance("public_cutover", now=NOW + timedelta(seconds=3))
    return state


class FakeHost:
    def __init__(
        self,
        current: dict[str, Any],
        *,
        old_app_passes: bool = True,
        external_passes: bool = True,
    ):
        self.watermark = current
        self.old_app_passes = old_app_passes
        self.external_passes = external_passes
        self.proxy_stopped = True
        self.calls: list[str] = []
        self.active_version = "candidate"
        self.validation_plan_value: dict[str, Any] | None = None
        self.validation_after = validation_watermark(current)

    def retain_exact_artifacts(self, record: Any) -> None:
        validate_release_record(record)
        self.calls.append("retain_exact_artifacts")

    def stop_proxy(self) -> None:
        self.proxy_stopped = True
        self.calls.append("stop_proxy")

    def stop_app(self) -> None:
        self.calls.append("stop_app")

    def start_app(self, identity: Any) -> None:
        self.active_version = identity["version"]
        self.calls.append(f"start_app:{self.active_version}")

    def proxy_is_stopped(self) -> bool:
        self.calls.append("assert_proxy_stopped")
        return self.proxy_stopped

    def full_old_app_validation(self) -> bool:
        self.calls.append("full_old_app_validation")
        if self.old_app_passes:
            self.watermark = self.validation_after
        return self.old_app_passes

    def compatibility_validation_plan(self) -> dict[str, Any]:
        self.calls.append("compatibility_validation_plan")
        assert self.validation_plan_value is not None
        return self.validation_plan_value

    def candidate_validation(self) -> bool:
        self.calls.append("candidate_validation")
        return True

    def authorize_proxy(self, evidence: Any) -> None:
        assert (
            evidence.get("full_read_write_validated") is True
            or evidence.get("roll_forward") is True
        )
        self.calls.append("authorize_proxy")

    def start_proxy(self) -> None:
        self.proxy_stopped = False
        self.calls.append("start_proxy")

    def external_readiness(self) -> bool:
        self.calls.append("external_readiness")
        return self.external_passes

    def current_watermark(self) -> dict[str, Any]:
        self.calls.append("current_watermark")
        return self.watermark


def engine(
    tmp_path: Path,
    record: dict[str, Any],
    state: PublicationState,
    current: dict[str, Any],
    host: FakeHost,
    *,
    isolated: bool,
) -> RollbackEngine:
    host.validation_plan_value = validation_plan(record, current, host.validation_after)
    return RollbackEngine(
        record=record,
        operation_id="operation-40",
        operator="synthetic-operator",
        publication_state=state,
        declared_watermark=current,
        proxy_continuously_isolated=isolated,
        clock=RollbackClock(
            tmp_path / "rto.json",
            operation_id="operation-40",
            release_id=record["release_id"],
            wall_now=lambda: NOW,
            monotonic_now=lambda: 0.0,
        ),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        runtime_identity_path=tmp_path / "runtime.json",
        host=host,
        now=NOW,
    )


def test_t40_01_to_06_release_record_is_complete_exact_and_release_specific(
    tmp_path: Path,
) -> None:
    record = release_record()
    identity = validate_release_record(record)
    store = ReleaseStore(tmp_path / "releases")
    assert store.seal(record) == identity
    assert store.load(record["release_id"]) == record

    invalid_records: list[dict[str, Any]] = []
    missing_image = deepcopy(record)
    del missing_image["stable"]["images"]["db"]
    invalid_records.append(missing_image)
    floating = deepcopy(record)
    floating["stable"]["images"]["app"] = "registry.invalid/app:latest"
    invalid_records.append(floating)
    missing_body = deepcopy(record)
    missing_body["stable"]["app_config"]["app-runtime.json"]["content_b64"] = ""
    invalid_records.append(missing_body)
    missing_secret_reference = deepcopy(record)
    missing_secret_reference["stable"]["secret_references"] = {}
    invalid_records.append(missing_secret_reference)
    stale_rehearsal = deepcopy(record)
    stale_rehearsal["candidate"]["commit"] = "5" * 40
    invalid_records.append(stale_rehearsal)
    old_backup = deepcopy(record)
    old_backup["pre_release_backup"]["completed_at"] = format_time(NOW - timedelta(hours=2))
    invalid_records.append(old_backup)
    for invalid in invalid_records:
        with pytest.raises(RollbackSafetyError):
            validate_release_record(invalid)

    with pytest.raises(RollbackSafetyError, match="already exists"):
        store.seal(record)
    (tmp_path / "releases/release-40.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(RollbackSafetyError, match="digest mismatch"):
        store.load("release-40")


def test_t40_06_incompatible_release_requires_preapproved_recovery_plan() -> None:
    record = release_record(compatible=False)
    validate_release_record(record)
    record["pre_publication_plan"]["roll_forward"] = None
    with pytest.raises(RollbackSafetyError, match="lacks rehearsed"):
        validate_release_record(record)


@pytest.mark.parametrize("stage", ["prepared", "migrated", "isolated_validated"])
def test_t40_07_only_all_positive_pre_public_proofs_select_path_one(
    tmp_path: Path,
    stage: str,
) -> None:
    record = release_record()
    state = PublicationState(
        tmp_path / "publication.json",
        release_id=record["release_id"],
        environment_id=record["environment_id"],
    )
    state.prepare(watermark(), now=NOW)
    if stage in {"migrated", "isolated_validated"}:
        state.advance("migrated", now=NOW + timedelta(seconds=1))
    if stage == "isolated_validated":
        state.advance("isolated_validated", now=NOW + timedelta(seconds=2))
    assert (
        choose_rollback_path(
            state.read,
            watermark(),
            proxy_continuously_isolated=True,
        )
        == "pre_public_restore"
    )


def test_t40_08_to_11_public_unknown_or_hidden_write_always_preserves_data(
    tmp_path: Path,
) -> None:
    record = release_record()
    public = publication(tmp_path / "public", record, public=True)
    assert (
        choose_rollback_path(
            public.read,
            watermark(),
            proxy_continuously_isolated=True,
        )
        == "preserve_forward_data"
    )

    isolated = publication(tmp_path / "isolated", record, public=False)
    assert (
        choose_rollback_path(
            isolated.read,
            watermark("hidden-write"),
            proxy_continuously_isolated=True,
        )
        == "preserve_forward_data"
    )
    assert (
        choose_rollback_path(
            isolated.read,
            watermark(),
            proxy_continuously_isolated=False,
        )
        == "preserve_forward_data"
    )
    isolated.path.write_text("{broken", encoding="utf-8")
    assert (
        choose_rollback_path(
            isolated.read,
            watermark(),
            proxy_continuously_isolated=True,
        )
        == "preserve_forward_data"
    )


def test_t40_10_action_race_is_refused_before_any_host_side_effect(tmp_path: Path) -> None:
    record = release_record()
    state = publication(tmp_path, record, public=False)
    host = FakeHost(watermark())
    rollback = engine(tmp_path, record, state, watermark(), host, isolated=True)
    rollback.expected_action = "APP_ONLY_SWITCH"
    with pytest.raises(RollbackSafetyError, match="changed after lock"):
        rollback.run()
    assert host.calls == []


def test_t40_10_state_is_atomic_monotonic_and_cutover_precedes_proxy(
    tmp_path: Path,
) -> None:
    record = release_record()
    state = PublicationState(
        tmp_path / "publication.json",
        release_id=record["release_id"],
        environment_id=record["environment_id"],
    )
    state.prepare(watermark(), now=NOW)
    with pytest.raises(RollbackSafetyError, match="blocked"):
        state.authorize_proxy_start()
    with pytest.raises(RollbackSafetyError, match="exactly one"):
        state.advance("isolated_validated", now=NOW)
    state.advance("migrated", now=NOW)
    state.advance("isolated_validated", now=NOW)
    state.advance("public_cutover", now=NOW)
    state.authorize_proxy_start()
    assert state.read()["proxy_ever_public"] is True
    tampered = json.loads(state.path.read_text())
    tampered["stage"] = "isolated_validated"
    tampered["proxy_ever_public"] = False
    tampered["history"] = tampered["history"][:-1]
    state.path.write_bytes(canonical_json(tampered))
    with pytest.raises(RollbackSafetyError, match="integrity seal"):
        state.read()


def test_t40_12_to_15_migrated_path_one_only_hands_off_to_pr2a(
    tmp_path: Path,
) -> None:
    record = release_record(migrated=True)
    state = publication(tmp_path, record, public=False)
    host = FakeHost(watermark())
    result = engine(tmp_path, record, state, watermark(), host, isolated=True).run()
    assert result.outcome == "INVOKE_UNMODIFIED_PR2A_RESTORE"
    assert result.backup_id == BACKUP_ID
    assert host.calls == []
    assert AuditLog(tmp_path / "audit.jsonl").verify()[-1]["pr2a_entrypoint"] == (
        "scripts/backup_recovery/restore-run.sh"
    )


def test_t40_12_no_migration_path_one_still_hands_off_to_pr2a(
    tmp_path: Path,
) -> None:
    record = release_record(migrated=False)
    state = publication(tmp_path, record, public=False)
    host = FakeHost(watermark())
    result = engine(tmp_path, record, state, watermark(), host, isolated=True).run()
    assert result.path == "pre_public_restore"
    assert result.outcome == "INVOKE_UNMODIFIED_PR2A_RESTORE"
    assert result.backup_id == BACKUP_ID
    assert host.calls == []


def test_t40_16_to_20_compatible_path_two_preserves_w0_and_w1(
    tmp_path: Path,
) -> None:
    record = release_record()
    state = publication(tmp_path, record, public=True)
    combined = watermark("w0-plus-w1")
    host = FakeHost(combined)
    result = engine(tmp_path, record, state, combined, host, isolated=False).run()
    assert result.path == "preserve_forward_data"
    assert result.outcome == "ROLLED_BACK"
    assert host.active_version == "stable"
    assert host.calls == [
        "retain_exact_artifacts",
        "compatibility_validation_plan",
        "stop_proxy",
        "assert_proxy_stopped",
        "stop_app",
        "start_app:stable",
        "assert_proxy_stopped",
        "current_watermark",
        "full_old_app_validation",
        "current_watermark",
        "authorize_proxy",
        "start_proxy",
        "external_readiness",
    ]
    assert json.loads((tmp_path / "runtime.json").read_text())["version"] == "stable"


def test_t40_01_artifact_failure_precedes_every_service_transition(tmp_path: Path) -> None:
    record = release_record()
    state = publication(tmp_path, record, public=True)
    combined = watermark("w0-plus-w1")

    class MissingArtifactHost(FakeHost):
        def retain_exact_artifacts(self, record: Any) -> None:
            raise RollbackSafetyError("synthetic registry artifact missing")

    host = MissingArtifactHost(combined)
    with pytest.raises(RollbackSafetyError, match="artifact missing"):
        engine(tmp_path, record, state, combined, host, isolated=False).run()
    assert host.calls == []


def test_t40_18_19_runtime_pointer_has_no_db_volume_or_secret_surface(
    tmp_path: Path,
) -> None:
    record = release_record()
    identity = build_runtime_identity(record, version="stable")
    serialized = canonical_json(identity).decode().lower()
    for forbidden in (
        "db_image",
        "database",
        "volume",
        "certificate",
        "secret_reference",
        "password",
        "alembic",
        "pg_restore",
    ):
        assert forbidden not in serialized
    switch_runtime_identity(tmp_path / "runtime.json", identity)
    assert json.loads((tmp_path / "runtime.json").read_text()) == identity


def test_t40_16_17_mutating_validation_requires_predeclared_delta_and_w1_proof(
    tmp_path: Path,
) -> None:
    record = release_record()
    baseline = watermark("w0-plus-w1")
    expected_after = validation_watermark(baseline)
    plan = validation_plan(record, baseline, expected_after)
    assert (
        validate_compatibility_validation_plan(
            plan,
            record=record,
            operation_id="operation-40",
            baseline_watermark=baseline,
        )
        == plan
    )
    changed = deepcopy(plan)
    changed["expected_after_watermark"] = watermark("observed-after-the-fact")
    host = FakeHost(baseline)
    host.validation_plan_value = changed
    state = publication(tmp_path, record, public=True)
    rollback = engine(tmp_path, record, state, baseline, host, isolated=False)
    host.validation_plan_value = changed
    with pytest.raises(RollbackSafetyError, match="predeclared expected delta"):
        rollback.run()
    assert host.active_version == "candidate"


def test_t40_20_22_incompatible_path_two_is_fixed_nonzero_decision(
    tmp_path: Path,
) -> None:
    assert NEEDS_ROLLBACK_DECISION != 0
    record = release_record(compatible=False)
    state = publication(tmp_path, record, public=True)
    combined = watermark("w0-plus-w1")
    host = FakeHost(combined)
    with pytest.raises(RollbackDecisionRequired, match="NEEDS_ROLLBACK_DECISION"):
        engine(tmp_path, record, state, combined, host, isolated=False).run()
    assert host.calls == []
    event = AuditLog(tmp_path / "audit.jsonl").verify()[-1]
    assert event["result"] == "NEEDS_ROLLBACK_DECISION"
    assert event["disposition"] == "keep_candidate_or_proxy_isolated"


def test_t40_21_31_old_app_failure_rolls_precisely_forward_and_stays_proxy_last(
    tmp_path: Path,
) -> None:
    record = release_record()
    state = publication(tmp_path, record, public=True)
    combined = watermark("w0-plus-w1")
    host = FakeHost(combined, old_app_passes=False)
    with pytest.raises(RollbackSafetyError, match="stable app"):
        engine(tmp_path, record, state, combined, host, isolated=False).run()
    assert host.active_version == "candidate"
    assert host.calls.index("candidate_validation") < host.calls.index("start_proxy")
    assert host.calls[-1] == "external_readiness"
    assert json.loads((tmp_path / "runtime.json").read_text())["version"] == "candidate"


def test_t40_23_24_lossy_authorization_is_exact_expiring_and_one_shot(
    tmp_path: Path,
) -> None:
    record = release_record(compatible=False)
    identity = release_identity(record)
    plan = record["pre_publication_plan"]["roll_forward"]
    record["pre_publication_plan"]["roll_forward"] = None
    approval = deepcopy(plan["approval"])
    record["pre_publication_plan"]["lossy_recovery"] = {
        "preapproved": True,
        "authorization_record": "loss-approval-40",
        "loss_start": format_time(NOW - timedelta(minutes=10)),
        "loss_end": format_time(NOW),
        "release_identity": identity,
        "approval": approval,
    }
    challenge = "synthetic-one-time-challenge"
    onsite = "9" * 64
    used_challenges = tmp_path / "used"
    used_challenges.mkdir(mode=0o700)
    authorization = {
        "schema_version": 1,
        "release_id": record["release_id"],
        "release_identity": identity,
        "operation_id": "operation-40",
        "environment_id": record["environment_id"],
        "backup_id": BACKUP_ID,
        "operator": "synthetic-operator",
        "authorization_record": "loss-approval-40",
        "approved_at": format_time(NOW - timedelta(minutes=1)),
        "expires_at": format_time(NOW + timedelta(minutes=5)),
        "one_time_challenge_sha256": hashlib.sha256(challenge.encode()).hexdigest(),
        "loss_start": format_time(NOW - timedelta(minutes=5)),
        "loss_end": format_time(NOW),
        "onsite_retention_sha256": onsite,
        "reconciliation_plan": "compare encrypted retained W1 projections and files",
        "approval": approval,
    }
    common = {
        "record": record,
        "operation_id": "operation-40",
        "environment_id": record["environment_id"],
        "operator": "synthetic-operator",
        "supplied_challenge": challenge,
        "onsite_retention_sha256": onsite,
        "used_challenges": used_challenges,
        "now": NOW,
    }
    assert (
        validate_lossy_authorization(
            authorization,
            **common,
            consume=False,
        )
        == BACKUP_ID
    )
    assert not list(used_challenges.iterdir())
    assert validate_lossy_authorization(authorization, **common) == BACKUP_ID
    with pytest.raises(RollbackSafetyError, match="already used"):
        validate_lossy_authorization(authorization, **common)
    mismatched = deepcopy(authorization)
    mismatched["environment_id"] = "synthetic-other"
    other_used = tmp_path / "other-used"
    other_used.mkdir(mode=0o700)
    with pytest.raises(RollbackSafetyError, match="identity mismatch"):
        validate_lossy_authorization(
            mismatched,
            record=record,
            operation_id="operation-40",
            environment_id=record["environment_id"],
            operator="synthetic-operator",
            supplied_challenge=challenge,
            onsite_retention_sha256=onsite,
            used_challenges=other_used,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("elapsed", "passed"),
    [(14_399, True), (14_400, True), (14_401, False)],
)
def test_t40_28_29_rto_boundary_never_resets(
    tmp_path: Path,
    elapsed: int,
    passed: bool,
) -> None:
    wall = [NOW]
    clock = RollbackClock(
        tmp_path / f"rto-{elapsed}.json",
        operation_id=f"operation-{elapsed}",
        release_id="release-40",
        wall_now=lambda: wall[0],
        monotonic_now=lambda: 0.0,
    )
    started = clock.declare()["started_at"]
    wall[0] = NOW + timedelta(seconds=elapsed)
    assert clock.complete_after_external_readiness() == (elapsed, passed)
    retry = RollbackClock(
        clock.path,
        operation_id=f"operation-{elapsed}",
        release_id="release-40",
        wall_now=lambda: NOW - timedelta(hours=1),
        monotonic_now=lambda: 0.0,
    )
    assert retry.read()["started_at"] == started
    assert retry.elapsed() == elapsed
    alert = clock.path.with_suffix(f"{clock.path.suffix}.rto-alert.json")
    assert alert.exists() is (not passed)
    if alert.exists():
        assert json.loads(alert.read_text())["action"] == "alert_only_no_automatic_data_loss"
    assert RTO_LIMIT_SECONDS == 14_400


def test_t40_26_30_to_32_proxy_failure_is_reisolated_and_candidate_restored(
    tmp_path: Path,
) -> None:
    record = release_record()
    state = publication(tmp_path, record, public=True)
    combined = watermark("w0-plus-w1")
    host = FakeHost(combined, external_passes=False)
    with pytest.raises(RollbackSafetyError, match="external readiness"):
        engine(tmp_path, record, state, combined, host, isolated=False).run()
    assert host.proxy_stopped is True
    assert host.active_version == "candidate"


def test_t40_28_lock_reacquisition_does_not_move_external_readiness_endpoint(
    tmp_path: Path,
) -> None:
    wall = [NOW]
    clock = RollbackClock(
        tmp_path / "rto-endpoint.json",
        operation_id="operation-endpoint",
        release_id="release-40",
        wall_now=lambda: wall[0],
        monotonic_now=lambda: 0.0,
    )
    clock.declare()
    wall[0] = NOW + timedelta(seconds=20_000)
    assert clock.complete_after_external_readiness(
        external_ready_at=NOW + timedelta(seconds=14_400)
    ) == (14_400, True)
    tampered = json.loads(clock.path.read_text())
    tampered["started_at"] = format_time(NOW + timedelta(hours=1))
    clock.path.write_bytes(canonical_json(tampered))
    with pytest.raises(RollbackSafetyError, match="integrity seal"):
        clock.read()


def test_t40_33_to_35_audit_is_append_only_tamper_evident_and_secret_free(
    tmp_path: Path,
) -> None:
    audit = AuditLog(tmp_path / "outside-db/audit.jsonl")
    first = audit.append({"release_id": "release-40", "result": "refused"})
    second = audit.append({"release_id": "release-40", "result": "failed"})
    assert len(audit.verify()) == 2
    assert first != second
    with pytest.raises(RollbackSafetyError, match="sensitive"):
        audit.append({"release_id": "release-40", "token": "not-allowed"})
    lines = audit.path.read_text(encoding="utf-8").splitlines()
    changed = json.loads(lines[0])
    changed["result"] = "success"
    lines[0] = json.dumps(changed)
    audit.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RollbackSafetyError, match="modified"):
        audit.verify()


def test_t40_33_environment_marker_rejects_default_context_and_generic_confirmation(
    tmp_path: Path,
) -> None:
    record = release_record()
    path = tmp_path / "environment.json"
    marker = {
        "schema_version": 1,
        "kind": "synthetic",
        "environment_id": record["environment_id"],
        "docker_context": "synthetic-run-40",
        "compose_project": "synthetic-run-40",
        "resource_prefix": "synthetic-run-40",
        "target_marker": "SYNTHETIC_PR2B_LOCAL_ONLY",
    }
    path.write_bytes(canonical_json(marker))
    path.chmod(0o600)
    assert (
        validate_execution_environment(
            path,
            record=record,
            operation_id="operation-40",
            operator="synthetic-operator",
            confirmation="synthetic:synthetic-pr2b:operation-40",
        )
        == marker
    )
    marker["docker_context"] = "default"
    path.write_bytes(canonical_json(marker))
    with pytest.raises(RollbackSafetyError, match="default context"):
        validate_execution_environment(
            path,
            record=record,
            operation_id="operation-40",
            operator="synthetic-operator",
            confirmation="YES",
        )


def test_t40_35_37_static_surface_reuses_pr2a_and_has_no_data_cleanup_bypass() -> None:
    wrapper = (ROOT / "scripts/deploy_rollback/rollback-run.sh").read_text(encoding="utf-8")
    publication_wrapper = (ROOT / "scripts/deploy_rollback/publication-run.sh").read_text(
        encoding="utf-8"
    )
    authorized = (ROOT / "scripts/deploy_rollback/authorized-lossy-run.sh").read_text(
        encoding="utf-8"
    )
    engine_source = (ROOT / "scripts/deploy_rollback/engine.py").read_text(encoding="utf-8")
    model_source = (ROOT / "scripts/deploy_rollback/model.py").read_text(encoding="utf-8")
    pr2a_restore = (ROOT / "scripts/backup_recovery/restore-run.sh").read_text(encoding="utf-8")
    watermark_overlay = (ROOT / "compose.rollback.yaml").read_text(encoding="utf-8")

    assert '. "$repository_root/scripts/backup_recovery/lock.sh"' in wrapper
    assert '. "$repository_root/scripts/backup_recovery/lock.sh"' in publication_wrapper
    assert publication_wrapper.index("--stage public_cutover") < publication_wrapper.index(
        "run_argv PR2B_PUBLICATION_COMMAND_JSON"
    )
    assert wrapper.index("    verify_exact_artifacts\n    require_stable_checkout") < wrapper.index(
        '"$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 proxy app'
    )
    assert authorized.index("cli verify-artifacts") < authorized.index(
        '"$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 proxy app'
    )
    assert '"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"' in wrapper
    assert wrapper.index("activate_stable_stopped_identity") < wrapper.rindex(
        '"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"'
    )
    assert "restore-database" not in wrapper
    assert "restore-files" not in wrapper
    assert "authorize-lossy-pr2a" in authorized
    assert (
        '"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"' in authorized
    )
    ordinary_restore = wrapper.rindex(
        '"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"'
    )
    assert ordinary_restore < wrapper.index("post_restore_watermark=", ordinary_restore)
    assert wrapper.index("post_restore_watermark=", ordinary_restore) < wrapper.index(
        "cli verify-pr2a-result", ordinary_restore
    )
    assert wrapper.index("cli verify-pr2a-result", ordinary_restore) < wrapper.index(
        "cli complete-pr2a", ordinary_restore
    )
    authorized_restore = authorized.index(
        '"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"'
    )
    assert authorized.index("activate_stable_stopped_identity") < authorized_restore
    assert authorized_restore < authorized.index("post_restore_watermark=", authorized_restore)
    assert "--use-persisted-baseline" not in wrapper
    assert "--use-persisted-baseline" not in authorized
    for forbidden in (
        "alembic downgrade",
        "pg_restore",
        "docker volume rm",
        "compose down -v",
        "rm -rf",
    ):
        assert forbidden not in engine_source
        assert forbidden not in wrapper
        assert forbidden not in authorized
        assert forbidden not in publication_wrapper
    assert "Destructive recovery is reachable only" in model_source
    assert "restore-database --backup-id" in pr2a_restore
    assert "restore-files --backup-id" in pr2a_restore
    assert "\n  rollback-watermark:" in watermark_overlay
    for service in ("proxy", "certbot", "app", "db", "migrate"):
        assert f"\n  {service}:" not in watermark_overlay
    assert "PGUSER: app_backup" in watermark_overlay
    assert "target: /data/files\n        read_only: true" in watermark_overlay
    for forbidden_secret in (
        "manifest-authentication.key",
        "age-identity",
        "rclone.conf",
        "app_migrate_pgpass",
        "restore-authorization",
    ):
        assert forbidden_secret not in watermark_overlay


def test_t40_36_any_exact_release_change_invalidates_old_g19() -> None:
    record = release_record()
    validate_release_record(record)
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda item: item["candidate"].__setitem__("commit", "9" * 40),
        lambda item: item["candidate"].__setitem__("alembic_revision", "another_revision"),
        lambda item: item["candidate"]["images"].__setitem__(
            "app", f"registry.invalid/app:new@sha256:{'8' * 64}"
        ),
        lambda item: item["candidate"]["app_config"]["app-runtime.json"].__setitem__(
            "sha256", "7" * 64
        ),
        lambda item: item["pre_release_backup"].__setitem__("g19_watermark_sha256", "6" * 64),
    )
    for mutation in mutations:
        changed = deepcopy(record)
        mutation(changed)
        with pytest.raises(RollbackSafetyError):
            validate_release_record(changed)


def test_watermark_rejects_counts_mtime_and_incomplete_file_identity() -> None:
    valid = watermark()
    validate_watermark(valid)
    invalid = deepcopy(valid)
    invalid["relations"]["products"] = 1
    with pytest.raises(RollbackSafetyError):
        validate_watermark(invalid)
    invalid = deepcopy(valid)
    invalid["files"][0] = {"path": "v1.pdf", "size": 8, "mtime": 123}
    with pytest.raises(RollbackSafetyError):
        validate_watermark(invalid)
    assert validate_watermark(valid) == digest_json(valid)


def test_watermark_builder_rejects_write_role_and_unbounded_file_root_before_connect() -> None:
    with pytest.raises(RollbackSafetyError, match="app_backup"):
        build_watermark(
            database_url="postgresql://app_rw:synthetic@127.0.0.1/synthetic",
            file_root=Path("/bounded"),
        )


def test_container_watermark_uses_pr2a_read_only_pg_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGUSER", "app_backup")
    monkeypatch.setenv("PGHOST", "db")
    monkeypatch.setenv("PGDATABASE", "synthetic")
    monkeypatch.setenv("PGPASSFILE", "/run/secrets/app_backup_pgpass")
    file_root = tmp_path / "files"
    file_root.mkdir()
    (file_root / "history.pdf").write_bytes(b"synthetic")
    projection: dict[str, list[object]] = {relation: [] for relation in sorted(RELATIONS)}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(projection, sort_keys=True) + "\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    observed = build_watermark_from_pg_environment(file_root=file_root)
    assert set(observed["relations"]) == RELATIONS
    assert observed["audit_projection"] == observed["relations"]["audit_events"]
    with pytest.raises(RollbackSafetyError, match="bounded"):
        build_watermark(
            database_url="postgresql://app_backup:synthetic@127.0.0.1/synthetic",
            file_root=Path("/"),
        )
