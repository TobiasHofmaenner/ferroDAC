"""Self-test for the replay engine (DESIGN §7.4).
Run: python3 -m ferrodac.store.replay_selftest

Checks full-res read_raw (no downsampling), the TimeContext head/window/observer,
and that PlaybackSource streams EVERY raw sample through a Bus in global time
order and in chunks — the "re-experience history at full resolution" path.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from ..core.bus import Bus
from . import PlaybackSource, TimeContext, ZarrStore


def main() -> int:
    d = tempfile.mkdtemp()
    st = ZarrStore(os.path.join(d, "s.zarr"))
    base = 1_000_000.0
    # two sources at different rates, interleaved in time
    st.add_source("dev/a"); st.add_source("dev/b")
    ta = base + np.arange(3000) * 0.1                 # 10 Hz
    tb = base + np.arange(600) * 0.5                  # 2 Hz
    st.append("dev/a", ta, np.sin(ta), epoch="e0")
    st.append("dev/b", tb, np.cos(tb), epoch="e0")
    st.finalize_rollups("dev/a"); st.finalize_rollups("dev/b")

    # read_raw is FULL resolution (every sample), unlike query's envelope
    rt, rv = st.read_raw("dev/a", base, base + 300)
    assert len(rt) == 3000, len(rt)
    qx, _ = st.query("dev/a", base, base + 300, max_points=500)
    assert len(qx) < 1100, len(qx)                    # query downsamples; read_raw doesn't
    print(f"✓ read_raw full-res ({len(rt)} samples) vs query envelope ({len(qx)})")

    # TimeContext: window, park/follow, observer
    fired = [0]
    tc = TimeContext(width=300.0, now_fn=lambda: base + 300)
    tc.subscribe(lambda: fired.__setitem__(0, fired[0] + 1))
    assert tc.following and tc.window == (base, base + 300)
    tc.park(base + 100); assert not tc.following and tc.head == base + 100
    tc.follow_now(); assert tc.following and tc.head == base + 300
    assert fired[0] >= 2
    print("✓ TimeContext: window, park/follow, observer fires")

    # PlaybackSource: stream the whole window through a Bus → a sink
    bus = Bus()
    got: list = []
    drains = [0]
    bus.subscribe(lambda batch: (got.extend(batch), drains.__setitem__(0, drains[0] + 1)))
    ps = PlaybackSource(st, bus, chunk=500)
    n = ps.stream(["dev/a", "dev/b"], base, base + 300)
    assert n == len(got) == 3000 + 600, (n, len(got))   # EVERY sample, both sources
    ts = [r.t for r in got]
    assert ts == sorted(ts), "not globally time-ordered"
    assert drains[0] > 1, "not chunked"
    assert {r.key for r in got} == {"dev/a", "dev/b"}
    print(f"✓ PlaybackSource: streamed all {n} samples, time-ordered, "
          f"in {drains[0]} chunks, keys preserved")

    # PlaybackSource replays TRACE sources too (2-D scans → Trace readings),
    # full-res, so the spectrum re-experiences history just like scalars.
    from ..core.trace import Trace
    axis = np.linspace(1, 50, 64)
    st.add_source("rga/spec", dtype="trace")
    for i in range(20):
        st.append_trace("rga/spec", base + i, axis,
                        np.exp(-((axis - 18) ** 2)), epoch="e0")
    tbus = Bus(); tgot: list = []
    tbus.subscribe(lambda b: tgot.extend(b))
    nt = PlaybackSource(st, tbus, chunk=8).stream(["rga/spec"], base, base + 19)
    assert nt == len(tgot) == 20, (nt, len(tgot))
    assert all(isinstance(r.value, Trace) for r in tgot), "trace not reconstructed"
    assert tgot[0].value.y.shape == (64,) and tgot[0].key == "rga/spec"
    print(f"✓ PlaybackSource: replayed {nt} TRACE scans full-res as Trace readings")

    # ReplayController: one playback bus, fed by live or historic per the head
    from ..core.reading import Reading
    from . import ReplayController

    class _Eng:
        def __init__(s): s.subs = []
        def subscribe(s, cb): s.subs.append(cb); return lambda: s.subs.remove(cb)
        def pub(s, b): [cb(b) for cb in list(s.subs)]

    eng = _Eng()
    tc = TimeContext(width=300.0, now_fn=lambda: base + 300)
    resets = [0]
    out: list = []
    ctl = ReplayController(eng, st, tc, sources=lambda: ["dev/a"],
                           on_reset=lambda: (out.clear(), resets.__setitem__(0, resets[0] + 1)))
    ctl.bus.subscribe(lambda b: out.extend(b))

    eng.pub([Reading("dev", "a", base + 300, 1.0)])
    assert any(r.value == 1.0 for r in out)                   # following → live on bus
    out.clear()
    tc.park(base + 100)                                       # historic window
    assert resets[0] >= 1 and len(out) > 0                    # reset + historic replay
    eng.pub([Reading("dev", "a", base + 300, 99.0)])
    assert not any(r.value == 99.0 for r in out)              # parked blocks live
    out.clear(); r0 = resets[0]
    tc.follow_now()                             # return to live: NO reload (panels hold
    assert resets[0] == r0                      # the data) — just catch up + resume
    eng.pub([Reading("dev", "a", base + 300, 2.0)])
    assert tc.following and any(r.value == 2.0 for r in out)   # live resumes on top
    print("✓ ReplayController: follow→live, park→reset+historic, parked blocks "
          "live, follow→resumes WITHOUT reload")

    # transport (pause/play) must NOT reload — only navigation does
    r1 = resets[0]
    tc.pause();  eng.pub([Reading("dev", "a", base + 300, 3.0)])
    tc.play()
    assert resets[0] == r1, "pause/play must not reload"
    print("✓ ReplayController: pause/play are free (no reload); only nav reloads")

    print("\nREPLAY SELFTEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
