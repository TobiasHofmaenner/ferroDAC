"""Store-and-forward sync + read tier over REAL gRPC (DESIGN §12.1).

An agent syncs a local Zarr store to the hub's Zarr store over the Store service
(GetSyncState + PushChunk), and the hub is then read back as a resolver tier
(ListSources / GetCoverage / Query / ReadRaw). Verifies cold backfill mirrors
exactly, live tails upload incrementally, and the read RPCs return the data.

Run (from server/):  PYTHONPATH=..:.:gen python tests/sync_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault("GRPC_VERBOSITY", "NONE")

import grpc
import numpy as np

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from hub.main import build_server
from ferrodac.store import ZarrStore, SyncEngine
from ferrodac.net.sync import GrpcSyncTransport

BASE = 1_000_000.0


def _make_local(path):
    st = ZarrStore(path)
    st.add_source("dev/g1")
    t = BASE + np.arange(400) * 0.1
    st.append("dev/g1", t, t - BASE, epoch="s1")           # value == seconds-from-base
    ax = np.linspace(1, 50, 64)
    st.add_source("rga/spec", dtype="trace")
    for i in range(12):
        st.append_trace("rga/spec", BASE + i, ax, np.exp(-((ax - 18) ** 2)), epoch="t1")
    return st


async def main() -> int:
    d = tempfile.mkdtemp()
    hub_store = ZarrStore(os.path.join(d, "hub.zarr"))
    server, _hub = build_server(store=hub_store)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    addr = f"127.0.0.1:{port}"

    local = _make_local(os.path.join(d, "local.zarr"))
    channel = grpc.insecure_channel(addr)
    engine = SyncEngine(local, GrpcSyncTransport(channel), chunk=100)

    # 1) cold connect → backfill everything over gRPC; hub mirrors local exactly
    n = await asyncio.to_thread(engine.sync_once)
    assert hub_store.epoch_lengths() == local.epoch_lengths(), \
        (hub_store.epoch_lengths(), local.epoch_lengths())
    lt, lv = local.read_raw("dev/g1", BASE, BASE + 100)
    ht, hv = hub_store.read_raw("dev/g1", BASE, BASE + 100)
    assert np.allclose(lt, ht) and np.allclose(lv, hv, equal_nan=True)
    print(f"✓ cold sync over gRPC: {n} samples; hub mirrors local (scalars + traces)")

    # 2) live tail: append locally, re-sync → only the new samples go up
    local.append("dev/g1", BASE + 40 + np.arange(200) * 0.1, np.zeros(200), epoch="s1")
    n = await asyncio.to_thread(engine.sync_once)
    assert n == 200 and hub_store.epoch_lengths()[("dev/g1", "s1")] == 600
    print(f"✓ live tail over gRPC: uploaded {n}; idempotent otherwise")

    # 3) READ TIER: read the hub back via the Store service (as a resolver tier)
    stub = rpc.StoreStub(channel)
    srcs = await asyncio.to_thread(lambda: stub.ListSources(pb.SourcesRequest()))
    keys = {s.key for s in srcs.sources}
    assert {"dev/g1", "rga/spec"} <= keys, keys
    cov = await asyncio.to_thread(
        lambda: stub.GetCoverage(pb.CoverageRequest(source="dev/g1")))
    assert cov.intervals and cov.intervals[0].t0 <= BASE + 1
    raw = await asyncio.to_thread(
        lambda: stub.ReadRaw(pb.RawRequest(source="dev/g1", t0=BASE, t1=BASE + 5)))
    assert len(raw.t) > 0 and abs(raw.v[0]) < 1e-6        # value≈seconds-from-base≈0
    qy = await asyncio.to_thread(
        lambda: stub.Query(pb.QueryRequest(source="dev/g1", t0=BASE, t1=BASE + 60,
                                           max_points=200)))
    assert len(qy.x) > 0
    print(f"✓ read tier over gRPC: ListSources={len(keys)}, coverage, "
          f"ReadRaw={len(raw.t)} pts, Query={len(qy.x)} pts")

    # 4) HUB AS A RESOLVER TIER: a client with EMPTY local tiers reads the hub's
    #    history through the resolver (the wiped-local-store / viewer scenario).
    from ferrodac.store import RamTier, Resolver, ZarrStore as _ZS
    from ferrodac.core.history import HistoryBuffer
    from ferrodac.net.readtier import HubReadTier
    empty = _ZS(os.path.join(d, "client.zarr"))
    resolver = Resolver([RamTier(HistoryBuffer()), empty])
    assert resolver.coverage("dev/g1") == []                  # nothing locally
    resolver.set_remote(HubReadTier(channel))
    rcov = await asyncio.to_thread(lambda: resolver.coverage("dev/g1"))
    rx, _ = await asyncio.to_thread(
        lambda: resolver.query("dev/g1", BASE, BASE + 60, max_points=200))
    assert rcov and len(rx) > 0, (rcov, len(rx))
    # full-res TRACE read-back over the wire (the waterfall/replay path)
    resolver.set_remote(HubReadTier(channel))
    tblocks = await asyncio.to_thread(
        lambda: resolver.read_raw_trace("rga/spec", BASE, BASE + 100))
    nscan = sum(len(t) for t, _Y, _x in tblocks)
    assert tblocks and nscan == 12 and tblocks[0][1].shape[1] == 64, (nscan, tblocks[0][1].shape)
    dtv = await asyncio.to_thread(lambda: resolver.source_dtype("rga/spec"))
    assert dtv == "trace", dtv
    print(f"✓ hub TRACE read tier: ReadRawTrace → {nscan} full-res scans "
          f"(m={tblocks[0][1].shape[1]} bins), dtype classified")
    resolver.clear_remote()
    assert resolver.coverage("dev/g1") == []                  # detaches cleanly
    print(f"✓ hub as resolver tier: empty local → reads hub history "
          f"(coverage={len(rcov)}, query={len(rx)} pts), detaches clean")

    channel.close()
    await server.stop(grace=0)
    print("\nSYNC E2E PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
