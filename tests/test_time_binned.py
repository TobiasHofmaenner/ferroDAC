"""The waterfall spectrogram binning (_time_binned) — vectorised (DESIGN §21 Tier-1).

The vectorised version must stay byte-identical to the reference per-scan loop
(the same image, just built with one strided median + one gather instead of a
Python loop that re-copied an m-wide row per scan). Qt-free numeric core.
"""
import numpy as np

from ferrodac.ui.panels import _time_binned


def _reference(scans, t0, t1, rows, hold=True):
    """The pre-vectorisation implementation, kept here as the oracle."""
    if not scans or t1 <= t0:
        return None, 0
    m = len(scans[-1][1])
    rows_in = sorted(((t, y) for (t, y) in scans if len(y) == m and t0 <= t <= t1),
                     key=lambda s: s[0])
    if not rows_in:
        return None, 0
    img = np.full((rows, m), np.nan, np.float32)
    span = t1 - t0
    ts = np.array([t for t, _ in rows_in])
    diffs = np.diff(ts) if len(ts) > 1 else None
    _K = 8

    def _bin(t):
        return min(rows - 1, max(0, int((t - t0) / span * rows)))

    for j, (t, y) in enumerate(rows_in):
        i0 = _bin(t)
        if not hold:
            img[i0] = y
            continue
        t_next = rows_in[j + 1][0] if j + 1 < len(rows_in) else t1
        if diffs is not None:
            a, b = max(0, j - _K), min(len(diffs), j + _K)
            cad = float(np.median(diffs[a:b])) if b > a else span
            t_next = min(t_next, t + max(3.0 * cad, 1e-9))
        i1 = max(i0 + 1, min(rows, int((t_next - t0) / span * rows)))
        img[i0:i1] = y
    return img, m


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return np.array_equal(np.nan_to_num(a, nan=-1e30), np.nan_to_num(b, nan=-1e30))


def _make(n, m=64, seed=0, outage=False):
    rng = np.random.default_rng(seed)
    if n == 0:
        return [], 1000.0, 2000.0
    ts = 1000.0 + np.cumsum(rng.exponential(1.0, n))
    if outage and n > 4:
        ts[n // 2:] += 40.0                        # a device-offline gap
    scans = [(float(t), rng.random(m).astype(np.float32)) for t in ts]
    return scans, 1000.0, float(ts[-1]) + 1.0


def test_matches_reference_across_sizes_and_modes():
    for n in (0, 1, 2, 5, 17, 50, 300, 2000):
        scans, t0, t1 = _make(n)
        for hold in (True, False):
            a, ma = _reference(scans, t0, t1, 320, hold)
            b, mb = _time_binned(scans, t0, t1, 320, hold)
            assert ma == mb and _same(a, b), f"n={n} hold={hold}"


def test_matches_reference_with_outage_gaps():
    scans, t0, t1 = _make(400, outage=True)
    for hold in (True, False):
        a, _ = _reference(scans, t0, t1, 320, hold)
        b, _ = _time_binned(scans, t0, t1, 320, hold)
        assert _same(a, b)                          # the blank-outage heuristic matches


def test_empty_and_degenerate():
    assert _time_binned([], 0, 10, 100) == (None, 0)
    scans, t0, t1 = _make(5)
    assert _time_binned(scans, t1, t0, 100) == (None, 0)   # t1 <= t0


def test_out_of_window_scans_dropped():
    scans = [(100.0, np.ones(4, np.float32)), (5000.0, np.ones(4, np.float32))]
    img, m = _time_binned(scans, 1000.0, 2000.0, 50)       # both outside [1000,2000]
    assert img is None and m == 0
