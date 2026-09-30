"""Sign-in attempt limits, per client address and per username. S-15.

The account lockout (`api/auth.py`) protects one account against guessing,
but it is not a limit on work: every attempt still costs a 32 MiB scrypt, and
an unknown username has no account to lock. This counts attempts in a
sliding window before any of that runs, keyed both by the client address and
by the username as typed (normalised the way the store normalises it), so
that one address cannot try many names and many addresses cannot try one
name. An unknown username is counted exactly like a known one: answering it
differently would reveal which names exist.

In memory, per process. A restart forgets the counts, and several API
processes each keep their own; the lockout in the database is the durable
protection, this is the cheap one in front of it.

The address is `request.client.host`, what the ASGI server reports, and
never a header read here. Behind the TLS front (P0-09) uvicorn rewrites it
from `X-Forwarded-For` only when the peer is in `FORWARDED_ALLOW_IPS`
(`api.config.ProxySettings`): without that every client would share the
front's budget, and trusting the header from anyone would let a client
choose a fresh address per attempt.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class _Window:
    """Attempt times for one key, oldest first."""

    times_s: deque[float] = field(default_factory=deque)

    def prune(self, now_s: float, window_s: float) -> None:
        while self.times_s and self.times_s[0] <= now_s - window_s:
            self.times_s.popleft()


@dataclass
class LoginRateLimiter:
    """Refuses a sign-in attempt when its address or its username has made
    too many attempts within `window_s`. Every attempt counts, successful or
    not: a person signing in does not need tens of tries a minute."""

    max_per_address: int = 20
    max_per_username: int = 10
    window_s: float = 300.0
    # A bound on memory under a spray of distinct names or addresses. When
    # it is reached, expired keys are dropped first, then the oldest.
    max_keys: int = 10_000
    clock_s: Callable[[], float] = time.monotonic
    _windows: dict[tuple[str, str], _Window] = field(default_factory=dict, init=False)

    def attempt(self, *, address: str | None, username: str) -> float | None:
        """Record an attempt. None if it may proceed, else the seconds until
        it may be retried. A refused attempt is not recorded, so a client
        that keeps trying is not locked out for longer than the window."""
        now_s = self.clock_s()
        keys = [(("username", username.strip().lower()), self.max_per_username)]
        if address is not None:
            keys.append((("address", address), self.max_per_address))

        retry_after_s: float | None = None
        for key, limit in keys:
            window = self._windows.get(key)
            if window is None:
                continue
            window.prune(now_s, self.window_s)
            if len(window.times_s) >= limit:
                # The oldest attempt in the window leaves it first.
                wait_s = window.times_s[0] + self.window_s - now_s
                retry_after_s = max(retry_after_s or 0.0, wait_s)
        if retry_after_s is not None:
            return max(retry_after_s, 0.0)

        for key, _ in keys:
            window = self._windows.pop(key, None) or _Window()
            # Re-inserted, so insertion order is order of last use and the
            # first key is the one idle longest.
            self._windows[key] = window
            window.times_s.append(now_s)
        self._bound(now_s)
        return None

    def _bound(self, now_s: float) -> None:
        if len(self._windows) <= self.max_keys:
            return
        for key in list(self._windows):
            window = self._windows[key]
            window.prune(now_s, self.window_s)
            if not window.times_s:
                del self._windows[key]
        while len(self._windows) > self.max_keys:
            del self._windows[next(iter(self._windows))]


def retry_after_header(wait_s: float) -> str:
    """Whole seconds, rounded up, and never 0: a 0 invites a tight loop."""
    return str(max(1, math.ceil(wait_s)))
