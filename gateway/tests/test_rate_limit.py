"""The rate limiter behind P1-07's rejection reporting, both directions.

A limiter that never reports again after the first hides an ongoing refusal;
one that always reports is the flood it exists to prevent. Both are tested,
along with the count that keeps a suppressed report honest.
"""

from __future__ import annotations

from gateway.rate_limit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


def limiter(interval_s: float = 60.0) -> tuple[RateLimiter, Clock]:
    clock = Clock()
    return RateLimiter(interval_s=interval_s, clock=clock), clock


def test_the_first_rejection_is_reported_with_nothing_suppressed() -> None:
    rejections, _ = limiter()

    assert rejections.admit("201/1") == 0


def test_rejections_within_the_interval_are_counted_not_reported() -> None:
    rejections, clock = limiter(interval_s=60.0)
    rejections.admit("201/1")

    for _ in range(500):
        clock.now_s += 0.1
        assert rejections.admit("201/1") is None


def test_the_next_report_carries_the_suppressed_count() -> None:
    """The presence half: a refusal that continues is reported again, and the
    report says how much it stands for."""
    rejections, clock = limiter(interval_s=60.0)
    rejections.admit("201/1")
    for _ in range(4212):
        rejections.admit("201/1")

    clock.now_s += 60.0

    assert rejections.admit("201/1") == 4212
    assert rejections.admit("201/1") is None


def test_keys_are_limited_independently() -> None:
    """Two refused addresses are two findings. One must not hide the other."""
    rejections, _ = limiter()
    rejections.admit("201/1")

    assert rejections.admit("202/1") == 0


# --- per-key state is bounded (S-07) ---------------------------------------


def test_expired_keys_are_evicted_before_live_ones() -> None:
    """Keys are chosen by whoever is being rejected, so the table must not
    grow with them. Expired keys go first."""
    clock = Clock()
    rejections = RateLimiter(interval_s=60.0, clock=clock, max_keys=3)
    for key in ("a", "b", "c"):
        rejections.admit(key)
    assert rejections.tracked_keys == 3

    clock.now_s += 61.0
    rejections.admit("d")

    assert rejections.tracked_keys == 1


def test_the_least_recently_reported_key_is_evicted_when_none_expired() -> None:
    clock = Clock()
    rejections = RateLimiter(interval_s=60.0, clock=clock, max_keys=3)
    rejections.admit("a")
    clock.now_s += 1.0
    for key in ("b", "c"):
        rejections.admit(key)
    for _ in range(5):
        rejections.admit("a")  # counted against "a", which stays the oldest

    rejections.admit("d")

    assert rejections.tracked_keys == 3
    # The evicted key is reported again as new, its count gone with it:
    # what a bound costs, stated so it is not mistaken for a lost report.
    # (Re-admitting it evicts the next oldest, "b", to make room.)
    assert rejections.admit("a") == 0
    # And a key that was kept is still suppressed.
    assert rejections.admit("c") is None


def test_keys_under_the_bound_are_all_kept() -> None:
    """The presence half of the bound."""
    rejections, _ = limiter()
    for n in range(500):
        rejections.admit(f"key-{n}")

    assert rejections.tracked_keys == 500
