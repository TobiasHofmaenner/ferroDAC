"""Self-test for the always-on StoreWriter (DESIGN §7.4).
Run: python3 -m ferrodac.store.writer_selftest

Streams synthetic readings through a fake engine into the durable store, checks
it persists continuously (queryable mid-run, grows, survives a reopen), skips
partial/non-scalar frames, and that on stop the rollups make a wide query cheap.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from . import StoreWriter, ZarrStore


class _Engine:
    def __init__(self):
        self._subs = []

    def subscribe(self, cb):
        self._subs.append(cb)
        return lambda: self._subs.remove(cb)

    def publish(self, batch):
        for cb in list(self._subs):
            cb(batch)


class _R:
    __slots__ = ("key", "t", "value", "status", "partial")

    def __init__(self, key, t, v, partial=False):
        self.key, self.t, self.value, self.status, self.partial = key, t, v, 0, partial


def main() -> int:
    now = 1_000_000.0
    d = tempfile.mkdtemp()
    root = os.path.join(d, "store.zarr")
    store = ZarrStore(root)
    eng = _Engine()
    w = StoreWriter(store, chunk=200, rollup_every=2000)
    w.attach(eng)

    # stream 10k samples @10 Hz for "g1" in batches of 250 (multiple flushes)
    t = now - 1000 + np.arange(10_000) * 0.1
    v = 1e-8 * (1 + 0.3 * np.sin(t))
    for i in range(0, len(t), 250):
        eng.publish([_R("g1", float(t[j]), float(v[j]))
                     for j in range(i, min(i + 250, len(t)))])
    # noise that must be ignored: a still-filling frame + a non-scalar value
    eng.publish([_R("g1", now, 9.9, partial=True), _R("g1", now, "n/a")])
    w.flush_all()

    # queryable MID-RUN (no stop yet) — the ambient durable tier is live
    x, y = store.query("g1", now - 1000, now, max_points=1000)
    assert len(x) > 0 and abs(np.nanmean(y) - 1e-8) < 5e-9, (len(x), np.nanmean(y))
    print(f"✓ persists continuously: queryable mid-run ({len(x)} pts)")
    assert store.coverage("g1") and store.coverage("g1")[0][0] <= now - 999
    print("✓ grows: coverage spans the streamed range")
    assert 9.9 not in y, "partial/non-scalar leaked into the store"
    print("✓ skipped the partial frame and the non-scalar value")

    w.stop()                                       # final flush + rollups
    xs, _ = store.query("g1", now - 1000, now, max_points=500)
    assert len(xs) < 1500, len(xs)
    print(f"✓ stop() built rollups → wide query bounded ({len(xs)} pts)")

    # survives a reopen (durable, not RAM)
    st2 = ZarrStore(root, mode="r")
    assert st2.sources() == ["g1"] and len(st2.query("g1", now - 1000, now, 500)[0]) > 0
    print("✓ durable: reopened a fresh handle and re-queried")

    print("\nWRITER SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
