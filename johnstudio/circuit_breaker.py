"""Circuit breaker for the iteration arc.

Halts a no-progress loop when:
  * the SAME error/failure signature occurs N consecutive times (default 3), or
  * the worktree state oscillates between the same hashes (edit/revert churn).

Signatures are normalized so volatile bits (paths, line/col numbers, hex
addresses, temp dirs, bare numbers) don't hide a genuinely-repeated failure.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional, Sequence


class BreakerState(str, Enum):
    CLOSED = "closed"  # healthy — loop may continue
    OPEN = "open"      # tripped — loop must halt


_HEX = re.compile(r"0x[0-9a-fA-F]+")
_TMP = re.compile(r"/(?:tmp|var|private)/[^\s'\"]+")
_PATHLINE = re.compile(r":\d+(?::\d+)?")
_NUM = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")


def normalize_error(message: str) -> str:
    """Reduce an error/failure message to a stable fingerprint."""
    if not message:
        return ""
    text = message.strip().lower()
    text = _HEX.sub("0xaddr", text)
    text = _TMP.sub("/tmp/path", text)
    text = _PATHLINE.sub(":line", text)
    text = _NUM.sub("n", text)
    text = _WS.sub(" ", text)
    return text.strip()


def repeated_failure(reasons: Sequence[str], max_repeats: int = 3) -> bool:
    """True if the last ``max_repeats`` non-empty reasons normalize identically.

    Pure helper used by the iteration arc to decide whether to halt without
    instantiating a stateful breaker.
    """
    if max_repeats <= 0:
        return False
    sigs = [normalize_error(r) for r in reasons if r and normalize_error(r)]
    if len(sigs) < max_repeats:
        return False
    tail = sigs[-max_repeats:]
    return all(s == tail[0] for s in tail)


@dataclass
class CircuitBreaker:
    """Stateful breaker: feed it per-iteration signals; it trips on no-progress."""

    max_repeats: int = 3
    churn_window: int = 6
    state: BreakerState = BreakerState.CLOSED
    reason: Optional[str] = None

    _last_sig: Optional[str] = field(default=None, repr=False)
    _repeat_count: int = field(default=0, repr=False)
    _state_history: Deque[str] = field(default_factory=deque, repr=False)

    @property
    def tripped(self) -> bool:
        return self.state is BreakerState.OPEN

    def _trip(self, reason: str) -> None:
        self.state = BreakerState.OPEN
        self.reason = reason

    def record_error(self, message: str) -> bool:
        """Record an iteration's error. Returns True iff the breaker tripped.

        A falsy message counts as progress and resets the repeat counter.
        """
        if self.tripped:
            return True
        sig = normalize_error(message)
        if not sig:
            self._last_sig = None
            self._repeat_count = 0
            return False
        if sig == self._last_sig:
            self._repeat_count += 1
        else:
            self._last_sig = sig
            self._repeat_count = 1
        if self._repeat_count >= self.max_repeats:
            self._trip(f"same error {self._repeat_count}x consecutively: {sig[:160]!r}")
            return True
        return False

    def record_state(self, state_hash: str) -> bool:
        """Record the worktree state hash. Trips on edit/revert churn.

        If the same hash appears 3+ times inside the window (A->B->A->B->A),
        the worker is thrashing. Returns True iff the breaker tripped.
        """
        if self.tripped:
            return True
        if not state_hash:
            return False
        self._state_history.append(state_hash)
        while len(self._state_history) > self.churn_window:
            self._state_history.popleft()
        if self._state_history.count(state_hash) >= 3:
            self._trip(f"edit/revert churn: state {state_hash[:16]!r} repeated")
            return True
        return False

    def report(self) -> dict:
        return {
            "state": self.state.value,
            "tripped": self.tripped,
            "reason": self.reason,
            "repeat_count": self._repeat_count,
            "last_signature": self._last_sig,
        }
