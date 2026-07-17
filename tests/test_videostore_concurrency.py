"""Concurrency regressions around the ambient video store (DESIGN §9.3).

Bug 1: VideoStore.commit (GUI thread), mark_synced (videosync thread, every
~10 s) and import_segment (hub executor / backfill) each ran an unguarded
load→modify→save cycle on index.json — an interleave saved a stale copy and
PERMANENTLY dropped a committed segment from the index. The store now holds one
RLock across every such cycle.

Bug 2: VideoCaptureService.stop() (app exit) finalized segments through a
1500 ms QTimer — which the dying event loop never fired, so the last segment of
every camera (up to 2 min of footage) stayed on disk but unindexed: invisible
forever. stop() now waits boundedly for the recorder's file and commits
synchronously.
"""

import os
import threading
import time

import pytest

from ferrodac.core.videostore import VideoStore, seg_key

CAM = "cam-0"


def _mk_segment_file(store: VideoStore, cam: str, t0: float, size: int = 64) -> str:
    path = store.segment_path(cam, t0)
    with open(path, "wb") as fh:
        fh.write(b"\x00" * size)
    return path


def _keys(store: VideoStore, cam: str) -> list:
    return [seg_key(e["t0"]) for e in store.segments(cam)]


# -- Bug 1: the index lost-update race -----------------------------------------

def test_deterministic_lost_update_window_is_closed(tmp_path, monkeypatch):
    """Widen the load→save window (a sleep injected into _save) and race two
    commits through it. Without the store lock this DETERMINISTICALLY loses one
    entry (both load the same index, each saves only its own append — last
    writer wins); with the lock both must land."""
    st = VideoStore(str(tmp_path / "video"))
    real_save = st._save

    def slow_save(cam_uuid, entries):
        time.sleep(0.05)                        # sit in the race window
        real_save(cam_uuid, entries)

    monkeypatch.setattr(st, "_save", slow_save)

    t0s = (1000.0, 2000.0)
    paths = {t0: _mk_segment_file(st, CAM, t0) for t0 in t0s}
    barrier = threading.Barrier(2)

    def commit(t0):
        barrier.wait()
        assert st.commit(CAM, t0, t0 + 120.0, paths[t0])

    threads = [threading.Thread(target=commit, args=(t0,)) for t0 in t0s]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(_keys(st, CAM)) == sorted(seg_key(t0) for t0 in t0s)


def test_hammer_commit_marksynced_import_from_three_threads(tmp_path):
    """The real 3-thread shape: GUI commit + videosync mark_synced +
    hub/backfill import_segment, all pounding one camera's index. Every
    committed/imported segment must survive, exactly once."""
    st = VideoStore(str(tmp_path / "video"))

    pre = [10_000.0 + i * 120.0 for i in range(8)]     # marker's targets
    for t0 in pre:
        assert st.commit(CAM, t0, t0 + 120.0, _mk_segment_file(st, CAM, t0))

    commits = [100_000.0 + i * 120.0 for i in range(30)]
    imports = [200_000.0 + i * 120.0 for i in range(30)]
    barrier = threading.Barrier(3)
    errors = []

    def guard(fn):
        def run():
            try:
                barrier.wait()
                fn()
            except Exception as exc:               # noqa: BLE001 — surface in-test
                errors.append(exc)
        return run

    def committer():
        for t0 in commits:
            assert st.commit(CAM, t0, t0 + 120.0, _mk_segment_file(st, CAM, t0))

    def marker():
        for i in range(40):
            for t0 in pre:
                st.mark_synced(CAM, t0, synced=bool(i % 2))

    def importer():
        for t0 in imports:
            assert st.import_segment(CAM, t0, t0 + 120.0, b"\x01" * 64) == 64

    threads = [threading.Thread(target=guard(fn))
               for fn in (committer, marker, importer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    keys = _keys(st, CAM)
    expected = sorted(seg_key(t0) for t0 in pre + commits + imports)
    assert len(keys) == len(set(keys)), "a segment was double-indexed"
    assert sorted(keys) == expected, "a committed segment was lost from the index"
    # imports were marked synced through the same racing index — spot-check one
    by_key = {seg_key(e["t0"]): e for e in st.segments(CAM)}
    assert by_key[seg_key(imports[0])]["synced"] is True


def test_concurrent_import_of_the_same_segment_stays_idempotent(tmp_path):
    """Two simultaneous imports of one seg_key (hub push racing local backfill):
    the check-then-write must be atomic — exactly one indexes it, the other
    reports 0 bytes."""
    st = VideoStore(str(tmp_path / "video"))
    t0, data = 5_000.0, b"\x02" * 64
    barrier = threading.Barrier(2)
    results = []

    def do_import():
        barrier.wait()
        results.append(st.import_segment(CAM, t0, t0 + 120.0, data))

    threads = [threading.Thread(target=do_import) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [0, len(data)]
    assert _keys(st, CAM) == [seg_key(t0)]


# -- Bug 2: exit-time segments must be committed synchronously ------------------

class _AsyncFinalizeCam:
    """A camera whose segment file lands only AFTER stop_segment, off-thread —
    QMediaRecorder's async finalization in miniature (cf. _FakeCam in
    test_media.py, which lands the file synchronously at start)."""

    def __init__(self, data_id, land_after=0.2, lands=True):
        self.data_id = self.name = data_id
        self.video_mode, self.video_retention = 2, ""
        self._land_after, self._lands = land_after, lands
        self.path = None

    def start_segment(self, path):
        self.path = path                       # nothing on disk yet
        return True

    def stop_segment(self):
        if not self._lands:
            return                             # encoder produced nothing
        def land(p=self.path, delay=self._land_after):
            time.sleep(delay)
            with open(p, "wb") as fh:
                fh.write(b"\x00" * 2048)
        threading.Thread(target=land, daemon=True).start()


def _service(tmp_path, cams):
    from ferrodac.ui.videocapture import VideoCaptureService
    st = VideoStore(str(tmp_path / "video"))
    svc = VideoCaptureService(st, devices=lambda: cams,
                              is_recording=lambda: False,
                              now=lambda: 1_700_000_000.0)
    return st, svc


@pytest.mark.ui
def test_stop_commits_the_pending_segment_synchronously(tmp_path, qapp):
    """stop() at app exit: the segment whose file finalizes ~0.2 s later must be
    IN the index when stop() returns — no event loop is pumped, so a deferred
    QTimer commit would never run again."""
    cam = _AsyncFinalizeCam("always")
    st, svc = _service(tmp_path, [cam])
    svc.reconcile()
    assert set(svc._active) == {"always"}
    assert st.segments("always") == []         # nothing indexed while open

    start = time.monotonic()
    svc.stop()                                 # returns only once committed
    elapsed = time.monotonic() - start

    assert not svc._active
    entries = st.segments("always")
    assert len(entries) == 1 and entries[0]["size"] == 2048
    assert elapsed < 2.5                       # bounded — file landed at ~0.2 s


@pytest.mark.ui
def test_stop_skips_a_segment_that_never_lands(tmp_path, qapp):
    """A segment whose file genuinely never materializes (broken encoder) is
    skipped at exit within the bounded deadline — logged, never raised, never
    indexed."""
    cam = _AsyncFinalizeCam("always", lands=False)
    st, svc = _service(tmp_path, [cam])
    svc.reconcile()
    assert set(svc._active) == {"always"}

    start = time.monotonic()
    svc.stop()                                 # must not raise
    elapsed = time.monotonic() - start

    assert not svc._active
    assert st.segments("always") == []
    assert elapsed < 4.0                       # ~2 s deadline + slop, never unbounded


@pytest.mark.ui
def test_non_exit_close_still_defers_the_commit(tmp_path, qapp):
    """The NORMAL path (rotation / reconcile) is unchanged: _close without an
    exit deadline schedules the commit behind the grace timer — the index stays
    empty immediately after, and the same deferred commit logic lands it."""
    cam = _AsyncFinalizeCam("always", land_after=0.0)
    st, svc = _service(tmp_path, [cam])
    svc.reconcile()
    seg = dict(svc._active["always"])

    svc._close("always")                       # rotation-style close: NO deadline
    assert st.segments("always") == []         # commit deferred, not synchronous

    time.sleep(0.05)                           # let the fake finalizer land the file
    svc._commit("always", seg, 1_700_000_000.0 + 5.0)   # what the timer runs later
    assert len(st.segments("always")) == 1
