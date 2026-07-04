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
