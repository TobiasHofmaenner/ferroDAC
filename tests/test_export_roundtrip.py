"""END-TO-END data-integrity round-trip: synthetic readings flow through the WHOLE
durable pipeline — engine bus → StoreWriter → ZarrStore (epochs + rollups) →
Resolver (RAM + store, nearest-wins stitch) → export_window → CSV — and the CSV
is parsed back and compared against the emitted ground truth, sample by sample.

This is the trust contract of the tool: if data.csv cannot be reconciled exactly
with what the devices emitted, nothing downstream matters. Every asserted
property here is exact (within the CSV's own stated precision: %.6f timestamps,
%.10g values, %.6E trace intensities) — no statistical fuzz.

Qt-free (numpy + zarr).
"""

import csv
import json
import os
import tempfile

import numpy as np

from ferrodac.core.history import HistoryBuffer
from ferrodac.core.trace import Trace
from ferrodac.store import RamTier, Resolver, StoreWriter, ZarrStore, export_window

BASE = 1_700_000_000.0


class _Engine:
    """Synchronous fake engine bus (the writer_selftest pattern) — delivers each
    published batch straight to subscribers, so the flow is deterministic."""

    def __init__(self):
        self._subs = []

    def subscribe(self, cb, **_kw):
        self._subs.append(cb)
        return lambda: self._subs.remove(cb)

    def publish(self, batch):
        for cb in list(self._subs):
            cb(batch)


class _R:
    __slots__ = ("key", "t", "value", "status", "partial", "sigma")

    def __init__(self, key, t, v, status=0, partial=False):
        self.key, self.t, self.value = key, float(t), v
        self.status, self.partial, self.sigma = status, partial, None


def _read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def _column_series(rows, header):
    """Reconstruct one source's (t, v) arrays from the wide CSV — non-blank cells
    of its column, paired with the epoch-seconds column."""
    col = rows[0].index(header)
    t, v = [], []
    for r in rows[1:]:
        if r[col] != "":
            t.append(float(r[1]))
            v.append(float(r[col]))
    return np.asarray(t), np.asarray(v)


def _expect(t, v):
    """Ground truth as the CSV must render it: %.6f time, %.10g value re-parsed."""
    return (np.round(np.asarray(t, dtype="f8"), 6),
            np.asarray([float(f"{float(x):.10g}") for x in v], dtype="f8"))


def _assert_series_equal(got, want, what):
    gt, gv = got
    wt, wv = want
    assert len(gt) == len(wt), f"{what}: {len(gt)} rows in CSV, {len(wt)} emitted"
    assert np.array_equal(gt, wt), f"{what}: timestamps diverge"
    assert np.array_equal(gv, wv), f"{what}: values diverge"


def _pipeline():
    """The real durable pipeline: engine → StoreWriter → ZarrStore, plus a RAM
    ring feeding the resolver exactly like the app wires it."""
    root = os.path.join(tempfile.mkdtemp(), "s.zarr")
    store = ZarrStore(root)
    eng = _Engine()
    writer = StoreWriter(store, chunk=256, rollup_every=5_000)
    writer.attach(eng)
    history = HistoryBuffer()
    eng.subscribe(history.feed)
    resolver = Resolver([RamTier(history), store])
    return eng, writer, store, resolver


def test_full_pipeline_roundtrip_scalars_exact():
    """The core contract: every finite sample emitted on the bus appears in
    data.csv exactly once, with its exact timestamp and value — across two
    devices at different rates, a bool channel, injected failed reads (NaN/inf,
    must be absent), and a recording gap (honest blanks, no bridging rows)."""
    eng, writer, store, resolver = _pipeline()
    rng = np.random.default_rng(7)

    ta = BASE + np.arange(4000) * 0.1                       # gauge A, 10 Hz
    va = 1e-6 * (1 + 0.3 * np.sin(ta * 0.05)) + 1e-9 * rng.standard_normal(4000)
    tb0 = BASE + np.arange(500) * 0.3                       # gauge B, 3.3 Hz, then a
    tb1 = BASE + 250 + np.arange(400) * 0.3                 # 100 s outage (real gap)
    tb = np.concatenate([tb0, tb1])
    vb = 300.0 + 5.0 * np.cos(tb * 0.02)
    tv = BASE + np.arange(40) * 10.0                        # valve, 0.1 Hz bool
    vv = (np.arange(40) % 3 == 0)

    batches = []
    for i in range(0, 4000, 100):
        batches.append([_R("gaugeA/p", t, float(v))
                        for t, v in zip(ta[i:i + 100], va[i:i + 100])])
    batches.append([_R("gaugeB/T", t, float(v)) for t, v in zip(tb, vb)])
    batches.append([_R("valve/open", t, bool(v)) for t, v in zip(tv, vv)])
    # failed reads: what a device emits on error — must never reach the CSV
    batches.append([_R("gaugeA/p", BASE + 401.0, float("nan"), status=1),
                    _R("gaugeB/T", BASE + 402.0, float("inf"), status=1)])
    for b in batches:
        eng.publish(b)
    writer.stop()                                           # final flush + rollups

    dest = os.path.join(tempfile.mkdtemp(), "out")
    sources = {"gaugeA/p": {"name": "pressure · Gauge A", "unit": "mbar", "dtype": "float"},
               "gaugeB/T": {"name": "temp · Gauge B", "unit": "K", "dtype": "float"},
               "valve/open": {"name": "valve · V1", "unit": "", "dtype": "bool"}}
    man = export_window(dest, sources, resolver, BASE - 1, ta[-1] + 1)
    rows = _read_csv(os.path.join(dest, "data.csv"))

    _assert_series_equal(_column_series(rows, "pressure · Gauge A [mbar]"),
                         _expect(ta, va), "gauge A")
    _assert_series_equal(_column_series(rows, "temp · Gauge B [K]"),
                         _expect(tb, vb), "gauge B (with gap)")
    _assert_series_equal(_column_series(rows, "valve · V1"),
                         _expect(tv, vv.astype(float)), "bool valve")
    # failed reads are ABSENT (not zero, not NaN-text, not a row)
    assert not any(abs(float(r[1]) - (BASE + 401.0)) < 1e-6 for r in rows[1:])
    assert not any(abs(float(r[1]) - (BASE + 402.0)) < 1e-6 for r in rows[1:])
    # the row count is exactly the union of emitted timestamps (no invented rows)
    union = set(np.round(ta, 6)) | set(np.round(tb, 6)) | set(np.round(tv, 6))
    assert len(rows) - 1 == len(union)
    by_key = {s["key"]: s for s in man["sources"]}
    assert by_key["gaugeA/p"]["samples"] == 4000
    assert by_key["gaugeB/T"]["samples"] == 900


def test_roundtrip_through_ram_and_store_stitch():
    """The resolver serves the freshest span from RAM and the rest from the Zarr
    store. The stitched export must equal the emitted stream exactly — no
    duplicate and no dropped sample at the tier seam."""
    eng, writer, store, resolver = _pipeline()
    t = BASE + np.arange(2000) * 0.1
    v = np.sin(t * 0.01)
    for i in range(0, 2000, 250):
        eng.publish([_R("dev/a", tt, float(vv))
                     for tt, vv in zip(t[i:i + 250], v[i:i + 250])])
    writer.flush_all()                                      # store has everything;
    #                                                         the RAM ring ALSO holds
    #                                                         the recent tail → overlap
    ram_cov = RamTier(resolver.tiers[0].history).coverage("dev/a")
    assert ram_cov, "test rig: the RAM ring must actually hold data"
    dest = os.path.join(tempfile.mkdtemp(), "out")
    export_window(dest, {"dev/a": {"name": "A", "unit": "V", "dtype": "float"}},
                  resolver, BASE - 1, t[-1] + 1)
    rows = _read_csv(os.path.join(dest, "data.csv"))
    _assert_series_equal(_column_series(rows, "A [V]"), _expect(t, v),
                         "RAM+store stitched export")


def test_duplicate_timestamps_are_not_collapsed():
    """Two samples with the SAME timestamp (fast polling / clock step) must both
    appear — the old {t: v} dict silently kept only the last one."""
    eng, writer, store, resolver = _pipeline()
    eng.publish([_R("dev/a", BASE, 1.0), _R("dev/a", BASE, 2.0),
                 _R("dev/a", BASE + 1, 3.0)])
    writer.stop()
    dest = os.path.join(tempfile.mkdtemp(), "out")
    export_window(dest, {"dev/a": {"name": "A", "unit": "", "dtype": "float"}},
                  resolver, BASE - 1, BASE + 2)
    rows = _read_csv(os.path.join(dest, "data.csv"))
    col = [r[2] for r in rows[1:] if r[2] != ""]
    assert col == ["1", "2", "3"], col                      # both duplicates kept, in order


def test_identical_source_names_get_unambiguous_columns():
    """Two same-model gauges share name+unit: the CSV must never contain two
    identical headers (matching the wrong column against a curve reads as
    corrupted data). The manifest maps each key to its exact column."""
    eng, writer, store, resolver = _pipeline()
    eng.publish([_R("g1/p", BASE, 1.0), _R("g2/p", BASE, 2.0)])
    writer.stop()
    dest = os.path.join(tempfile.mkdtemp(), "out")
    man = export_window(dest, {"g1/p": {"name": "pressure", "unit": "mbar", "dtype": "float"},
                               "g2/p": {"name": "pressure", "unit": "mbar", "dtype": "float"}},
                        resolver, BASE - 1, BASE + 1)
    header = _read_csv(os.path.join(dest, "data.csv"))[0]
    assert len(set(header)) == len(header), f"ambiguous columns: {header}"
    cols = {s["key"]: s["column"] for s in man["sources"]}
    assert cols["g1/p"] != cols["g2/p"]
    rows = _read_csv(os.path.join(dest, "data.csv"))
    assert _column_series(rows, cols["g1/p"])[1].tolist() == [1.0]
    assert _column_series(rows, cols["g2/p"])[1].tolist() == [2.0]


def test_trace_roundtrip_exact_and_partial_skipped():
    """Trace scans round-trip through writer → store → export as a matrix within
    the CSV's stated precision; partial (preview) frames never persist."""
    eng, writer, store, resolver = _pipeline()
    ax = np.linspace(1, 50, 64)
    scans = [np.exp(-((ax - 10 - i) ** 2)) * (1 + i) for i in range(5)]
    tt = BASE + np.arange(5) * 3.0
    for i in range(5):
        eng.publish([_R("rga/spec", tt[i], Trace(x=ax, y=scans[i]))])
    eng.publish([_R("rga/spec", BASE + 100, Trace(x=ax, y=ax * 0), partial=True)])
    writer.stop()
    dest = os.path.join(tempfile.mkdtemp(), "out")
    man = export_window(dest, {"rga/spec": {"name": "spec", "unit": "mbar",
                                            "dtype": "trace"}},
                        resolver, BASE - 1, BASE + 200)
    tf = next(s["file"] for s in man["sources"] if s["dtype"] == "trace")
    rows = _read_csv(os.path.join(dest, tf))
    assert len(rows) - 1 == 5                               # 5 real scans, partial skipped
    assert [float(c) for c in rows[0][1:]] == [float(f"{m:g}") for m in ax]
    for i in range(5):
        got = np.asarray([float(c) for c in rows[1 + i][1:]])
        want = np.asarray([float(f"{y:.6E}") for y in scans[i]])
        assert float(rows[1 + i][0]) == round(tt[i], 6)
        assert np.array_equal(got, want), f"scan {i} diverges"


def test_csv_matches_what_a_chart_would_draw():
    """The graph↔CSV trust check: the parked chart draws resolver.query envelope
    values — every one of them must literally exist in the exported raw column
    (the envelope's min/max ARE real samples), and the exported extremes must
    equal the drawn extremes. No path may show data the other can't account for."""
    eng, writer, store, resolver = _pipeline()
    rng = np.random.default_rng(3)
    t = BASE + np.arange(20_000) * 0.05
    v = np.sin(t * 0.01) + 0.05 * rng.standard_normal(20_000)
    v[7_000] = 9.0                                          # a spike the chart must show
    for i in range(0, 20_000, 500):
        eng.publish([_R("dev/a", tt, float(vv))
                     for tt, vv in zip(t[i:i + 500], v[i:i + 500])])
    writer.stop()
    dest = os.path.join(tempfile.mkdtemp(), "out")
    export_window(dest, {"dev/a": {"name": "A", "unit": "V", "dtype": "float"}},
                  resolver, BASE - 1, t[-1] + 1)
    rows = _read_csv(os.path.join(dest, "data.csv"))
    ct, cv = _column_series(rows, "A [V]")

    qx, qy = resolver.query("dev/a", BASE - 1, t[-1] + 1, 2000)   # what a chart draws
    drawn = qy[np.isfinite(qy)]
    exported = set(cv.tolist())
    missing = [y for y in drawn.tolist() if float(f"{y:.10g}") not in exported]
    assert not missing, f"{len(missing)} drawn values unaccounted for in the CSV"
    assert cv.max() == float(f"{9.0:.10g}") == drawn.max()  # the spike, both places