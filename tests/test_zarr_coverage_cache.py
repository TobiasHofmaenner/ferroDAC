"""ZarrStore.coverage() is memoised per source and invalidated on write.

Regression for a play-tick GUI freeze: coverage() re-splits the whole DIRTY tail
array every call, and the resolver's partition path calls it per tier per source on
the GUI thread (chart advance / redraw) while the prefetch worker hammers it too.
Without the memo, both block on the store lock + zarr sync for tens of ms per call
on a large live epoch. The memo must stay CORRECT — a stale coverage would under-
report the store and drop on-disk samples from reads/exports."""

import os
import tempfile

import numpy as np

from ferrodac.store import ZarrStore

BASE = 1_000_000.0


def _store():
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s.zarr"))
    st.add_source("dev/a")
    return st


def test_coverage_is_memoised_and_invalidated_on_append():
    st = _store()
    t = BASE + np.arange(1000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e")          # dirty epoch
    cov1 = st.coverage("dev/a")
    assert cov1 and cov1[0][0] == BASE
    assert st._cov_cache.get("dev/a") is cov1            # cached (same object) on 2nd read
    assert st.coverage("dev/a") is cov1

    # an append must invalidate → coverage grows to include the new tail
    t2 = t[-1] + np.arange(1, 501) * 0.1
    st.append("dev/a", t2, np.zeros(500), epoch="e")
    cov2 = st.coverage("dev/a")
    assert cov2 is not cov1
    assert cov2[-1][1] >= float(t2[-1]) - 1e-6           # the new data is reported


def test_coverage_memo_matches_uncached_including_gaps():
    """The fast path must return exactly what the slow path computes — including a
    real recording gap that _split_intervals breaks into two intervals."""
    st = _store()
    a = BASE + np.arange(400) * 0.1                       # [BASE, BASE+40)
    b = BASE + 200.0 + np.arange(400) * 0.1               # a 160 s gap, then more
    t = np.concatenate([a, b])
    st.append("dev/a", t, np.sin(t), epoch="e")
    assert st.coverage("dev/a") == st._coverage_uncached("dev/a")
    assert len(st.coverage("dev/a")) == 2                # the gap is preserved, not memo'd away


def test_coverage_memo_survives_finalize():
    st = _store()
    t = BASE + np.arange(2000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e")
    before = list(st.coverage("dev/a"))
    st.finalize_rollups("dev/a", "e")                    # dirty→clean: must invalidate + agree
    assert st.coverage("dev/a") == before
