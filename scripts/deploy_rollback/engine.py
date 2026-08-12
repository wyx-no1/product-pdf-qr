"""Proxy-last rollback state machine with an injectable host adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from scripts.deploy_rollback.model import (
    AuditLog,
    RollbackClock,
    RollbackDecisionRequired,
    RollbackSafetyError,
    build_runtime_identity,
    choose_rollback_path,
    digest_json,
    format_time,
    release_identity,
    switch_runtime_identity,
    validate_release_record,
    validate_watermark,
)

PATH_ONE = "pre_public_restore"
PATH_TWO = "preserve_forward_data"


class HostAdapter(Protocol):
    """Narrow non-destructive app/proxy command surface consumed by path two."""

    def retain_exact_artifacts(self, record: Mapping[str, Any]) -> None:
        """Prove both exact artifact sets remain available."""

    def stop_proxy(self) -> None:
        """Stop the only public entrypoint."""

    def stop_app(self) -> None:
        """Stop app without touching migrate, db, files, certificates, or secrets."""

    def start_app(self, identity: Mapping[str, Any]) -> None:
        """Start one exact app identity while proxy remains stopped."""

    def proxy_is_stopped(self) -> bool:
        """Return positive isolation evidence."""

    def full_old_app_validation(self) -> bool:
        """Exercise every supported read/write action against the forward schema."""

    def candidate_validation(self) -> bool:
        """Exercise the exact candidate after a failed old-app validation."""

    def authorize_proxy(self, evidence: Mapping[str, Any]) -> None:
        """Authorize publication only after isolated functional evidence."""

    def start_proxy(self) -> None:
        """Start the proxy as the final service transition."""

    def external_readiness(self) -> bool:
        """Verify the externally reachable release identity."""

    def current_watermark(self) -> Mapping[str, Any]:
        """Compute all relation, historical file, and audit projections."""


@dataclass(frozen=True)
class RollbackResult:
    """Machine-readable successful result or handoff to unchanged PR2A."""

    operation_id: str
    release_id: str
    path: str
    outcome: str
    elapsed_seconds: int
    rto_passed: bool
    backup_id: str | None = None


class RollbackEngine:
    """Select data semantics first, then execute only the allowed command surface."""

    def __init__(
        self,
        *,
        record: Mapping[str, Any],
        operation_id: str,
        operator: str,
        publication_state: Any,
        declared_watermark: Mapping[str, Any],
        proxy_continuously_isolated: bool,
        clock: RollbackClock,
        audit: AuditLog,
        runtime_identity_path: Path,
        host: HostAdapter,
        expected_action: str | None = None,
        now: datetime | None = None,
    ):
        self.record = record
        self.operation_id = operation_id
        self.operator = operator
        self.publication_state = publication_state
        self.declared_watermark = declared_watermark
        self.proxy_continuously_isolated = proxy_continuously_isolated
        self.clock = clock
        self.audit = audit
        self.runtime_identity_path = runtime_identity_path
        self.host = host
        self.expected_action = expected_action
        self.now = now or datetime.now(tz=UTC)
        self.identity = validate_release_record(record)
        validate_watermark(declared_watermark)
        if clock.operation_id != operation_id or clock.release_id != record["release_id"]:
            raise RollbackSafetyError("RTO clock is cross-bound")
        if not operator.strip():
            raise RollbackSafetyError("operator is required")

    def _audit(self, *, result: str, stage: str, path: str, **details: Any) -> None:
        stable = self.record["stable"]
        candidate = self.record["candidate"]
        lossy_plan = self.record["pre_publication_plan"]["lossy_recovery"]
        self.audit.append(
            {
                "schema_version": 1,
                "release_id": self.record["release_id"],
                "release_identity": self.identity,
                "operation_id": self.operation_id,
                "environment_id": self.record["environment_id"],
                "operator": self.operator,
                "at": format_time(self.now),
                "path": path,
                "stage": stage,
                "result": result,
                "stable_app_digest": stable["images"]["app"].rsplit("@sha256:", maxsplit=1)[1],
                "candidate_app_digest": candidate["images"]["app"].rsplit("@sha256:", maxsplit=1)[
                    1
                ],
                "backup_id": self.record["pre_release_backup"]["backup_id"],
                "compatibility": self.record["compatibility"]["verdict"],
                "release_approval": self.record["release_approval"]["approval_id"],
                "compatibility_approval": self.record["compatibility"]["approval"]["approval_id"],
                "human_authorization_reference": (
                    lossy_plan.get("authorization_record")
                    if isinstance(lossy_plan, dict)
                    else "none"
                ),
                "watermark_sha256": digest_json(self.declared_watermark),
                **details,
            }
        )

    def decide(self) -> str:
        """Persist the operation start before making a conservative path decision."""

        self.clock.declare()
        path = choose_rollback_path(
            self.publication_state.read,
            self.declared_watermark,
            proxy_continuously_isolated=self.proxy_continuously_isolated,
        )
        self._audit(result="selected", stage="path_decision", path=path)
        return path

    def run(self) -> RollbackResult:
        """Run a safe automatic path or stop at a fixed non-zero decision node."""

        path = self.decide()
        if path == PATH_TWO and self.record["compatibility"]["verdict"] != "compatible":
            action = "NEEDS_ROLLBACK_DECISION"
        elif path == PATH_ONE:
            action = "INVOKE_UNMODIFIED_PR2A_RESTORE"
        else:
            action = "APP_ONLY_SWITCH"
        if self.expected_action is not None and action != self.expected_action:
            self._audit(
                result="refused_raced_path_change",
                stage="path_decision",
                path=path,
                expected_action=self.expected_action,
                observed_action=action,
            )
            raise RollbackSafetyError("rollback action changed after lock strategy selection")
        if path == PATH_ONE:
            return self._path_one()
        return self._path_two()

    def _path_one(self) -> RollbackResult:
        # PR2B performs no restore phase. The caller must activate the stable
        # checkout/config identity, then invoke this exact unchanged PR2A command.
        # This is unconditional for path one so its data semantics always mean B0.
        elapsed = self.clock.elapsed()
        backup_id = str(self.record["pre_release_backup"]["backup_id"])
        self._audit(
            result="pr2a_handoff_required",
            stage="stable_identity_verified",
            path=PATH_ONE,
            pr2a_entrypoint="scripts/backup_recovery/restore-run.sh",
        )
        return RollbackResult(
            operation_id=self.operation_id,
            release_id=str(self.record["release_id"]),
            path=PATH_ONE,
            outcome="INVOKE_UNMODIFIED_PR2A_RESTORE",
            elapsed_seconds=elapsed,
            rto_passed=elapsed <= 14_400,
            backup_id=backup_id,
        )

    def _path_two(self) -> RollbackResult:
        compatibility = self.record["compatibility"]
        if compatibility["verdict"] != "compatible":
            self._audit(
                result="NEEDS_ROLLBACK_DECISION",
                stage="human_decision",
                path=PATH_TWO,
                disposition="keep_candidate_or_proxy_isolated",
            )
            raise RollbackDecisionRequired("NEEDS_ROLLBACK_DECISION")
        return self._switch_app(path=PATH_TWO, expected_watermark=self.declared_watermark)

    def _switch_app(
        self,
        *,
        path: str,
        expected_watermark: Mapping[str, Any],
    ) -> RollbackResult:
        """Fixed proxy-last switch; any old-app failure rolls precisely forward."""

        validate_watermark(expected_watermark)
        stable_identity = build_runtime_identity(self.record, version="stable")
        candidate_identity = build_runtime_identity(self.record, version="candidate")
        switched = False
        app_stopped = False
        proxy_stop_attempted = False
        stage = "artifact_retention"
        try:
            self.host.retain_exact_artifacts(self.record)
            stage = "proxy_isolation"
            proxy_stop_attempted = True
            self.host.stop_proxy()
            if not self.host.proxy_is_stopped():
                raise RollbackSafetyError("proxy isolation could not be proved")
            stage = "app_stop"
            self.host.stop_app()
            app_stopped = True
            stage = "stable_atomic_switch"
            switch_runtime_identity(self.runtime_identity_path, stable_identity)
            switched = True
            stage = "stable_app_start"
            self.host.start_app(stable_identity)
            if not self.host.proxy_is_stopped():
                raise RollbackSafetyError("proxy opened before isolated validation")
            stage = "stable_full_read_write_validation"
            if not self.host.full_old_app_validation():
                raise RollbackSafetyError("stable app full read/write validation failed")
            stage = "complete_watermark_validation"
            observed = self.host.current_watermark()
            validate_watermark(observed)
            if digest_json(observed) != digest_json(expected_watermark):
                raise RollbackSafetyError(
                    "data, file, or audit watermark changed during app switch"
                )
            evidence = {
                "release_identity": release_identity(self.record),
                "operation_id": self.operation_id,
                "path": path,
                "watermark_sha256": digest_json(observed),
                "full_read_write_validated": True,
            }
            stage = "proxy_authorization"
            self.host.authorize_proxy(evidence)
            stage = "proxy_start"
            self.host.start_proxy()
            stage = "external_readiness"
            if not self.host.external_readiness():
                self.host.stop_proxy()
                raise RollbackSafetyError("external readiness failed; proxy re-isolated")
            elapsed, rto_passed = self.clock.complete_after_external_readiness()
            if not rto_passed:
                self._audit(
                    result="RTO_EXCEEDED",
                    stage="external_readiness",
                    path=path,
                    elapsed_seconds=elapsed,
                )
                raise RollbackSafetyError("G-19 RTO exceeded after safe service recovery")
            self._audit(
                result="completed",
                stage="external_readiness",
                path=path,
                elapsed_seconds=elapsed,
            )
            return RollbackResult(
                operation_id=self.operation_id,
                release_id=str(self.record["release_id"]),
                path=path,
                outcome="ROLLED_BACK",
                elapsed_seconds=elapsed,
                rto_passed=True,
            )
        except BaseException as original:
            if proxy_stop_attempted:
                try:
                    self.host.stop_proxy()
                except BaseException:
                    pass
            if path == PATH_TWO and (switched or app_stopped):
                try:
                    self.host.stop_app()
                    switch_runtime_identity(self.runtime_identity_path, candidate_identity)
                    self.host.start_app(candidate_identity)
                    if (
                        not self.host.proxy_is_stopped()
                        or not self.host.candidate_validation()
                        or digest_json(self.host.current_watermark())
                        != digest_json(expected_watermark)
                    ):
                        raise RollbackSafetyError("candidate roll-forward validation failed")
                    if path == PATH_TWO:
                        self.host.authorize_proxy(
                            {
                                "release_identity": release_identity(self.record),
                                "operation_id": self.operation_id,
                                "path": path,
                                "roll_forward": True,
                                "watermark_sha256": digest_json(expected_watermark),
                            }
                        )
                        self.host.start_proxy()
                        if not self.host.external_readiness():
                            self.host.stop_proxy()
                            raise RollbackSafetyError("candidate external readiness failed")
                    self._audit(
                        result="rolled_forward_after_failure",
                        stage="candidate_external_readiness",
                        path=path,
                        failed_stage=stage,
                    )
                except BaseException as roll_forward_error:
                    try:
                        self.host.stop_proxy()
                    except BaseException:
                        pass
                    self._audit(
                        result="failed_isolated",
                        stage="candidate_roll_forward",
                        path=path,
                        failed_stage=f"{stage}:{type(roll_forward_error).__name__}",
                    )
            else:
                self._audit(
                    result="failed_isolated",
                    stage=stage,
                    path=path,
                    failed_stage=type(original).__name__,
                )
            raise
