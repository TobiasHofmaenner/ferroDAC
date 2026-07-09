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

    # NB: point count vs budget is a STEP function (F=16 between pyramid levels),
    # so the budget pair must straddle a level boundary — 1000→4000 stopped doing
    # that when budgets became proportional-by-overlap (both land on level 2).
    counts = [len(st.query(uid, t0 - 10, tb[-1] + 10, max_points=mp)[0])
              for mp in (1000, 16000)]
    assert counts[1] > counts[0], counts
    print(f"✓ resolution scales with budget: {counts} pts for max_points 1000/16000")

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

    # --- dirty-tail top-up (regression 2026-07-09): the pyramid is rebuilt only
    # every ~50k samples, so a wide (coarse-level) query used to end at the last
    # finalize — a right-edge hole that vanished on zoom-in while coverage()
    # (dirty-aware) still reported the span. query() must top up from raw.
    dk = "dirty/gauge"
    st.add_source(dk, name="dirty")
    dt0 = time.time() - 40_000
    dta = dt0 + np.arange(200_000) * 0.1
    st.append(dk, dta, np.sin(dta * 0.01), epoch="d0")
    st.finalize_rollups(dk, "d0")
    dtb = dta[-1] + 0.1 + np.arange(5_000) * 0.1      # appended, NOT finalized
    st.append(dk, dtb, np.cos(dtb * 0.01), epoch="d0")
    qx, qy = st.query(dk, dt0, dtb[-1] + 1, max_points=800)
    fx = qx[np.isfinite(qx)]
    assert fx.max() >= dtb[-1] - 1.0, f"dirty tail missing ({dtb[-1] - fx.max():.0f}s hole)"
    assert (np.diff(fx) >= 0).all(), "pyramid→tail seam out of order"
    print(f"✓ dirty tail served at coarse zoom: query reaches {dtb[-1] - fx.max():.1f}s of tail end")

    # --- NaN-robust rollups (regression 2026-07-09): legacy stores hold NaN for
    # failed reads; plain min/max poisoned the bucket at every level, so one bad
    # sample grew into a 16^L-wide hole at wide zoom. Finite extrema must win.
    nk = "legacy/nan"
    st.add_source(nk, name="nan")
    nt = dt0 + np.arange(100_000) * 0.1
    nv = np.ones(100_000)
    nv[50_000] = np.nan                                # one legacy bad sample
    st.append(nk, nt, nv, epoch="n0")
    st.finalize_rollups(nk, "n0")
    qx, qy = st.query(nk, dt0, nt[-1] + 1, max_points=400)
    assert not np.isnan(qy[np.isfinite(qx)]).any(), "one NaN poisoned its rollup bucket"
    print("✓ NaN-robust rollups: a lone legacy NaN no longer poisons coarse levels")

    # --- proportional epoch budgets (regression 2026-07-09): an even per-epoch
    # split let restart stubs steal a long epoch's resolution the moment they
    # entered the window; budget must follow each epoch's overlap share.
    pk = "prop/gauge"
    st.add_source(pk, name="prop")
    pt = dt0 + np.arange(500_000) * 0.1
    st.append(pk, pt, np.sin(pt * 0.001), epoch="p0")
    for i in range(4):                                 # four 10-sample restart stubs
        ps = pt[-1] + 100 + i * 200 + np.arange(10) * 0.1
        st.append(pk, ps, np.ones(10), epoch=f"pstub{i}")
    st.finalize_rollups(pk)
    qx, qy = st.query(pk, dt0, pt[-1] + 1000, max_points=2000)
    on_main = (np.isfinite(qx) & (qx <= pt[-1])).sum()
    assert on_main > 1500, f"stubs stole the long epoch's budget ({on_main} pts)"
    print(f"✓ proportional budgets: long epoch kept {on_main} of 2000 pts despite 4 stubs")

    print("\nSTORE SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
