from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Three-state circuit breaker.

    - CLOSED: calls pass through; consecutive failures are counted.
    - OPEN: calls fail fast until ``reset_timeout_seconds`` elapses.
    - HALF_OPEN: a limited number of probes are allowed; ``success_threshold``
      consecutive probe successes close the circuit, a single probe failure
      re-opens it immediately.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted."""
        if self.state is CircuitState.CLOSED:
            return True
        if self.state is CircuitState.HALF_OPEN:
            return True

        # OPEN: fail fast until the reset timeout has elapsed, then probe once.
        if self.opened_at is None:
            # Defensive: OPEN without a timestamp cannot age out, so probe now.
            self._to_half_open()
            return True
        if time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
            self._to_half_open()
            return True
        return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call a function through the circuit breaker."""
        if not self.allow_request():
            raise CircuitOpenError(f"circuit '{self.name}' is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a successful call."""
        self.failure_count = 0
        self.success_count += 1
        if self.state is CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
            self._transition(CircuitState.CLOSED, "probe_success")
            self.success_count = 0
            self.opened_at = None

    def record_failure(self) -> None:
        """Record a failed call."""
        self.failure_count += 1
        self.success_count = 0
        # HALF_OPEN and threshold breaches are distinct triggers with distinct
        # reasons, so they must stay in separate branches.
        if self.state is CircuitState.HALF_OPEN:
            self._open("probe_failure")
        elif self.failure_count >= self.failure_threshold:
            self._open("failure_threshold_reached")

    def _open(self, reason: str) -> None:
        """Trip the circuit, stamping ``opened_at`` only on a real transition.

        Re-stamping while already OPEN would push the reset deadline forward on
        every failing call and the circuit would never reach HALF_OPEN.
        """
        if self.state is CircuitState.OPEN:
            return
        self._transition(CircuitState.OPEN, reason)
        self.opened_at = time.monotonic()

    def _to_half_open(self) -> None:
        self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
        self.success_count = 0

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
