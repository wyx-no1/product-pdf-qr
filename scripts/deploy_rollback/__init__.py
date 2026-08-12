"""Fail-closed release rollback orchestration for G-19."""

from scripts.deploy_rollback.model import (
    NEEDS_ROLLBACK_DECISION,
    RTO_LIMIT_SECONDS,
    RollbackSafetyError,
)

__all__ = [
    "NEEDS_ROLLBACK_DECISION",
    "RTO_LIMIT_SECONDS",
    "RollbackSafetyError",
]
