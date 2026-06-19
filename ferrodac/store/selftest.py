"""Self-test for the local Zarr store (DESIGN §7.4). Run: python3 -m ferrodac.store.selftest

Exercises the tier protocol end to end with no GUI: epochs (config/shape change),
the rollup pyramid (resolution-aware, budget-bounded, peak-preserving), the
config/state stream (fold to state-at-T), coverage, and persistence on reopen.
"""

from __future__ import annotations

import os
import tempfile
import time

import numpy as np

from . import ZarrStore


def main() -> int:
    d = tempfile.mkdtemp()
    root = os.path.join(d, "run1")
    st = ZarrStore(root)
    uid = "11111111-2222-3333"
    st.add_source(uid, name="Ion gauge", unit="mbar", dtype="scalar")

    # epoch e0 — 50k samples @10 Hz with a lone spike (peak-survival check)
    t0 = time.time() - 5000
    t = t0 + np.arange(50_000) * 0.1
    v = 1e-8 * (1 + 0.2 * np.sin(t)); v[25_000] = 5e-7
    st.emit_config(uid, t0, "filament", "on")
    st.emit_config(uid, t0, "range", "1e-8")
    st.append(uid, t, v, epoch="e0")

    # epoch e1 — a range change → new config-epoch (different meaning)
    t1 = t[-1] + 1
    tb = t1 + np.arange(30_000) * 0.1
    vb = 3e-9 * (1 + 0.1 * np.cos(tb))
    st.emit_config(uid, t1, "range", "1e-9")
    st.append(uid, tb, vb, epoch="e1")
    st.finalize_rollups(uid)

    cov = st.coverage(uid)
    assert len(cov) == 2, cov
    print(f"✓ coverage: 2 epochs, spans {[round(b - a) for a, b in cov]} s")

    x, y = st.query(uid, t0 - 10, tb[-1] + 10, max_points=1000)
    assert len(x) < 2500, f"wide query not bounded: {len(x)}"
    assert np.nanmax(y) > 4e-7, "spike lost in the envelope"
    assert np.isnan(y).any(), "epochs not separated"
    print(f"✓ wide query: {len(x)} pts (budget-bounded), spike survived, epoch gap present")

    counts = [len(st.query(uid, t0 - 10, tb[-1] + 10, max_points=mp)[0])
              for mp in (1000, 4000)]
    assert counts[1] > counts[0], counts
    print(f"✓ resolution scales with budget: {counts} pts for max_points 1000/4000")

    xa, _ = st.query(uid, t0 + 100, t0 + 110, max_points=1000)
    assert 90 <= len(xa) <= 120, len(xa)
    print(f"✓ narrow 10 s query: {len(xa)} pts (raw)")

    assert st.config_at(uid, t0 + 1) == {"filament": "on", "range": "1e-8"}
    assert st.config_at(uid, t1 + 1)["range"] == "1e-9"
    print("✓ config folds to state-at-T (range 1e-8 → 1e-9 across the epoch)")

    st2 = ZarrStore(root, mode="r")
    assert st2.sources() == [uid]
    assert len(st2.query(uid, t0 - 10, tb[-1] + 10, max_points=500)[0]) > 0
    print("✓ persists: reopened read-only and re-queried")

    # cross-platform group names: real device ids carry ':' (e.g. 'sim:gauge:A')
    # which is ILLEGAL in Windows paths. The on-disk group dir must contain no
    # reserved char, and write/read_raw/query must round-trip the colon key.
    ck = "sim:gauge:A/p"
    st.add_source(ck, name="Pirani", unit="mbar")
    ct = time.time() + np.arange(200) * 0.2
    st.append(ck, ct, 1e-6 * (1 + 0.1 * np.sin(ct)), epoch="c0")
    gdir = ZarrStore._gname(ck)
    assert not any(c in gdir for c in ':*?"<>|\\/'), gdir
    assert gdir in os.listdir(root), (gdir, os.listdir(root))
    rt, rv = st.read_raw(ck, ct[0] - 1, ct[-1] + 1)
    assert len(rt) == 200 and len(st.query(ck, ct[0] - 1, ct[-1] + 1)[0]) > 0
    assert st.read_raw("absent:dev/x", 0, 1)[0].size == 0   # missing → empty, no raise
    print(f"✓ Windows-safe group names: ':' key → dir '{gdir}', read_raw round-trips")

    print("\nSTORE SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
