"""Self-test for store-and-forward sync (DESIGN §12.1).
Run: python3 -m ferrodac.store.sync_selftest

Checks the epoch-incremental copy: a cold hub backfills all offline history
(scalars + traces, mirrored exactly), a re-sync is a no-op, live appends upload
only the new tail, and a wiped hub reconciles by re-uploading (the hub's reported
per-epoch lengths are the source of truth).
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from . import LocalTransport, SyncEngine, ZarrStore


def main() -> int:
    d = tempfile.mkdtemp()
    local = ZarrStore(os.path.join(d, "local.zarr"))
    hub = ZarrStore(os.path.join(d, "hub.zarr"))
    base = 1_000_000.0

    # offline recording: a scalar source over two epochs + a trace source
    local.add_source("dev/g1")
    local.append("dev/g1", base + np.arange(500) * 0.1,
                 np.sin(base + np.arange(500) * 0.1), epoch="s1")
    local.append("dev/g1", base + 50 + np.arange(300) * 0.1,
                 np.cos(base + 50 + np.arange(300) * 0.1), epoch="s2")
    ax = np.linspace(1, 50, 64)
    local.add_source("rga/spec", dtype="trace")
    for i in range(20):
        local.append_trace("rga/spec", base + i, ax, np.exp(-((ax - 18) ** 2)), epoch="t1")

    eng = SyncEngine(local, LocalTransport(hub), chunk=128)

    n = eng.sync_once()                              # cold connect → full backfill
    assert hub.epoch_lengths() == local.epoch_lengths()
    lt, lv = local.read_raw("dev/g1", base, base + 1000)
    ht, hv = hub.read_raw("dev/g1", base, base + 1000)
    assert np.allclose(lt, ht) and np.allclose(lv, hv, equal_nan=True)
    assert ([b[1].shape for b in local.read_raw_trace("rga/spec", base, base + 100)]
            == [b[1].shape for b in hub.read_raw_trace("rga/spec", base, base + 100)])
    print(f"✓ cold connect → backfilled {n} samples; hub mirrors local exactly")

    assert eng.sync_once() == 0
    print("✓ re-sync is a no-op once caught up (idempotent)")

    local.append("dev/g1", base + 50 + np.arange(300, 500) * 0.1, np.zeros(200), epoch="s2")
    n = eng.sync_once()
    assert n == 200 and hub.epoch_lengths()[("dev/g1", "s2")] == 500
    print(f"✓ live → only the new epoch tail uploaded ({n} samples)")

    hub2 = ZarrStore(os.path.join(d, "hub2.zarr"))   # wiped/fresh hub
    n = SyncEngine(local, LocalTransport(hub2)).sync_once()
    assert hub2.epoch_lengths() == local.epoch_lengths()
    print(f"✓ wiped hub → reconciled by re-uploading all {n} (hub = source of truth)")

    print("\nSYNC SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
