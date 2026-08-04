"""Process-local dual-dimension login failure backoff."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _FailureState:
    count: int
    blocked_until: float
    last_failure: float


class LoginRateLimiter:
    """Apply exponential backoff independently to source IP and username."""

    def __init__(
        self,
        *,
        failure_limit: int,
        window_seconds: int,
        base_backoff_seconds: float,
        max_backoff_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_limit = failure_limit
        self.window_seconds = window_seconds
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.clock = clock
        self._failures: dict[tuple[str, str], _FailureState] = {}

    def retry_after(self, ip_address: str, username: str) -> float:
        """Return the longest remaining IP/account backoff."""

        now = self.clock()
        states = [
            self._active_state(("ip", ip_address), now),
            self._active_state(("account", username.casefold()), now),
        ]
        return max(
            (state.blocked_until - now for state in states if state is not None),
            default=0.0,
        )

    def register_failure(self, ip_address: str, username: str) -> float:
        """Increment both dimensions and return the resulting backoff."""

        now = self.clock()
        for key in (("ip", ip_address), ("account", username.casefold())):
            state = self._active_state(key, now)
            count = 1 if state is None else state.count + 1
            delay = 0.0
            if count >= self.failure_limit:
                exponent = count - self.failure_limit
                delay = min(
                    self.base_backoff_seconds * (2**exponent),
                    self.max_backoff_seconds,
                )
            self._failures[key] = _FailureState(
                count=count,
                blocked_until=now + delay,
                last_failure=now,
            )
        return self.retry_after(ip_address, username)

    def register_success(self, ip_address: str, username: str) -> None:
        """Clear the authenticated IP and account failure histories."""

        self._failures.pop(("ip", ip_address), None)
        self._failures.pop(("account", username.casefold()), None)

    def _active_state(
        self,
        key: tuple[str, str],
        now: float,
    ) -> _FailureState | None:
        state = self._failures.get(key)
        if state is None:
            return None
        if now - state.last_failure > self.window_seconds:
            self._failures.pop(key, None)
            return None
        return state
