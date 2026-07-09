"""Self-test for the tiered resolver (DESIGN §7.4).
Run: python3 -m ferrodac.store.resolver_selftest

Composes a live HistoryBuffer (recent) + a ZarrStore (older recorded run) behind
one Resolver and checks: union coverage, seamless stitch across the local→RAM
handoff (no false NaN), nearest-wins in the overlap (RAM, the fresher tier), and
a NaN break at a genuine coverage gap.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from ..core.history import HistoryBuffer
from . import RamTier, Resolver, ZarrStore


class _R:                       # a minimal Reading-like object for HistoryBuffer.feed
    __slots__ = ("key", "t", "value", "status", "partial")

    def __init__(self, key, t, v):
        self.key, self.t, self.value, self.status, self.partial = key, t, v, 0, False


def _feed(hist, key, t0, t1, value, hz=10):
    hist.feed([_R(key, t, value) for t in np.arange(t0, t1, 1.0 / hz)])


def main() -> int:
    now = 1_000_000.0
    d = tempfile.mkdtemp()
    store = ZarrStore(os.path.join(d, "run"))
    hist = HistoryBuffer(window_s=5000)
    res = Resolver([RamTier(hist), store])      # nearest → far: RAM, then local

    # series A — local [-1000,-400] (v=1) overlapping RAM [-500,0] (v=2): continuous
    store.add_source("A")
    ta = np.arange(now - 1000, now - 400, 0.1)
    store.append("A", ta, np.ones(len(ta)), epoch="e0")
    store.finalize_rollups("A")
    _feed(hist, "A", now - 500, now, 2.0)

    # series B — local [-1000,-700] (v=1), RAM [-300,0] (v=2): a gap in [-700,-300]
    store.add_source("B")
    tb = np.arange(now - 1000, now - 700, 0.1)
    store.append("B", tb, np.ones(len(tb)), epoch="e0")
    store.finalize_rollups("B")
    _feed(hist, "B", now - 300, now, 2.0)

    # coverage = merged union across tiers (RAM tail ends ~now-0.1)
    cov = res.coverage("A")
    assert len(cov) == 1 and abs(cov[0][0] - (now - 1000)) < 1 and abs(cov[0][1] - now) < 1, cov
    print("✓ coverage A = merged union [-1000, 0]")

    # full A query: continuous, NO false NaN at the local→RAM handoff
    x, y = res.query("A", now - 1000, now, max_points=2000)
    assert not np.isnan(y).any(), "false gap across a continuous handoff"
    assert x.min() <= now - 990 and x.max() >= now - 10
    print(f"✓ A stitched local→RAM seamlessly: {len(x)} pts, no NaN, spans the range")

    # nearest-wins: the overlap [-500,-400] is served by RAM (v≈2), not local (v=1)
    _, yo = res.query("A", now - 480, now - 420, max_points=2000)
    assert abs(np.nanmean(yo) - 2.0) < 0.01, np.nanmean(yo)
    # local-only region still reads local (v≈1)
    _, yl = res.query("A", now - 800, now - 700, max_points=2000)
    assert abs(np.nanmean(yl) - 1.0) < 0.01, np.nanmean(yl)
    print("✓ nearest-wins: overlap served by RAM (2.0), local-only by store (1.0)")

    # series B: a real gap → exactly one NaN break, values 1 then 2 around it
    xb, yb = res.query("B", now - 1000, now, max_points=2000)
    assert np.isnan(yb).any(), "gap not broken"
    assert abs(np.nanmean(yb[xb < now - 700]) - 1.0) < 0.01
    assert abs(np.nanmean(yb[xb > now - 300]) - 2.0) < 0.01
    print("✓ gap honored: NaN break in [-700,-300], v=1 before / v=2 after")

    # series C — a device RECONNECT mid-session: two runs land in the SAME epoch with a
    # big time gap between them (a reconnect does NOT roll an epoch). coverage() must
    # SPLIT it (else the chart AND the Timeline preview draw a straight line across the
    # outage), while read_raw stays FULL-RES and NaN-free (the physics invariant).
    store.add_source("C")
    tc1 = np.arange(now - 5000, now - 4700, 0.1)         # run 1
    tc2 = np.arange(now - 1000, now - 700, 0.1)          # run 2, ~3700 s later
    store.append("C", tc1, np.ones(len(tc1)), epoch="e0")
    store.append("C", tc2, 2 * np.ones(len(tc2)), epoch="e0")   # SAME epoch, no roll
    store.finalize_rollups("C")
    covc = store.coverage("C")
    assert len(covc) == 2, f"same-epoch gap not split: {covc}"
    assert covc[0][1] < now - 4600 and covc[1][0] > now - 1100, covc
    xc, yc = res.query("C", now - 5000, now - 700, max_points=2000)
    assert np.isnan(yc).any(), "same-epoch reconnect gap not broken in query()"
    tr, vr = store.read_raw("C", now - 5000, now - 700)    # physics: read_raw untouched
    assert len(tr) == len(tc1) + len(tc2) and not np.isnan(vr).any(), "read_raw altered"
    print("✓ same-epoch reconnect gap: coverage split in two, query breaks, read_raw intact")

    # series D — a MID-RECORDING rollup (the writer rolls up every N samples) then more
    # appends must NOT drop the un-rolled-up tail: coverage() must report the fresh full
    # extent while the epoch is dirty, not the stale cached `intervals`. (Data-loss regression.)
    store.add_source("D")
    td1 = np.arange(now - 2000, now - 1000, 0.1)
    store.append("D", td1, np.ones(len(td1)), epoch="e0")
    store.finalize_rollups("D", "e0")                 # rollup mid-recording → caches intervals
    td2 = np.arange(now - 1000, now - 500, 0.1)       # …then keep recording (epoch dirty again)
    store.append("D", td2, 2 * np.ones(len(td2)), epoch="e0")
    covd = store.coverage("D")
    assert len(covd) == 1 and covd[0][1] > now - 501, covd   # full extent, not stale
    xd, _ = res.read_raw("D", now - 2000, now - 500)
    assert len(xd) == len(td1) + len(td2), f"un-rolled-up tail dropped: {len(xd)}"
    print("✓ mid-recording rollup + append: coverage spans full extent, read_raw keeps all")

    # series E — an isolated sample (a 1-flap reconnect) must survive read_raw, not be
    # dropped by a ZERO-WIDTH coverage interval that _partition can't own. (Data-loss regression.)
    store.add_source("E")
    te = np.concatenate([np.arange(now - 5000, now - 4900, 0.1), [now - 1000.0]])
    store.append("E", te, np.ones(len(te)), epoch="e0")
    store.finalize_rollups("E", "e0")
    cove = store.coverage("E")
    assert len(cove) == 2, f"isolated sample not split out: {cove}"
    xe, _ = res.read_raw("E", now - 5000, now - 900)
    assert len(xe) == len(te), f"isolated flap sample dropped: {len(xe)} != {len(te)}"
    print("✓ isolated flap sample: point interval widened, read_raw keeps it")

    # series F — a DIRTY epoch (still recording, never finalized) with a real gap must be
    # SPLIT in coverage too, else a farther tier's _merge(union) swallows the gap and the live
    # view ramps across it (C2). read_raw must still keep every sample.
    store.add_source("F")
    tf1 = np.arange(now - 3000, now - 2900, 0.1)
    tf2 = np.arange(now - 2000, now - 1900, 0.1)       # ~900s gap; NO finalize → epoch dirty
    store.append("F", tf1, np.ones(len(tf1)), epoch="e0")
    store.append("F", tf2, 2 * np.ones(len(tf2)), epoch="e0")
    covf = store.coverage("F")
    assert len(covf) == 2, f"dirty epoch gap not split: {covf}"
    xf, _ = res.read_raw("F", now - 3000, now - 1900)
    assert len(xf) == len(tf1) + len(tf2), f"dirty read_raw dropped: {len(xf)}"
    print("✓ dirty epoch with a gap: coverage splits (no union-swallow), read_raw keeps all")

    # series G/H — a zero-width degenerate coverage (a lone dirty sample, or 3+ all-equal
    # timestamps) must NOT drop the sample from resolver.read_raw (C3 physics no-loss).
    store.add_source("G")
    store.append("G", np.array([now - 500.0]), np.array([1.0]), epoch="e0")     # ONE sample, dirty
    covg = store.coverage("G")
    assert covg and covg[0][1] > covg[0][0], f"zero-width interval not widened: {covg}"
    xg, _ = res.read_raw("G", now - 600, now - 400)
    assert len(xg) == 1, f"lone dirty sample dropped: {len(xg)}"
    store.add_source("H")
    store.append("H", np.full(20, now - 100.0), np.arange(20.0), epoch="e0")     # 20 all-equal ts
    store.finalize_rollups("H", "e0")
    xh, _ = res.read_raw("H", now - 200, now)
    assert len(xh) == 20, f"all-equal finalized samples dropped: {len(xh)}"
    print("✓ zero-width coverage widened: lone/all-equal samples survive read_raw")

    # --- local_only: the remote tier must be UNREACHABLE (regression 2026-07-09:
    # the chart gap-break provider and the per-play-tick advance path reached the
    # hub tier's networked GetCoverage synchronously on the GUI thread — watchdog
    # stalls >700 ms). knows/coverage/read_raw with local_only=True must never
    # touch the remote; without it, the hub still fills remote-only history.
    class _CountingRemote:
        def __init__(self):
            self.calls = 0

        def coverage(self, series):
            self.calls += 1
            return [(now - 10.0, now - 9.0)]

        def read_raw(self, series, a, b):
            self.calls += 1
            return np.array([now - 9.5]), np.array([42.0])

    remote = _CountingRemote()
    r2 = Resolver([store])
    r2.set_remote(remote)
    r2.coverage("hub-only/x", local_only=True)
    assert not r2.knows("hub-only/x", local_only=True)
    r2.read_raw("hub-only/x", now - 20, now, local_only=True)
    assert remote.calls == 0, f"local_only leaked {remote.calls} remote calls"
    assert r2.knows("hub-only/x")        # the full resolver still reaches the hub
    t_h, v_h = r2.read_raw("hub-only/x", now - 20, now)
    assert remote.calls >= 2 and list(v_h) == [42.0]
    print("✓ local_only shields the remote tier; full reads still reach it")

    print("\nRESOLVER SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
