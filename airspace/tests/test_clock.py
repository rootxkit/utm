"""Per-source clock offsets: skew absorbed, delay reported. B1."""

from __future__ import annotations

import pytest

from airspace.clock import SourceClocks


def test_the_first_message_sets_the_offset_and_has_no_excess() -> None:
    clocks = SourceClocks(relax_s_per_s=0.1)
    delay = clocks.observe("gs-1", wall_s=100.0, ts_s=40.0)
    assert (delay.offset_s, delay.excess_s) == (60.0, 0.0)


def test_a_constant_skew_of_either_sign_is_absorbed() -> None:
    clocks = SourceClocks(relax_s_per_s=0.1)
    for skew_s, source in ((60.0, "slow"), (-60.0, "fast")):
        excess = [
            clocks.observe(source, wall_s=t, ts_s=t - skew_s - 0.8).excess_s
            for t in (0.0, 1.0, 2.0, 3.0)
        ]
        assert excess == pytest.approx([0.0, 0.0, 0.0, 0.0], abs=1e-9)
        assert clocks.offset_s(source) == pytest.approx(skew_s + 0.8)


def test_only_delay_above_the_sources_usual_delay_is_excess() -> None:
    clocks = SourceClocks(relax_s_per_s=0.0)
    clocks.observe("gs-1", wall_s=0.0, ts_s=-1.0)
    late = clocks.observe("gs-1", wall_s=10.0, ts_s=-20.0)
    assert late.excess_s == pytest.approx(29.0)
    # A faster message lowers the offset, and is itself never excess.
    fast = clocks.observe("gs-1", wall_s=11.0, ts_s=10.5)
    assert (fast.offset_s, fast.excess_s) == (0.5, 0.0)


def test_the_offset_relaxes_upwards_at_the_rate_and_no_faster() -> None:
    clocks = SourceClocks(relax_s_per_s=0.1)
    clocks.observe("gs-1", wall_s=0.0, ts_s=0.0)
    # The clock is stepped back 30 s: the messages look 30 s late.
    at_10 = clocks.observe("gs-1", wall_s=10.0, ts_s=-20.0)
    assert (at_10.offset_s, at_10.excess_s) == pytest.approx((1.0, 29.0))
    at_310 = clocks.observe("gs-1", wall_s=310.0, ts_s=280.0)
    assert (at_310.offset_s, at_310.excess_s) == pytest.approx((30.0, 0.0))


def test_sources_are_independent() -> None:
    clocks = SourceClocks(relax_s_per_s=0.1)
    clocks.observe("a", wall_s=0.0, ts_s=-60.0)
    assert clocks.observe("b", wall_s=0.0, ts_s=0.0).excess_s == 0.0
    assert clocks.offset_s("a") == 60.0 and clocks.offset_s("b") == 0.0
    assert clocks.offset_s("c") is None


def test_a_negative_relax_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="relax"):
        SourceClocks(relax_s_per_s=-0.1)
