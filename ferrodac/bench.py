"""Benchmark harness — time the REAL data-plane paths at controlled load sizes.

Seeds synthetic Zarr stores of a chosen size and times exactly the code the app
runs (`resolver.query`/`read_raw`/`read_raw_trace`, `PlaybackSource.read_window`,
`export_window`, `ZarrStore.append`), so "how long to load a data range" is
measured, not guessed. Reused by the in-app Benchmark dialog (faithful, in-process)
and a pytest-benchmark suite (regression tracking, no GUI noise). Qt-free.

A benchmark != a profile: this says HOW LONG a path takes at size N; when one is
slow, py-spy/scalene say WHERE the time goes inside it.
"""

from __future__ import annotations

import os
import statistics
import tempfile
import time

import numpy as np

from .core.bus import Bus
from .core.history import HistoryBuffer
from .store import (PlaybackSource, RamTier, Resolver, ZarrStore,  # noqa: F401
                    export_window)

SCALAR_SIZES = (10_000, 100_000, 1_000_000)     # samples PER source
TRACE_SIZES = (200, 1_000, 5_000)               # scans
SOURCES = ("dev/a", "dev/b", "dev/c", "dev/d")
_HZ = 10.0
_T0 = 1_700_000_000.0


# -- synthetic loads ---------------------------------------------------------
def seed_scalar(path, n, sources=SOURCES, seed=0):
    """A store with `n` samples/source of a realistic random-walk signal, rollups
    built (so the decimated-query path is faithful). Returns (store, t0, t1)."""
    st = ZarrStore(path)
    rng = np.random.default_rng(seed)
    t = _T0 + np.arange(n) / _HZ
    for s in sources:
        st.add_source(s, name=s, unit="mbar")
        v = 100.0 + np.cumsum(rng.standard_normal(n)) * 0.01
        st.append(s, t, v.astype("f8"), epoch="b")
        try:
            st.finalize_rollups(s, "b")
        except Exception:                        # noqa: BLE001 — query falls back to raw
            pass
    return st, float(t[0]), float(t[-1])


def seed_trace(path, n_scans, bins=64, source="rga/spec", seed=0):
    """A store with `n_scans` swept-axis trace scans (RGA-like). Returns
    (store, t0, t1)."""
    st = ZarrStore(path)
    rng = np.random.default_rng(seed)
    x = np.linspace(1.0, 50.0, bins)
    st.add_source(source, name=source, dtype="trace")
    for i in range(n_scans):
        y = np.exp(-((x - 18.0) ** 2) / 4.0) * (1.0 + 0.05 * rng.standard_normal(bins))
        st.append_trace(source, _T0 + i, x, y.astype("f8"), epoch="t")
    return st, _T0, _T0 + n_scans


# -- timing ------------------------------------------------------------------
def time_it(run, setup=None, rounds=5, warmup=1) -> dict:
    """Median wall-clock of `run(ctx)` over `rounds` (after `warmup`). `setup()`
    (untimed) runs before each round — for write benchmarks needing a fresh store."""
    def once():
        ctx = setup() if setup is not None else None
        a = time.perf_counter()
        run(ctx)
        return time.perf_counter() - a
    for _ in range(max(0, warmup)):
        once()
    ts = [once() for _ in range(max(1, rounds))]
    return {"min": min(ts), "median": statistics.median(ts), "mean": statistics.mean(ts)}


def _row(path, size, unit, stats, n) -> dict:
    med = stats["median"]
    return {"path": path, "size": size, "unit": unit,
            "median_ms": med * 1000.0, "min_ms": stats["min"] * 1000.0,
            "rate": (n / med) if med > 0 else 0.0}     # items (samples/scans) per second


# -- the scenarios -----------------------------------------------------------
def scalar_scenarios(tmp, sizes=SCALAR_SIZES, rounds=5, on_progress=None,
                     cancel=None) -> list:
    """Read paths over scalar windows: decimated query, full-res read, the
    re-stream materialization, and CSV export."""
    out = []
    for n in sizes:
        if cancel is not None and cancel():
            break
        st, a, b = seed_scalar(os.path.join(tmp, f"sc{n}.zarr"), n)
        res = Resolver([RamTier(HistoryBuffer()), st])
        play = PlaybackSource(res, Bus())
        srcs = list(SOURCES)
        total = n * len(srcs)
        out.append(_row("resolver.query (decimated display)", n, "samples",
                        time_it(lambda _c: [res.query(s, a, b, 2000) for s in srcs],
                                rounds=rounds), total))
        if _tick(on_progress, out, cancel):
            return out
        out.append(_row("resolver.read_raw (full-res)", n, "samples",
                        time_it(lambda _c: [res.read_raw(s, a, b) for s in srcs],
                                rounds=rounds), total))
        if _tick(on_progress, out, cancel):
            return out
        out.append(_row("playback.read_window (re-stream)", n, "samples",
                        time_it(lambda _c: play.read_window(srcs, a, b),
                                rounds=max(2, rounds // 2)), total))
        if _tick(on_progress, out, cancel):
            return out
        meta = {s: {"name": s, "unit": "mbar", "dtype": "scalar"} for s in srcs}
        out.append(_row("export_window (CSV)", n, "samples",
                        time_it(lambda dest: export_window(dest, meta, res, a, b),
                                setup=lambda: tempfile.mkdtemp(dir=tmp),
                                rounds=max(2, rounds // 2)), total))
        if _tick(on_progress, out, cancel):
            return out
    return out


def trace_scenarios(tmp, sizes=TRACE_SIZES, rounds=5, on_progress=None,
                    cancel=None) -> list:
    out = []
    for n in sizes:
        if cancel is not None and cancel():
            break
        st, a, b = seed_trace(os.path.join(tmp, f"tr{n}.zarr"), n)
        res = Resolver([RamTier(HistoryBuffer()), st])
        out.append(_row("resolver.read_raw_trace", n, "scans",
                        time_it(lambda _c: res.read_raw_trace("rga/spec", a, b),
                                rounds=rounds), n))
        if _tick(on_progress, out, cancel):
            return out
        out.append(_row("resolver.query_trace (waterfall)", n, "scans",
                        time_it(lambda _c: res.query_trace("rga/spec", a, b, 400),
                                rounds=rounds), n))
        if _tick(on_progress, out, cancel):
            return out
    return out


def write_scenarios(tmp, rounds=3, on_progress=None, cancel=None) -> list:
    """The write hot path is NOT one bulk append — the StoreWriter flushes small
    batches into a growing store (a partial tail-chunk rewrite each time), and a
    slow source flushes ~seconds of samples. So the LEVER is the batch size; measure
    a fixed total appended in different batch sizes (small = the realistic case)."""
    out = []
    total = 8_000
    for batch in (20, 200, 4096):
        if cancel is not None and cancel():
            break

        def setup():
            d = tempfile.mkdtemp(dir=tmp)
            st = ZarrStore(os.path.join(d, "w.zarr"))
            st.add_source("dev/a")
            t = _T0 + np.arange(total) / _HZ
            v = 100.0 + np.random.default_rng(0).standard_normal(total)
            return st, t, v.astype("f8")

        def run(ctx, b=batch):
            st, t, v = ctx
            for i in range(0, len(t), b):
                st.append("dev/a", t[i:i + b], v[i:i + b], epoch="e")
        out.append(_row(f"ZarrStore.append (batch={batch})", batch, "/batch",
                        time_it(run, setup=setup, rounds=rounds), total))
        if _tick(on_progress, out, cancel):
            return out
    return out


def _tick(on_progress, out, cancel=None) -> bool:
    """Report progress after a measurement + poll cancel BETWEEN measurements (so a
    long run stops promptly, not only between whole load sizes). Returns True to stop."""
    if on_progress is not None:
        try:
            on_progress(len(out), out[-1])
        except Exception:                        # noqa: BLE001 — a bad observer ≠ abort
            pass
    return bool(cancel is not None and cancel())


def run_all(scalar_sizes=SCALAR_SIZES, trace_sizes=TRACE_SIZES, rounds=5,
            on_progress=None, cancel=None) -> list:
    """The full headless data-plane suite in a throwaway temp dir. `on_progress(k,
    row)` after each measurement; `cancel()` truthy → stop early. Returns rows."""
    tmp = tempfile.mkdtemp(prefix="ferrodac-bench-")
    try:
        rows = []
        rows += write_scenarios(tmp, max(2, rounds // 2), on_progress, cancel)
        rows += scalar_scenarios(tmp, scalar_sizes, rounds, on_progress, cancel)
        rows += trace_scenarios(tmp, trace_sizes, rounds, on_progress, cancel)
        return rows
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
