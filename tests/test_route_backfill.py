"""Routing a source onto a chart backfills its recorded history over the current
window (#8) — via PlaybackSource.read_window, the read-only half of stream that
returns time-ordered Readings WITHOUT emitting to the shared bus (so a panel already
showing the source isn't double-fed). Qt-free."""
import os
import tempfile

import numpy as np

from ferrodac.core.trace import Trace
from ferrodac.store import PlaybackSource, ZarrStore


def _store():
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s.zarr"))
    base = 1_000_000.0
    st.add_source("dev/a")
    st.add_source("dev/b")
    ta = base + np.arange(300) * 0.1                 # 10 Hz
    tb = base + np.arange(60) * 0.5                  # 2 Hz
    st.append("dev/a", ta, np.sin(ta), epoch="e0")
    st.append("dev/b", tb, np.cos(tb), epoch="e0")
    st.finalize_rollups("dev/a")
    st.finalize_rollups("dev/b")
    return st, base


def test_read_window_returns_time_ordered_readings_without_emitting():
    st, base = _store()
    bus_hits = []
    class _Bus:                                       # must NOT be touched by read_window
        def publish(self, r):
            bus_hits.append(r)
    ps = PlaybackSource(st, _Bus(), chunk=50)
    out = ps.read_window(["dev/a", "dev/b"], base, base + 30)
    assert len(out) == 300 + 60                       # every full-res sample, both sources
    assert [r.t for r in out] == sorted(r.t for r in out)   # global time order
    assert {r.key for r in out} == {"dev/a", "dev/b"}
    assert bus_hits == []                             # read-only: nothing published


def test_read_window_single_source_and_empty_range():
    st, base = _store()
    ps = PlaybackSource(st, None)
    one = ps.read_window(["dev/a"], base, base + 30)
    assert one and {r.key for r in one} == {"dev/a"}  # just the routed source
    assert ps.read_window(["dev/a"], base - 1000, base - 900) == []   # no data → []


def test_read_window_reconstructs_traces():
    st = ZarrStore(os.path.join(tempfile.mkdtemp(), "s.zarr"))
    base = 1_000_000.0
    axis = np.linspace(1, 50, 64)
    st.add_source("rga/spec", dtype="trace")
    for i in range(10):
        st.append_trace("rga/spec", base + i, axis,
                        np.exp(-((axis - 18) ** 2)), epoch="e0")
    out = PlaybackSource(st, None).read_window(["rga/spec"], base, base + 9)
    assert len(out) == 10
    assert all(isinstance(r.value, Trace) for r in out)
    assert out[0].key == "rga/spec" and out[0].value.y.shape == (64,)
