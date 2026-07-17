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
    assert st._cov_cache["dev/a"]["out"] is cov1         # cached (same object) on 2nd read
    assert st.coverage("dev/a") is cov1

    # an append must invalidate ITS epoch → coverage grows to include the new tail
    t2 = t[-1] + np.arange(1, 501) * 0.1
    st.append("dev/a", t2, np.zeros(500), epoch="e")
    assert "e" in st._cov_cache["dev/a"]["stale"]        # only that epoch marked stale
    cov2 = st.coverage("dev/a")
    assert cov2 is not cov1
    assert cov2[-1][1] >= float(t2[-1]) - 1e-6           # the new data is reported

    # an append to epoch B must NOT force a re-split of untouched epoch A
    st.append("dev/a", t2 + 10_000.0, np.zeros(500), epoch="e2")
    per_before = st._cov_cache["dev/a"]["per"]["e"]
    st.coverage("dev/a")
    assert st._cov_cache["dev/a"]["per"]["e"] is per_before   # epoch 'e' reused, not recomputed


def test_coverage_memo_matches_uncached_including_gaps():
    """The fast path must return exactly what the slow path computes — including a
    real recording gap that _split_intervals breaks into two intervals."""
    st = _store()
    a = BASE + np.arange(400) * 0.1                       # [BASE, BASE+40)
    b = BASE + 200.0 + np.arange(400) * 0.1               # a 160 s gap, then more
    t = np.concatenate([a, b])
    st.append("dev/a", t, np.sin(t), epoch="e")
    cold = ZarrStore(st.root.store.root)                  # a fresh store object = no memo
    assert st.coverage("dev/a") == cold.coverage("dev/a")
    assert len(st.coverage("dev/a")) == 2                # the gap is preserved, not memo'd away


def test_coverage_memo_survives_finalize():
    st = _store()
    t = BASE + np.arange(2000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e")
    before = list(st.coverage("dev/a"))
    st.finalize_rollups("dev/a", "e")                    # dirty→clean: must invalidate + agree
    assert st.coverage("dev/a") == before


def test_epoch_lengths_cached_and_correct():
    """epoch_lengths() used to sweep every source × epoch group under the store lock
    (the sync thread calls it every 5 s — the metronomic store-writer backlog). It is
    now maintained in-memory on append; it must still match a cold full sweep."""
    st = _store()
    t = BASE + np.arange(100) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e1")
    st.append("dev/a", t + 100, np.sin(t), epoch="e2")
    st.add_source("dev/b", dtype="trace")
    st.append_trace("dev/b", BASE, np.arange(8.0), np.zeros(8), epoch="tr0")
    st.append_trace("dev/b", BASE + 1, np.arange(8.0), np.ones(8), epoch="tr0")

    live = st.epoch_lengths()
    assert live[("dev/a", "e1")] == 100
    assert live[("dev/a", "e2")] == 100
    assert live[("dev/b", "tr0")] == 2

    st.append("dev/a", t + 200, np.sin(t), epoch="e2")   # cache must track growth
    assert st.epoch_lengths()[("dev/a", "e2")] == 200

    cold = ZarrStore(st.root.store.root)                 # fresh object = full sweep
    assert cold.epoch_lengths() == st.epoch_lengths()


def test_append_after_finalize_preserves_rollup_attrs():
    """Attribute writes go through per-epoch cached handles; update_attributes
    rewrites the WHOLE doc from the handle's in-memory attrs, so an append through a
    stale handle silently dropped the rollup's levels/rolled_n/intervals (found by
    the store selftest: the dirty-tail top-up lost its watermark)."""
    st = _store()
    t = BASE + np.arange(60_000) * 0.1
    st.append("dev/a", t, np.sin(t), epoch="e")
    st.finalize_rollups("dev/a", "e")
    t2 = t[-1] + 0.1 + np.arange(100) * 0.1
    st.append("dev/a", t2, np.zeros(100), epoch="e")     # append AFTER the rollup
    g = st.root["dev%2Fa"]["e"]
    assert g.attrs.get("levels", 0) >= 1                 # rollup attrs survived
    assert g.attrs.get("rolled_n") == 60_000
    assert g.attrs.get("dirty") is True                  # the tail is honestly dirty
    assert g.attrs.get("n") == 60_100


def test_windowed_read_uses_chunk_index_and_matches_full_read():
    """read_raw/read_raw_trace/query bisect the per-chunk time index ('cidx') to the
    chunks a window touches instead of decompressing the whole t array (O(epoch) per
    read; the play tick reads a small window every 50 ms). The windowed result must
    be IDENTICAL to a full-array searchsorted, across chunk boundaries."""
    st = _store()
    # 3.5 chunks of scalars (chunk = 16384) at 1 Hz
    n = 16384 * 3 + 8000
    t = BASE + np.arange(n, dtype="f8")
    for i in range(0, n, 5000):                      # batch appends crossing boundaries
        st.append("dev/a", t[i:i + 5000], np.sin(t[i:i + 5000]), epoch="e")
    g = st.root["dev%2Fa"]["e"]
    cidx = g.attrs.get("cidx")
    assert cidx is not None and len(cidx) == 4       # one entry per chunk
    assert cidx[0] == t[0] and cidx[1] == t[16384]

    # windows: inside one chunk, spanning a boundary, spanning everything
    for (a, b) in [(BASE + 100, BASE + 200),
                   (BASE + 16380, BASE + 16390),
                   (BASE - 10, BASE + n + 10)]:
        rt, rv = st.read_raw("dev/a", a, b)
        j0 = int(np.searchsorted(t, a, side="left"))
        j1 = int(np.searchsorted(t, b, side="right"))
        assert np.array_equal(rt, t[j0:j1]), (a, b)
        assert np.array_equal(rv, np.sin(t[j0:j1])), (a, b)

    # trace path: 2.5 chunks of scans (t chunk = 4096)
    st.add_source("dev/tr", dtype="trace")
    x = np.arange(8.0)
    for i in range(4096 * 2 + 100):
        st.append_trace("dev/tr", BASE + i, x, np.full(8, float(i)), epoch="tr")
    blocks = st.read_raw_trace("dev/tr", BASE + 4090, BASE + 4100)
    assert len(blocks) == 1
    bt, by, bx = blocks[0]
    assert list(bt) == [BASE + i for i in range(4090, 4101)]
    assert by[0][0] == 4090.0 and by[-1][0] == 4100.0
