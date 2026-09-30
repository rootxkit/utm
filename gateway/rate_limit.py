"""Rate-limited reporting for rejections. P1-07.

A rejection has to be visible and must not become a flood. Neither extreme
works:

- **Logging every rejection** hands whoever is being rejected a way to fill
  the disk. At relay rates that is hundreds of lines a second from one
  misconfigured or hostile station, and the one line that matters scrolls
  away under them.
- **Logging only the first**, as unclaimed sources are announced, is silence
  after that. For a source that is being *refused*, silence reads as "it
  stopped", which is exactly what an operator must not conclude about a
  station presenting an aircraft it was never assigned.

So: at most one report per key per interval, and each report carries how many
rejections were suppressed since the last one. The count is what keeps the
suppression honest - a report that says "and 4,212 more" is a different
finding from one that stands alone.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

DEFAULT_INTERVAL_S = 60.0
# S-07. Keys are remote addresses and source ids chosen by whoever is being
# rejected, so without a ceiling the per-key state is memory a stranger can
# grow. Expired keys go first; if none have expired, the least recently
# reported key does, and it is simply reported again as new next time.
DEFAULT_MAX_KEYS = 10_000


@dataclass
class RateLimiter:
    """Decides, per key, whether a rejection is reported now or counted."""

    interval_s: float = DEFAULT_INTERVAL_S
    clock: Callable[[], float] = time.monotonic
    max_keys: int = DEFAULT_MAX_KEYS

    _last_reported: dict[Hashable, float] = field(default_factory=dict, init=False)
    _suppressed: dict[Hashable, int] = field(default_factory=dict, init=False)

    def admit(self, key: Hashable) -> int | None:
        """Record one rejection for `key`.

        Returns the number suppressed since the previous report when this one
        should be reported, or None when it should only be counted. The first
        rejection for a key is always reported, with zero suppressed.
        """
        now = self.clock()
        last = self._last_reported.get(key)
        if last is not None and now - last < self.interval_s:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return None
        if key not in self._last_reported and len(self._last_reported) >= self.max_keys:
            self._evict(now)
        # Removed and re-added so insertion order is report order, which is
        # what `_evict` falls back on.
        self._last_reported.pop(key, None)
        self._last_reported[key] = now
        return self._suppressed.pop(key, 0)

    @property
    def tracked_keys(self) -> int:
        return len(self._last_reported)

    def _evict(self, now: float) -> None:
        expired = [
            key
            for key, last in self._last_reported.items()
            if now - last >= self.interval_s
        ]
        for key in expired:
            del self._last_reported[key]
            self._suppressed.pop(key, None)
        if len(self._last_reported) >= self.max_keys:
            oldest = next(iter(self._last_reported))
            del self._last_reported[oldest]
            self._suppressed.pop(oldest, None)
