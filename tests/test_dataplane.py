"""The data-plane suite: run each Qt-free self-test as a real pass/fail.

These wrap the existing `ferrodac.store.*_selftest` / `ferrodac.core.graph_selftest`
modules (each exposes `main() -> int`, 0 = pass) so `pytest` discovers them, gives
a clean summary, and CI can gate on them. They need only numpy + zarr (no Qt, no
gRPC) — the durable store, tiered resolver, replay engine, store-and-forward sync
and the dataflow graph.
"""

import importlib

import pytest

SELFTESTS = [
    "ferrodac.store.selftest",            # ZarrStore: epochs, rollups, config stream
    "ferrodac.store.resolver_selftest",   # tiered resolver: nearest-wins + stitch
    "ferrodac.store.writer_selftest",     # StoreWriter: durable, traces, bool, rollups
    "ferrodac.store.replay_selftest",     # TimeContext + PlaybackSource + controller
    "ferrodac.store.sync_selftest",       # store-and-forward sync (in-process)
    "ferrodac.core.graph_selftest",       # DataflowGraph
]


@pytest.mark.parametrize("module", SELFTESTS)
def test_selftest_passes(module):
    mod = importlib.import_module(module)
    assert mod.main() == 0, f"{module}.main() reported failure"
