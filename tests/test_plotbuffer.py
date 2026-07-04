"""CurveBuffer — the bounded, decimating live-chart buffer (DESIGN §21 Tier-1).

Pins the fix for the week-long-slowdown's #1 mechanism: the buffer must stay
bounded at its cap (no matter how long the app streams), preserve time order and
the full span when it decimates, and support slide-mode trimming — all Qt-free.
"""
import numpy as np

from ferrodac.core.plotbuffer import CurveBuffer


def test_append_accumulates_in_order():
    b = CurveBuffer(cap=1000)
    b.append([1.0, 2.0], [10.0, 20.0])
    b.append([3.0], [30.0])
    assert len(b) == 3
    assert list(b.x) == [1.0, 2.0, 3.0]
    assert list(b.y) == [10.0, 20.0, 30.0]


def test_stays_bounded_no_matter_how_much_is_fed():
    """The core guarantee: a week of streaming can't grow the buffer past cap."""
    b = CurveBuffer(cap=1000)
    t = 0.0
    for _ in range(500):                       # 500 batches × 100 = 50k points fed
        xs = t + np.arange(100) * 0.1
        b.append(xs, xs)
        t = float(xs[-1]) + 0.1
    assert len(b) <= 1000                      # bounded regardless of total fed
    # span preserved: still covers from near the start to the end
    assert b.x[0] < 100.0 and b.x[-1] > 4000.0
    assert list(b.x) == sorted(b.x)            # still time-ordered after decimation


def test_decimation_preserves_span_and_order():
    b = CurveBuffer(cap=8)
    for i in range(16):                         # accumulate one at a time → decimates
        b.append([float(i)], [float(i)])
    assert len(b) <= 8
    xs = list(b.x)
    assert xs == sorted(xs)
    assert xs[0] == 0.0                         # earliest kept (stride from index 0)
    assert xs[-1] >= 12.0                       # a recent point kept


def test_single_batch_larger_than_cap_keeps_recent_tail():
    b = CurveBuffer(cap=100)
    xs = np.arange(500, dtype="f8")
    b.append(xs, xs)
    assert len(b) == 100
    assert b.x[-1] == 499.0                     # newest point retained
    assert b.x[0] >= 400.0                      # only the recent tail


def test_trim_drops_old_points():
    b = CurveBuffer(cap=1000)
    b.append(list(range(100)), list(range(100)))
    changed = b.trim(50.0)                      # slide: drop t < 50
    assert changed
    assert b.x[0] == 50.0 and len(b) == 50
    assert not b.trim(-1.0)                     # nothing older → no change


def test_clear():
    b = CurveBuffer(cap=10)
    b.append([1.0], [1.0])
    b.clear()
    assert len(b) == 0 and list(b.x) == []


def test_nan_values_survive():
    b = CurveBuffer(cap=10)
    b.append([1.0, 2.0], [float("nan"), 5.0])
    assert np.isnan(b.y[0]) and b.y[1] == 5.0


def test_sigma_lane_optional_and_aligned():
    # no σ passed → all-NaN lane, has_sigma False
    b = CurveBuffer(cap=10)
    b.append([1.0, 2.0], [10.0, 20.0])
    assert not b.has_sigma and np.isnan(b.sigma).all()
    # σ passed → buffered, has_sigma True
    b.append([3.0], [30.0], [0.3])
    assert b.has_sigma
    np.testing.assert_allclose(b.sigma[-1], 0.3)


def test_sigma_stays_aligned_through_decimation():
    b = CurveBuffer(cap=4)
    b.append([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0])
    b.append([5.0, 6.0], [5.0, 6.0], [5.0, 6.0])   # overflow → decimate (stride 2)
    # σ[i] must still correspond to x[i] (here σ == x == y by construction)
    np.testing.assert_allclose(b.sigma, b.x)
    np.testing.assert_allclose(b.sigma, b.y)


def test_sigma_stays_aligned_through_trim():
    b = CurveBuffer(cap=10)
    b.append([1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0], [1.0, 2.0, 3.0, 4.0])
    b.trim(3.0)                                    # drop < 3.0
    np.testing.assert_allclose(b.x, [3.0, 4.0])
    np.testing.assert_allclose(b.sigma, [3.0, 4.0])


def test_asymmetric_sigma_lanes():
    """A (lo, hi) σ pair (a fit folded at a physical bound, §19.7) buffers as two
    lanes; a plain 1-D σ fills both (symmetric)."""
    b = CurveBuffer(cap=10)
    b.append([1.0, 2.0], [10.0, 20.0], ([0.1, 0.2], [1.0, 2.0]))
    np.testing.assert_allclose(b.sigma_lo, [0.1, 0.2])
    np.testing.assert_allclose(b.sigma_hi, [1.0, 2.0])
    assert b.has_sigma
    b.append([3.0], [30.0], [0.5])                 # symmetric → both lanes equal
    assert b.sigma_lo[-1] == b.sigma_hi[-1] == 0.5


def test_asymmetric_sigma_stays_aligned_through_decimation():
    b = CurveBuffer(cap=4)
    xs = [1.0, 2.0, 3.0, 4.0]
    b.append(xs, xs, ([v / 10 for v in xs], xs))
    b.append([5.0, 6.0], [5.0, 6.0], ([0.5, 0.6], [5.0, 6.0]))  # overflow → decimate
    np.testing.assert_allclose(b.sigma_lo, b.x / 10)  # lo[i] still matches x[i]
    np.testing.assert_allclose(b.sigma_hi, b.x)       # hi[i] still matches x[i]
