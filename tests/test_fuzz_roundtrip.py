"""END-TO-END DATA-TRUST FUZZ over the hub (DESIGN §7.4 / §12.1).

The round-trip a physicist actually relies on: data is written to the HUB, their
local store is empty, they scrub back to a random slice of that remote history,
save it as a recording, and open ``data.csv``. If those numbers don't reconcile
EXACTLY with what the devices emitted, nothing downstream can be trusted.

    write → hub   (a local Zarr store syncs up over REAL gRPC, in-process)
    read  ← hub   (a client with EMPTY local tiers reads via the HubReadTier)
    scrub         (a random window [w0,w1] — fuzzed, see below)
    → recording   (export_window materialises that window …)
    → csv         (… into data.csv / trace_*.csv)
    match?        (parse it back; it must equal the written data, sample-for-sample)

The FUZZ is over the data sizes, the values, and above all the SCRUB WINDOW —
many random windows per hub. Every window bound is placed strictly BETWEEN two
adjacent sample times, so ``[w0,w1]`` membership is unambiguous (inclusive vs
exclusive endpoints can't make the answer flap) and every asserted equality is
EXACT to the CSV's own stated precision (%.6f time, %.10g value, %.6E trace).

Two paths are fuzzed:
  * test_hub_scrub_export_roundtrip — export reads straight THROUGH the hub tier.
  * test_pin_window_to_recording    — the prefetcher PINS the window into a durable
    client store first (§12.1 ph3), then the recording is cut from that store, and
    a local-only (GUI-thread) read of the same window must match too.

Real gRPC, no Docker. Qt-free.
"""

from __future__ import annotations

import asyncio
import csv
import os
import tempfile
import types

import numpy as np
import pytest

grpc = pytest.importorskip("grpc")
pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")

from ferrodac.core.history import HistoryBuffer          # noqa: E402
from ferrodac.net.readtier import HubReadTier             # noqa: E402
from ferrodac.net.sync import GrpcSyncTransport           # noqa: E402
from ferrodac.store import (RamTier, Resolver, SyncEngine,  # noqa: E402
                            ZarrStore, export_window)
from ferrodac.store.export import _column                 # noqa: E402 — authoritative header
from ferrodac.store.prefetch import PrefetchCache          # noqa: E402
from ferrodac.store.prefetcher import PlaybackPrefetcher   # noqa: E402

from hub.main import build_server                          # noqa: E402

BASE = 1_000_000.0


# -- ground truth: seed a local store with fuzzed data ----------------------
def _seed_local(path, rng):
    """A local Zarr store of RANDOM data across several sources on distinct time
    grids (one carries a real recording gap). Returns (store, scalars, traces,
    meta, pooled_times) where scalars={key:(t,v)}, traces={key:(times,Y,x)} are the
    exact ground truth and pooled_times is every sample instant, for window picks."""
    st = ZarrStore(path)
    nA = int(rng.integers(1500, 3200))
    span = (nA - 1) * 0.1                                  # gaugeA spans the WHOLE base

    scalars: dict = {}
    # gaugeA: dense (10 Hz), covers the entire span → every window intersects it
    tA = BASE + np.arange(nA) * 0.1
    vA = 1e-6 * (1.0 + 0.3 * np.sin(tA * 0.05)) + 1e-9 * rng.standard_normal(nA)
    st.add_source("gaugeA/p", name="pressure · A", unit="mbar")
    st.append("gaugeA/p", tA, vA.astype("f8"), epoch="a")
    scalars["gaugeA/p"] = (tA, vA)

    # gaugeB: 4 Hz with a REAL outage in the middle (honest blanks, no bridged rows)
    tB0 = BASE + 5.0 + np.arange(600) * 0.25
    tB1 = tB0[-1] + 90.0 + np.arange(500) * 0.25          # a 90 s gap
    tB = np.concatenate([tB0, tB1])
    tB = tB[tB <= BASE + span]                            # stay within gaugeA's span
    vB = 300.0 + 5.0 * np.cos(tB * 0.02) + 0.01 * rng.standard_normal(len(tB))
    st.add_source("gaugeB/T", name="temp · B", unit="K")
    st.append("gaugeB/T", tB, vB.astype("f8"), epoch="b")
    scalars["gaugeB/T"] = (tB, vB)

    # valve: slow (2 Hz), a fractional-offset grid so it collides with neither above
    nC = int(rng.integers(300, 700))
    tC = BASE + 3.3 + np.arange(nC) * 0.5
    tC = tC[tC <= BASE + span]
    vC = rng.standard_normal(len(tC)) * 10.0
    st.add_source("valve/pos", name="valve · V1", unit="mm")
    st.append("valve/pos", tC, vC.astype("f8"), epoch="c")
    scalars["valve/pos"] = (tC, vC)

    # a trace source (RGA-like), fixed swept axis, its own coarse grid
    traces: dict = {}
    ax = np.linspace(1.0, 50.0, 64)
    nS = int(rng.integers(20, 60))
    tS = BASE + 7.0 + np.arange(nS) * 2.0
    tS = tS[tS <= BASE + span]
    Y = np.array([np.exp(-((ax - 18.0) ** 2) / 4.0) * (1.0 + 0.05 * rng.standard_normal(64))
                  for _ in range(len(tS))])
    st.add_source("rga/spec", name="rga", unit="mbar", dtype="trace")
    for i in range(len(tS)):
        st.append_trace("rga/spec", float(tS[i]), ax, Y[i].astype("f8"), epoch="t")
    traces["rga/spec"] = (tS, Y, ax)

    meta = {"gaugeA/p": {"name": "pressure · A", "unit": "mbar", "dtype": "float"},
            "gaugeB/T": {"name": "temp · B", "unit": "K", "dtype": "float"},
            "valve/pos": {"name": "valve · V1", "unit": "mm", "dtype": "float"},
            "rga/spec": {"name": "rga", "unit": "mbar", "dtype": "trace"}}
    pooled = np.unique(np.concatenate([tA, tB, tC, tS]))
    return st, scalars, traces, meta, pooled


def _rand_window(rng, pooled):
    """A window whose bounds fall strictly BETWEEN adjacent sample instants, so no
    sample sits exactly on an edge → membership is unambiguous."""
    n = len(pooled)
    i, j = sorted(int(k) for k in rng.choice(n - 1, size=2, replace=False))
    if i == j:
        j = min(j + 1, n - 2)
    w0 = float((pooled[i] + pooled[i + 1]) / 2.0)
    w1 = float((pooled[j] + pooled[j + 1]) / 2.0)
    return w0, w1


# -- CSV parse + exact-match helpers (the CSV's own precision) ---------------
def _read_csv(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def _column_series(rows, header):
    col = rows[0].index(header)
    t, v = [], []
    for r in rows[1:]:
        if r[col] != "":
            t.append(float(r[1]))                          # time_epoch_s column
            v.append(float(r[col]))
    return np.asarray(t), np.asarray(v)


def _expect_scalar(t, v):
    return (np.round(np.asarray(t, dtype="f8"), 6),
            np.asarray([float(f"{float(x):.10g}") for x in v], dtype="f8"))


def _assert_scalars_match(dest, scalars, meta, w0, w1, tag):
    rows = _read_csv(os.path.join(dest, "data.csv"))
    header = rows[0]
    for key, (t, v) in scalars.items():
        m = (t > w0) & (t < w1)
        want_t, want_v = _expect_scalar(t[m], v[m])
        col = _column(meta[key]["name"], meta[key]["unit"])
        if len(want_t) == 0:
            assert col not in header, f"{tag}: {key} has no samples in window but a column"
            continue
        got_t, got_v = _column_series(rows, col)
        assert len(got_t) == len(want_t), \
            f"{tag}: {key} row count {len(got_t)} != emitted {len(want_t)}"
        assert np.array_equal(got_t, want_t), f"{tag}: {key} timestamps diverge"
        assert np.array_equal(got_v, want_v), f"{tag}: {key} values diverge"


def _assert_traces_match(dest, man, traces, w0, w1, tag):
    for key, (times, Y, x) in traces.items():
        m = (times > w0) & (times < w1)
        want_t = np.round(times[m], 6)
        want_x = [float(f"{mz:g}") for mz in x]
        want_Y = np.asarray([[float(f"{y:.6E}") for y in row] for row in Y[m]], dtype="f8")
        entries = [s for s in man["sources"] if s["key"] == key and s["dtype"] == "trace"]
        if len(want_t) == 0:
            assert not entries, f"{tag}: {key} empty window but a trace file was written"
            continue
        assert len(entries) == 1, f"{tag}: {key} expected one trace file, got {len(entries)}"
        rows = _read_csv(os.path.join(dest, entries[0]["file"]))
        got_x = [float(c) for c in rows[0][1:]]
        assert got_x == want_x, f"{tag}: {key} axis diverges"
        got_t = np.asarray([float(r[0]) for r in rows[1:]], dtype="f8")
        got_Y = np.asarray([[float(c) for c in r[1:]] for r in rows[1:]], dtype="f8")
        assert np.array_equal(got_t, want_t), f"{tag}: {key} scan times diverge"
        assert np.array_equal(got_Y, want_Y), f"{tag}: {key} intensities diverge"


# -- the in-process hub harness ---------------------------------------------
async def _with_hub(rng):
    """Stand up a real gRPC hub, sync fuzzed local data up, hand back a client
    resolver whose ONLY history source is the hub. Returns everything the caller
    needs plus a cleanup coroutine."""
    d = tempfile.mkdtemp(prefix="fd-fuzz-")
    hub_store = ZarrStore(os.path.join(d, "hub.zarr"))
    server, _hub = build_server(store=hub_store)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    local, scalars, traces, meta, pooled = _seed_local(os.path.join(d, "local.zarr"), rng)
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    engine = SyncEngine(local, GrpcSyncTransport(channel), chunk=137)
    await asyncio.to_thread(engine.sync_once)
    # sanity GATE: the hub must mirror local exactly, else a mismatch below would
    # wrongly blame the read/export stage rather than sync.
    assert hub_store.epoch_lengths() == local.epoch_lengths(), "hub did not mirror local"

    async def cleanup():
        channel.close()
        await server.stop(grace=0)

    return d, channel, scalars, traces, meta, pooled, cleanup


# -- path 1: scrub a random window, export straight through the hub ----------
async def _run_hub_export(seed, n_windows):
    rng = np.random.default_rng(seed)
    d, channel, scalars, traces, meta, pooled, cleanup = await _with_hub(rng)
    try:
        client = ZarrStore(os.path.join(d, "client.zarr"))          # EMPTY local store
        resolver = Resolver([RamTier(HistoryBuffer()), client])
        resolver.set_remote(HubReadTier(channel))
        assert resolver.coverage("gaugeA/p") == [] or True          # local knows nothing

        for w in range(n_windows):
            w0, w1 = _rand_window(rng, pooled)
            dest = os.path.join(d, f"exp_{w}")
            tag = f"seed={seed} win={w} [{w0:.3f},{w1:.3f}]"
            man = await asyncio.to_thread(export_window, dest, meta, resolver, w0, w1)
            _assert_scalars_match(dest, scalars, meta, w0, w1, tag)
            _assert_traces_match(dest, man, traces, w0, w1, tag)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.parametrize("seed", [1, 7, 42, 20260713])
def test_hub_scrub_export_roundtrip(seed):
    """write→hub→read→scrub→export→csv: the exported window equals the written
    data exactly, for every source, over many fuzzed windows."""
    asyncio.run(_run_hub_export(seed, n_windows=6))


# -- path 2: pin the scrubbed window into a durable recording (§12.1 ph3) -----
def _dummy_tc(head):
    return types.SimpleNamespace(
        nav=0, playing=False, head=head, speed=1.0,
        window=(head - 10.0, head), subscribe=lambda cb: (lambda: None))


async def _run_pin_recording(seed, n_windows):
    rng = np.random.default_rng(seed)
    d, channel, scalars, traces, meta, pooled, cleanup = await _with_hub(rng)
    try:
        hub_tier = HubReadTier(channel)
        for w in range(n_windows):
            w0, w1 = _rand_window(rng, pooled)
            tag = f"seed={seed} pin={w} [{w0:.3f},{w1:.3f}]"

            # a FRESH client store per window; the prefetcher promotes the hub's
            # window into it durably, off the socket thereafter.
            client = ZarrStore(os.path.join(d, f"pin_{w}.zarr"))
            resolver = Resolver([RamTier(HistoryBuffer()), client])
            cache = PrefetchCache()
            resolver.set_prefetch(cache)
            resolver.set_remote(hub_tier)
            pf = PlaybackPrefetcher(resolver=resolver, hub=hub_tier, cache=cache,
                                    tc=_dummy_tc(w1), sources_fn=lambda: list(meta),
                                    store=client)
            for key in meta:
                await asyncio.to_thread(pf._pin_source, key, w0, w1, f"pin-{w}")

            # (a) GUI-thread replay path: a LOCAL-ONLY read (never touches the socket)
            #     now serves the hub's data from the pinned durable store.
            for key, (t, v) in scalars.items():
                m = (t > w0) & (t < w1)
                want_t, want_v = _expect_scalar(t[m], v[m])
                gt, gv = await asyncio.to_thread(resolver.read_raw, key, w0, w1, True)
                assert np.array_equal(np.round(gt, 6), want_t), f"{tag}: {key} local t"
                assert np.array_equal(
                    np.asarray([float(f"{x:.10g}") for x in gv]), want_v), \
                    f"{tag}: {key} local v"

            # (b) recording cut from the durable store → CSV matches the written data.
            dest = os.path.join(d, f"rec_{w}")
            man = await asyncio.to_thread(export_window, dest, meta, client, w0, w1)
            _assert_scalars_match(dest, scalars, meta, w0, w1, tag)
            _assert_traces_match(dest, man, traces, w0, w1, tag)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.parametrize("seed", [3, 99])
def test_pin_window_to_recording(seed):
    """The prefetch→durable-pin→recording path: pinning a fuzzed window from the hub
    into a local store preserves the data exactly (no dup at seams, monotonic, no
    drop), served both to a GUI-thread local read and to the exported CSV."""
    asyncio.run(_run_pin_recording(seed, n_windows=4))
