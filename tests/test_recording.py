"""RecordingController — the recording lifecycle, now testable headless (no Qt).

Pins the policy the audit wanted out of the Qt shell: start/stop opens+closes a
REC marker and finalises (exports) the span; a crashed open recording is recovered
with an end time computed from the store's coverage.
"""
from types import SimpleNamespace

from ferrodac.core.markers import RECORDING
from ferrodac.core.recording import RecordingController


class FakeMarkers:
    def __init__(self, existing=()):
        self._m = {m.id: m for m in existing}
        self._n = 0

    def add(self, t, kind, label, comment):
        self._n += 1
        mid = f"m{self._n}"
        self._m[mid] = SimpleNamespace(id=mid, t=t, kind=kind, label=label,
                                       comment=comment, t_end=None, run_dir=None)
        return mid

    def get(self, mid):
        return self._m.get(mid)

    def update(self, mid, **kw):
        for k, v in kw.items():
            setattr(self._m[mid], k, v)

    def all(self):
        return list(self._m.values())


class FakeResolver:
    def __init__(self, cov):
        self._cov = cov

    def coverage(self, key):
        return self._cov.get(key, [])


def _controller(markers, resolver, exports, saved, commits, now=1000.0):
    def run_export(dest, sources, t0, t1, *, flush, exclusive, on_ok, on_fail):
        exports.append(SimpleNamespace(dest=dest, sources=sources, t0=t0, t1=t1,
                                       flush=flush, exclusive=exclusive,
                                       on_ok=on_ok, on_fail=on_fail))
    return RecordingController(
        markers=markers, resolver=resolver, store_writer=None,
        run_export=run_export, runs_dir=lambda: "/runs",
        export_sources=lambda: ["dev/ch"],
        on_saved=lambda mid, dest, n: saved.append((mid, dest, n)),
        commit=lambda msg: commits.append(msg), now=lambda: now)


def test_start_stop_finalises_and_saves():
    markers = FakeMarkers()
    exports, saved, commits = [], [], []
    rc = _controller(markers, FakeResolver({}), exports, saved, commits)

    assert rc.toggle() == "started" and rc.recording
    mid = rc.open_mid
    assert markers.get(mid).kind == RECORDING

    assert rc.toggle() == "stopped" and not rc.recording
    assert markers.get(mid).t_end == 1000.0            # region closed
    assert len(exports) == 1                           # export kicked off
    e = exports[0]
    assert e.dest.startswith("/runs") and "run_" in e.dest and e.flush

    e.on_ok({"sources": ["dev/ch", "dev/ch2"]})        # export finished
    assert markers.get(mid).run_dir == e.dest
    assert saved == [(mid, e.dest, 2)]
    assert commits and commits[0].startswith("Recorded run_")


def test_no_export_without_a_resolver():
    markers = FakeMarkers()
    exports = []
    rc = RecordingController(
        markers=markers, resolver=None, store_writer=None,
        run_export=lambda *a, **k: exports.append(1), runs_dir=lambda: "/runs",
        export_sources=lambda: [], now=lambda: 1.0)
    rc.toggle()                                         # start
    rc.toggle()                                         # stop → finalize is a no-op
    assert exports == []


def test_close_open_marker_without_export():
    markers = FakeMarkers()
    exports = []
    rc = _controller(markers, FakeResolver({}), exports, [], [])
    rc.toggle()
    mid = rc.open_mid
    assert rc.close_open_marker() == mid
    assert markers.get(mid).t_end == 1000.0 and not rc.recording
    assert exports == []                               # shutdown path never exports


def test_last_data_time_from_coverage():
    resolver = FakeResolver({"dev/ch": [(500.0, 900.0), (950.0, 1200.0)]})
    rc = _controller(FakeMarkers(), resolver, [], [], [], now=1000.0)
    # latest sample in [t0, now=1000] = min(1200, 1000)=1000 from the 2nd interval
    assert rc.last_data_time(600.0) == 1000.0
    assert rc.last_data_time(2000.0) is None           # nothing in range


def test_recover_open_recordings():
    open_rec = SimpleNamespace(id="old", t=600.0, kind=RECORDING, t_end=None)
    markers = FakeMarkers([open_rec])
    resolver = FakeResolver({"dev/ch": [(600.0, 880.0)]})
    exports, saved, commits = [], [], []
    rc = _controller(markers, resolver, exports, saved, commits, now=1000.0)

    assert rc.recover_open() == 1
    assert markers.get("old").t_end == 880.0           # end = last stored sample
    assert len(exports) == 1 and exports[0].t0 == 600.0 and exports[0].t1 == 880.0
