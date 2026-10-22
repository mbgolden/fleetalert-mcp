"""A token bucket per key, in memory.

One process, one bucket set: this server is a thin edge in front of one
demo backend, so a shared store (Redis, DynamoDB) is a later problem, noted
in docs/BACKLOG.md. The clock is injectable for tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")
        self.capacity = float(capacity)
        self.refill_per_second = refill_per_second
        self._clock = clock
        self._state: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)

    def try_acquire(self, key: str) -> float:
        """Take one token for `key`. Returns 0.0 on success, else the seconds
        until a token will be available."""
        now = self._clock()
        tokens, last = self._state.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return 0.0
        self._state[key] = (tokens, now)
        return (1.0 - tokens) / self.refill_per_second

    def remaining(self, key: str) -> float:
        now = self._clock()
        tokens, last = self._state.get(key, (self.capacity, now))
        return min(self.capacity, tokens + (now - last) * self.refill_per_second)
