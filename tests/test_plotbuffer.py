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


def _feed_in_chunks(b, xs, ys, chunk=200):
    """Feed like the park re-stream does — in batches well under the cap, so the
    buffer accumulates and decimates in place (NOT the single-batch>cap tail path)."""
    for i in range(0, len(xs), chunk):
        b.append(xs[i:i + chunk], ys[i:i + chunk])


def test_decimation_preserves_a_lone_spike():
    """The min/max envelope must NOT drop a narrow feature the way stride-2 did:
    a single tall spike buried in a flat baseline, once the buffer overflows and
    decimates, must still be representable (its extreme value survives)."""
    b = CurveBuffer(cap=1000)
    n = 20_000                                 # >> cap → many decimations
    xs = np.arange(n, dtype="f8")
    ys = np.zeros(n)
    ys[n // 2] = 42.0                          # one lone spike, flat elsewhere
    _feed_in_chunks(b, xs, ys)
    assert len(b) <= 1000
    assert b.y.max() == 42.0                   # the spike is still there (stride lost it)
    # ...and it stayed at roughly the right time (envelope carries the spike's own x)
    spike_x = float(b.x[int(np.argmax(b.y))])
    assert abs(spike_x - n / 2) < 16.0         # within a few buckets of its true time


def test_decimation_keeps_min_and_max_extrema():
    """Both extremes of an oscillation survive — the envelope keeps argmin AND
    argmax per bucket, so a symmetric signal is not biased toward one rail."""
    b = CurveBuffer(cap=1000)
    n = 16_000
    xs = np.arange(n, dtype="f8")
    ys = np.sin(xs * 0.5)                       # full ±1 swing, aliasing-prone
    _feed_in_chunks(b, xs, ys)
    assert b.y.max() > 0.99 and b.y.min() < -0.99   # both rails preserved


def test_decimation_preserves_a_buffered_nan_gap_marker():
    """An offline-gap NaN that lives IN the buffer (a live drop) must remain a
    break after the buffer overflows and decimates — never healed into a line."""
    b = CurveBuffer(cap=1000)
    n = 8_000
    xs = np.arange(n, dtype="f8")
    ys = np.ones(n)
    ys[n // 2] = np.nan                        # a single gap marker
    _feed_in_chunks(b, xs, ys)
    assert len(b) <= 1000
    assert np.isnan(b.y).any()                 # the break survived decimation


def test_decimation_pins_the_left_edge():
    """Regression (2026-07-09): extrema-only bucket selection dropped sample 0
    whenever it wasn't a bucket extremum, so the left edge eroded geometrically —
    a week of 50 Hz grow-mode silently lost over half its span. Bucket 0 must
    always keep sample 0: the buffer is a coarsening SPAN, not a ring."""
    b = CurveBuffer(cap=256)
    rng = np.random.default_rng(0)
    for gen in range(1_500):                   # many decimation generations
        xs = np.arange(gen * 128, (gen + 1) * 128, dtype="f8")
        b.append(xs, np.sin(xs * 0.01) + 0.1 * rng.standard_normal(128))
    assert b.x[0] == 0.0                       # left edge never moved
    assert list(b.x) == sorted(b.x)


def test_nan_gap_marker_stays_anchored():
    """Regression (2026-07-09): the whole-bucket NaN wipe re-stamped the break at
    the bucket's extrema x's, so the marker MIGRATED forward every decimation —
    a dropout fed at t=1000 ended up drawn ~200000 s later, breaking the curve
    where there is no gap. The break must keep the NaN sample's own x forever."""
    b = CurveBuffer(cap=4096)
    for gen in range(1_500):
        xs = np.arange(gen * 128, (gen + 1) * 128, dtype="f8")
        ys = np.sin(xs * 0.01)
        if gen == 7:                           # covers t=1000
            ys = ys.copy()
            ys[1000 - gen * 128] = np.nan
        b.append(xs, ys)
    nan_x = b.x[np.isnan(b.y)]
    assert len(nan_x) >= 1 and set(nan_x) == {1000.0}, nan_x


def test_gap_bucket_keeps_a_finite_neighbor():
    """Regression (2026-07-09): the old wipe turned BOTH emitted points of a
    NaN-holding bucket to NaN, eating up to 3 finite samples per bucket — a spike
    right next to a dropout vanished. The bucket must keep its strongest finite
    extremum alongside the anchored break."""
    b = CurveBuffer(cap=8)
    #                 bucket 0: pin+ordinary   bucket 1: spike AND a dropout
    b.append(np.arange(8, dtype="f8"), [1.0, 1.0, 1.0, 1.0, 1.0, 42.0, np.nan, 1.0])
    b.append([8.0, 9.0], [1.0, 1.0])           # overflow → decimate once
    assert 42.0 in b.y                         # the spike beside the gap survived
    assert np.isnan(b.y).any()                 # and so did the break
    assert b.x[np.isnan(b.y)][0] == 6.0        # anchored at the NaN sample's own x
