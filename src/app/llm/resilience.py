from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.config import settings


class CircuitOpenError(RuntimeError):
    """Circuit is open and the next probe has not been released yet."""

    def __init__(self, remaining_seconds: float):
        self.remaining_seconds = max(0.0, remaining_seconds)
        super().__init__(
            f"LLM 服务熔断中，请在约 {self.remaining_seconds:.1f} 秒后重试。"
        )


@dataclass
class CircuitSnapshot:
    state: str
    failures: int
    remaining_seconds: float


class CircuitBreaker:
    """Consecutive-failure circuit breaker with one half-open probe."""

    def __init__(self, threshold: int, cooldown_seconds: float):
        self.threshold = max(1, int(threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    def before_request(self) -> bool:
        """Raise when blocked; return True when this request is a probe."""
        with self._lock:
            if self._opened_at is None:
                return False

            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.cooldown_seconds:
                raise CircuitOpenError(self.cooldown_seconds - elapsed)
            if self._probe_in_flight:
                raise CircuitOpenError(0.0)

            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._opened_at is not None or self._probe_in_flight:
                self._opened_at = time.monotonic()
                self._probe_in_flight = False
                return
            if self._failures >= self.threshold:
                self._opened_at = time.monotonic()

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            if self._opened_at is None:
                return CircuitSnapshot("closed", self._failures, 0.0)

            remaining = max(
                0.0,
                self.cooldown_seconds - (time.monotonic() - self._opened_at),
            )
            state = "half_open" if remaining == 0 else "open"
            return CircuitSnapshot(state, self._failures, remaining)


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def current_llm_key() -> str:
    return f"{settings.LLM_BASE_URL.rstrip('/')}|{settings.LLM_MODEL}"


def get_llm_breaker() -> CircuitBreaker:
    key = current_llm_key()
    with _breakers_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(
                threshold=settings.CIRCUIT_BREAKER_THRESHOLD,
                cooldown_seconds=settings.CIRCUIT_BREAKER_COOLDOWN,
            )
            _breakers[key] = breaker
        return breaker


def get_llm_circuit_snapshot() -> dict:
    snapshot = get_llm_breaker().snapshot()
    return {
        "state": snapshot.state,
        "failures": snapshot.failures,
        "remaining_seconds": round(snapshot.remaining_seconds, 1),
    }
