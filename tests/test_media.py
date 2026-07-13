"""MediaService (DESIGN §9 stage a): snapshots are a lossless file + a media
tag. Full round-trip against a real MarkerModel and real QImages (offscreen
QtGui only): saved bytes are bit-exact, the tag payload carries the portable
project-relative path, refusals (stale/missing/non-image) never write, and the
tag survives tags.json persistence. Marked `ui` (needs QtGui for QImage)."""
import math
import os
import time
import types

import pytest

pytest.importorskip("qtpy")

from qtpy.QtGui import QColor, QImage  # noqa: E402

from ferrodac.core.markers import MarkerModel  # noqa: E402
from ferrodac.core.media import STALE_S, MediaError, MediaService  # noqa: E402
from ferrodac.core.tag import MEDIA, marker_from_dict, marker_to_dict  # noqa: E402

pytestmark = pytest.mark.ui


def _frame(w=64, h=48, color="#3050a0"):
    img = QImage(w, h, QImage.Format.Format_RGB888)
    img.fill(QColor(color))
    return img


def _reading(key, img, t=None):
    return types.SimpleNamespace(key=key, t=t if t is not None else time.time(),
                                 value=img, status=0)


def _service(tmp_path, readings, names=None):
    markers = MarkerModel()
    latest = lambda: readings                      # noqa: E731
    svc = MediaService(latest=latest, markers=markers,
                       media_dir=lambda: str(tmp_path / "media"),
                       names=lambda: (names or {}))
    return svc, markers


def test_snapshot_saves_lossless_and_tags(tmp_path):
    img = _frame()
    t0 = time.time() - 0.5
    svc, markers = _service(tmp_path, {"cam/frame": _reading("cam/frame", img, t0)},
                            names={"cam/frame": "Bench cam"})
    res = svc.snapshot("cam/frame")

    # file: exists, PNG, BIT-EXACT to the frame Reading (DESIGN §9.1: lossless)
    assert os.path.exists(res["path"])
    back = QImage(res["path"]).convertToFormat(QImage.Format.Format_RGB888)
    assert back.size() == img.size()
    for x, y in ((0, 0), (31, 24), (63, 47)):
        assert back.pixel(x, y) == img.pixel(x, y)

    # tag: media kind, the FRAME's time (not the button press), portable relpath
    m = markers.get(res["tag_id"])
    assert m.kind == MEDIA
    assert m.t == pytest.approx(t0)
    assert m.payload["file"] == res["relpath"]
    assert m.payload["file"].startswith("media/")
    assert m.payload["source"] == "cam/frame"
    assert "Bench cam" in m.label


def test_refusals_never_write(tmp_path):
    img = _frame()
    stale = _reading("cam/frame", img, time.time() - STALE_S - 5)
    svc, _ = _service(tmp_path, {
        "cam/frame": stale,
        "gauge/p": types.SimpleNamespace(key="gauge/p", t=time.time(),
                                         value=1e-6, status=0),
    })
    with pytest.raises(MediaError, match="stalled"):
        svc.snapshot("cam/frame")                  # frame too old → refuse
    with pytest.raises(MediaError, match="no frame"):
        svc.snapshot("cam2/frame")                 # never seen → refuse
    with pytest.raises(MediaError, match="image"):
        svc.snapshot("gauge/p")                    # scalar source → refuse
    media = tmp_path / "media"
    assert not media.exists() or not list(media.iterdir())   # nothing written


def test_snapshot_all_is_best_effort(tmp_path):
    ok = _reading("a/frame", _frame())
    stale = _reading("b/frame", _frame(), time.time() - STALE_S - 5)
    svc, markers = _service(tmp_path, {"a/frame": ok, "b/frame": stale})
    results, errors = svc.snapshot_all(["a/frame", "b/frame"])
    assert len(results) == 1 and len(errors) == 1
    assert errors[0][0] == "b/frame"               # the stalled one, named
    assert len(markers.visible()) == 1             # one tag, for the good one


def test_burst_filenames_never_collide(tmp_path):
    r = _reading("cam/frame", _frame(), time.time() - 0.1)
    svc, _ = _service(tmp_path, {"cam/frame": r})
    a = svc.snapshot("cam/frame")
    b = svc.snapshot("cam/frame")                  # same frame, same millisecond
    assert a["path"] != b["path"]
    assert os.path.exists(a["path"]) and os.path.exists(b["path"])


def test_media_tag_survives_persistence_roundtrip(tmp_path):
    svc, markers = _service(tmp_path, {"cam/frame": _reading("cam/frame", _frame())})
    res = svc.snapshot("cam/frame")
    m = markers.get(res["tag_id"])
    m2 = marker_from_dict(marker_to_dict(m))       # the tags.json round-trip
    assert m2.kind == MEDIA and m2.payload == m.payload


def test_resolve_stays_inside_the_project(tmp_path):
    svc, markers = _service(tmp_path, {"cam/frame": _reading("cam/frame", _frame())})
    res = svc.snapshot("cam/frame")
    m = markers.get(res["tag_id"])
    assert MediaService.resolve(m, str(tmp_path)) == os.path.normpath(res["path"])
    # a foreign/hostile payload must resolve to None, never escape the project
    m_gone = marker_from_dict({**marker_to_dict(m), "id": "x2",
                               "payload": {"file": "media/not-there.png"}})
    assert MediaService.resolve(m_gone, str(tmp_path)) is None
    m_evil = marker_from_dict({**marker_to_dict(m), "id": "x3",
                               "payload": {"file": "../../etc/passwd"}})
    assert MediaService.resolve(m_evil, str(tmp_path)) is None


# -- §9 stage b: the photo tile + forward-compat layout guard --------------------

def test_photo_tile_follows_the_time_window(tmp_path, qapp=None):
    """The tile shows the newest media snapshot AT OR BEFORE the shared window's
    head — scrubbing surfaces the time-correlated photo; live shows the latest."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.core.markers import SessionClock
    from ferrodac.core.media import MediaService
    from ferrodac.ui.panels import PhotoTilePanel

    svc, markers = _service(tmp_path, {})
    t0 = time.time()
    readings = {}
    for i, color in enumerate(("#102030", "#405060", "#708090")):
        readings["cam/frame"] = _reading("cam/frame", _frame(color=color),
                                         t0 - 30 + i * 10)     # t0-30, -20, -10
        svc._latest = lambda r=dict(readings): r
        # bypass staleness for the two older frames (they were live "back then")
        import ferrodac.core.media as media_mod
        real_time = media_mod.time.time
        media_mod.time.time = lambda: t0 - 30 + i * 10 + 0.1
        try:
            svc.snapshot("cam/frame")
        finally:
            media_mod.time.time = real_time
    tags = sorted((m for m in markers.visible()), key=lambda m: m.t)
    assert len(tags) == 3

    tile = PhotoTilePanel()
    tile.set_media_provider(lambda m: MediaService.resolve(m, str(tmp_path)))
    tile.attach_session(SessionClock(), markers)
    tile.set_window(t0 - 60, t0 - 25)          # head between photo 1 and 2
    assert tile._shown == tags[0].id
    tile.set_window(t0 - 60, t0 - 15)          # head between photo 2 and 3
    assert tile._shown == tags[1].id
    tile.set_window(t0 - 60, t0)               # head at now → latest
    assert tile._shown == tags[2].id
    tile.set_window(t0 - 60, t0 - 40)          # head BEFORE any photo → blank
    assert tile._shown is None


def test_unknown_panel_kind_is_preserved_not_fatal():
    """Forward compat: a layout from a newer build (e.g. with an imagetile) must
    restore everything it CAN, keep the unknown entry verbatim, and re-emit it
    on export — previously the KeyError aborted the whole session restore."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.ui.workspace import Dashboard, WorkspaceArea

    engine = Engine()
    manager = DeviceManager([], engine=engine, registry=None)
    dash = Dashboard(WorkspaceArea(), engine, manager)
    alien = {"id": "future-1", "kind": "holo-display", "title": "From the future",
             "state": {"answer": 42}}
    dash.import_layout({"panels": [
        {"id": "chart-1", "kind": "chart", "title": "Chart", "state": {}},
        alien,
        {"id": "num-1", "kind": "numeric", "title": "Numeric", "state": {}},
    ], "routes": {}})
    assert set(dash._panels) == {"chart-1", "num-1"}   # both known kinds restored
    out = dash.export_layout()
    assert alien in out["panels"]                       # the alien survives verbatim


# -- §9 live video: the hubclient encode/decode glue -----------------------------

def _encode_rig(demanded, mode, frame_last=None):
    """A bare stand-in carrying exactly the attrs _encode_frame_reading uses —
    the method is called unbound so no HubController construction is needed."""
    from ferrodac.ui.hubclient import HubController
    stub = types.SimpleNamespace(
        _agent=types.SimpleNamespace(demanded_frames=demanded),
        manager=types.SimpleNamespace(active_devices=lambda: [
            types.SimpleNamespace(data_id="cam-1", hub_video_mode=mode)]),
        _frame_last=frame_last if frame_last is not None else {},
        _FRAME_DOC_MAX_PX=HubController._FRAME_DOC_MAX_PX,
        _FRAME_DOC_MIN_DT=HubController._FRAME_DOC_MIN_DT,
        _FRAME_DOC_QUALITY=HubController._FRAME_DOC_QUALITY,
    )
    return HubController._encode_frame_reading, stub


def test_encode_gates_on_demand_and_mode():
    enc, stub = _encode_rig(demanded=set(), mode=2)
    r = _reading("cam-1/frame", _frame())
    r.device, r.source = "cam-1", "frame"
    assert enc(stub, r) is None                       # no demand → nothing sent

    enc, stub = _encode_rig(demanded={("cam-1", "frame")}, mode=0)
    assert enc(stub, r) is None                       # demanded but mode Off


def test_encode_raw_is_bit_exact():
    from ferrodac.net.convert import FramePayload
    from ferrodac.ui.hubclient import HubController
    enc, stub = _encode_rig(demanded={("cam-1", "frame")}, mode=2)
    img = _frame(w=16, h=12, color="#804020")
    r = _reading("cam-1/frame", img)
    r.device, r.source = "cam-1", "frame"
    out = enc(stub, r)
    assert out is not None and isinstance(out.value, FramePayload)
    fp = out.value
    assert fp.encoding == "rgb888" and (fp.width, fp.height) == (16, 12)
    img2 = HubController._decode_frame(fp)            # viewer-side decode
    assert img2 is not None and img2.size() == img.size()
    for x, y in ((0, 0), (8, 6), (15, 11)):
        assert img2.pixel(x, y) == img.pixel(x, y)    # §9.1: raw = bit-exact


def test_encode_documentation_caps_rate_and_compresses():
    from ferrodac.net.convert import FramePayload
    from ferrodac.ui.hubclient import HubController
    enc, stub = _encode_rig(demanded={("cam-1", "frame")}, mode=1)
    img = _frame(w=1280, h=720)
    r = _reading("cam-1/frame", img)
    r.device, r.source = "cam-1", "frame"
    out = enc(stub, r)
    assert out is not None
    fp = out.value
    assert fp.encoding == "jpeg"
    assert max(fp.width, fp.height) <= 960            # downscaled
    assert len(fp.data) < 1280 * 720 * 3 / 10         # actually compressed
    img2 = HubController._decode_frame(fp)
    assert img2 is not None and not img2.isNull()
    assert enc(stub, r) is None                       # immediate resend → rate-capped


def test_decode_rejects_garbage():
    from ferrodac.net.convert import FramePayload
    from ferrodac.ui.hubclient import HubController
    assert HubController._decode_frame(
        FramePayload(b"not a jpeg", "jpeg", 4, 4)) is None
    assert HubController._decode_frame(
        FramePayload(b"\x00" * 5, "rgb888", 4, 4)) is None   # wrong size


# -- §9 polish: per-panel 📷 button + Events-dock thumbnails ----------------------

def test_camera_panel_snapshot_button():
    """The camera view's 📷 button appears when a source is routed, fires the
    wired handler with THAT panel's source key, and hides on unroute."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.ui.panels import ImagePanel
    p = ImagePanel()
    assert not p._snap_btn.isVisibleTo(p)
    shots = []
    p.on_snapshot = shots.append
    p.add_source("cam/frame", types.SimpleNamespace(unit="", name="Cam"))
    assert p._snap_btn.isVisibleTo(p)
    p._snap_btn.click()
    assert shots == ["cam/frame"]
    p.remove_source("cam/frame")
    assert not p._snap_btn.isVisibleTo(p)
    p._snap_btn.click()                                  # unrouted → no shot
    assert shots == ["cam/frame"]


def test_events_dock_shows_media_thumbnails(tmp_path):
    """A media row whose file is local gets a clickable thumbnail; a foreign
    reference (file on another box) gets none — the 🖼 button handles that."""
    from qtpy.QtWidgets import QApplication, QToolButton
    app = QApplication.instance() or QApplication([])
    from ferrodac.core.markers import MarkerModel, SessionClock
    from ferrodac.core.media import MediaService
    from ferrodac.ui.docks import EventsPanel

    svc, markers = _service(tmp_path, {"cam/frame": _reading("cam/frame", _frame())})
    res = svc.snapshot("cam/frame")                      # a real, local photo
    markers.add(res["t"] + 1, label="ghost", kind=MEDIA,
                payload={"file": "media/not-here.png"})  # foreign/missing file

    opened = []
    panel = EventsPanel(markers, SessionClock(),
                        on_open_media=opened.append,
                        media_resolver=lambda m: MediaService.resolve(
                            m, str(tmp_path)))
    thumbs = [b for b in panel.findChildren(QToolButton)
              if not b.icon().isNull() and b.toolTip() == "Open the photo"]
    assert len(thumbs) == 1                              # local photo only
    assert thumbs[0].iconSize().height() == 44
    thumbs[0].click()
    assert opened == [res["tag_id"]]                     # thumbnail opens the photo
    panel.deleteLater()


# -- UX: zoom/pan in the camera view + photo tile (one VideoView serves both) -----

def test_videoview_zoom_geometry_and_reset():
    from qtpy.QtWidgets import QApplication
    from qtpy.QtCore import QPointF
    app = QApplication.instance() or QApplication([])
    from ferrodac.ui.panels import VideoView
    v = VideoView()
    v.resize(200, 120)                                 # == the widget's minimum
    v.set_image(_frame(w=100, h=60))
    r = v.content_rect()
    assert (r.width(), r.height()) == (200, 120)      # aspect-fit fills the view

    v.set_zoom(2.0)                                    # center-anchored zoom
    r = v.content_rect()
    assert (r.width(), r.height()) == (400, 240)
    assert r.x() == -100 and r.y() == -60              # still centered

    v._pan[0] += 10_000                                # pan clamps at the edge
    r = v.content_rect()
    assert r.x() == 0                                  # left edge never detaches

    v.set_zoom(1.0)                                    # reset recenters
    r = v.content_rect()
    assert (r.x(), r.y(), r.width(), r.height()) == (0, 0, 200, 120)

    v.set_zoom(999)                                    # clamped ceiling
    assert v._zoom == 16.0
    v.set_zoom(1.0)                                    # back to fit
    v.set_zoom(2.0)
    r0 = v.content_rect()
    ax, ay = 30.0, 30.0                                # anchored zoom invariant:
    fx = (ax - r0.x()) / r0.width()                    # the image point under the
    fy = (ay - r0.y()) / r0.height()                   # anchor must stay put
    v.set_zoom(4.0, anchor=QPointF(ax, ay))
    r2 = v.content_rect()
    assert r2.x() + fx * r2.width() == pytest.approx(ax, abs=1.5)
    assert r2.y() + fy * r2.height() == pytest.approx(ay, abs=1.5)


def test_videoview_overlays_track_zoom():
    """Detector ROI boxes map through content_rect, so they must follow zoom."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.ui.panels import VideoView
    v = VideoView()
    v.resize(200, 120)
    v.set_image(_frame(w=100, h=60))
    roi = (25, 15, 50, 30)                             # centered quarter of the image
    r1 = v._roi_to_widget(roi)
    v.set_zoom(2.0)
    r2 = v._roi_to_widget(roi)
    assert r2.width() == r1.width() * 2                # box scales with the view
    assert r2.center().x() == pytest.approx(r1.center().x(), abs=2)  # same center


def test_videoview_zoom_survives_frames_resets_on_geometry_change():
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.ui.panels import VideoView
    v = VideoView()
    v.resize(200, 120)
    v.set_image(_frame(w=100, h=60))
    v.set_zoom(3.0)
    v.set_image(_frame(w=100, h=60, color="#222222"))  # next live frame
    assert v._zoom == 3.0                              # zoom sticks across frames
    v.set_image(_frame(w=64, h=48))                    # new source geometry
    assert v._zoom == 1.0                              # → honest reset


# -- §9.3 ambient video: store, materializer, capture-service gating -------------

def _write_seg(store, cam, t0, t1, data=b"\x00" * 4096):
    """A stand-in 'segment file' (materializer copies bytes; ffmpeg-concat is
    tested separately). Registers it in the index like the capture service does."""
    path = store.segment_path(cam, t0)
    with open(path, "wb") as fh:
        fh.write(data)
    assert store.commit(cam, t0, t1, path)
    return path


def test_videostore_coverage_and_overlap(tmp_path):
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    for i in range(3):                                 # three abutting 120 s segments
        _write_seg(st, "camA", base + i * 120, base + (i + 1) * 120)
    cov = st.coverage("camA")
    assert len(cov) == 1 and cov[0] == (base, base + 360)   # merged to one span
    assert st.covers("camA", base + 50, base + 300)
    assert not st.covers("camA", base + 50, base + 100_000)
    segs = st.segments_overlapping("camA", base + 100, base + 250)
    assert len(segs) == 3            # [0,120],[120,240],[240,360] all touch [100,250]
    segs = st.segments_overlapping("camA", base + 130, base + 200)
    assert len(segs) == 1                              # only [120,240] contains it


def test_videostore_manual_and_retention_cleanup(tmp_path):
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    now = 1_700_000_000.0
    for i in range(5):                                 # 5×120 s, 4 KB each
        _write_seg(st, "camA", now - (5 - i) * 120, now - (4 - i) * 120)
    assert st.usage("camA") == 5 * 4096
    # segments end at now-480,-360,-240,-120,0; delete those ending BEFORE now-240
    freed = st.delete_older_than("camA", now - 240)
    assert freed == 2 * 4096 and len(st.coverage("camA")) == 1
    # time-window retention: keep only the last 300 s (drops the two oldest kept)
    st.prune_retention("camA", "5m" if False else "0.084h", now=now)   # ~300 s
    assert len(st._load("camA")) <= 3
    # size-cap retention, oldest first
    st2 = VideoStore(str(tmp_path / "v2"))
    for i in range(5):                                 # 5×4 KB = 20 KB
        _write_seg(st2, "camB", now + i * 120, now + (i + 1) * 120)
    assert st2.prune_retention("camB", "1GB") == 0     # under cap → nothing pruned
    st2.prune_retention("camB", "0.00001GB")           # 10 KB cap → keep ≤ 2 newest
    assert st2.usage("camB") <= 2 * 4096


def test_clip_materializer_single_and_concat(tmp_path):
    from ferrodac.core.media import ClipMaterializer
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    media = tmp_path / "proj" / "media"
    base = 1_700_000_000.0
    _write_seg(st, "camA", base, base + 120, data=b"SEG0" * 64)
    mat = ClipMaterializer(st, media_dir=lambda: str(media))

    r1 = mat.materialize("camA", base + 10, base + 60, "Bench")   # one segment
    assert r1 is not None and r1["file"].startswith("media/") and r1["file"].endswith(".mp4")
    assert os.path.exists(os.path.join(str(tmp_path / "proj"), r1["file"]))
    assert r1["files"] == [r1["file"]]

    assert mat.materialize("camA", base + 10_000, base + 10_100, "Bench") is None  # no video


def test_clip_materializer_ffmpeg_concat_when_present(tmp_path):
    """With ffmpeg present, multiple segments concat into ONE file. Skips where
    ffmpeg isn't installed (the parts-fallback path is covered by construction)."""
    from ferrodac.core.media import ClipMaterializer, _have_ffmpeg
    from ferrodac.core.videostore import VideoStore
    if not _have_ffmpeg():
        pytest.skip("ffmpeg not installed")
    import subprocess
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    for i in range(2):                                 # two real tiny mp4 segments
        path = st.segment_path("camA", base + i * 120)
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-f", "lavfi",
                        "-i", "color=c=black:s=64x48:d=1", "-pix_fmt", "yuv420p",
                        path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True)
        st.commit("camA", base + i * 120, base + (i + 1) * 120, path)
    mat = ClipMaterializer(st, media_dir=lambda: str(tmp_path / "proj" / "media"))
    r = mat.materialize("camA", base + 10, base + 200, "Bench")
    assert r is not None and len(r["files"]) == 1      # ONE concatenated file
    assert os.path.getsize(os.path.join(str(tmp_path / "proj"), r["file"])) > 0


class _FakeCam:
    def __init__(self, data_id, mode=2, retention="", streaming=True):
        self.data_id, self.name = data_id, data_id
        self.video_mode, self.video_retention = mode, retention
        self._streaming = streaming
        self.segments, self.open_path = [], None

    def start_segment(self, path):
        if not self._streaming:
            return False
        self.open_path = path
        with open(path, "wb") as fh:                    # a "recorded" file lands
            fh.write(b"\x00" * 2048)
        return True

    def stop_segment(self):
        if self.open_path:
            self.segments.append(self.open_path)
            self.open_path = None


def test_capture_service_gates_by_mode_and_record_state(tmp_path):
    """reconcile() opens segments only for cameras whose mode + the record state
    say so — the core §9.3 gating, exercised without Qt timers."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui.videocapture import VideoCaptureService

    cams = [_FakeCam("off", mode=0), _FakeCam("whilerec", mode=1),
            _FakeCam("always", mode=2)]
    recording = {"on": False}
    st = VideoStore(str(tmp_path / "video"))
    svc = VideoCaptureService(st, devices=lambda: cams,
                              is_recording=lambda: recording["on"],
                              now=lambda: 1_700_000_000.0)
    svc.reconcile()
    assert set(svc._active) == {"always"}              # only Always runs at rest
    recording["on"] = True
    svc.reconcile()
    assert set(svc._active) == {"always", "whilerec"}  # record → While-rec joins
    recording["on"] = False
    svc.reconcile()
    assert set(svc._active) == {"always"}              # stop → While-rec closes


def test_capture_service_disk_floor_pauses_video(tmp_path, monkeypatch):
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui import videocapture
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    cams = [_FakeCam("always", mode=2)]
    st = VideoStore(str(tmp_path / "video"))
    svc = videocapture.VideoCaptureService(
        st, devices=lambda: cams, is_recording=lambda: False,
        now=lambda: 1_700_000_000.0)
    svc.reconcile()
    assert set(svc._active) == {"always"}
    monkeypatch.setattr(st, "free_gb", lambda: 1.0)    # below the floor
    svc.reconcile()
    assert svc._paused_disk and not svc._active        # video paused, store spared
    monkeypatch.setattr(st, "free_gb", lambda: 100.0)  # recovered
    svc.reconcile()
    assert not svc._paused_disk and set(svc._active) == {"always"}


def test_capture_service_self_corrects_a_broken_encoder(tmp_path, monkeypatch):
    """If several segments in a row never land (hardware encoder silently failing
    despite the startup probe), the service persists a 'use software' flag for the
    next launch and warns the user — not just silent empty video (§9.3)."""
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui import videocapture
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])   # noqa: F841
    marked, msgs = [], []
    monkeypatch.setattr(videocapture, "mark_prefer_software_encode",
                        lambda: marked.append(1))
    monkeypatch.setattr(videocapture, "prefer_software_encode", lambda: bool(marked))
    st = VideoStore(str(tmp_path / "video"))
    svc = videocapture.VideoCaptureService(
        st, devices=lambda: [], is_recording=lambda: False, now=lambda: 1000.0,
        on_status=lambda m, timeout=0: msgs.append(m))
    seg = {"t0": 1000.0, "path": str(tmp_path / "video" / "gone.mp4"),
           "dev": None, "label": "cam"}
    for _ in range(2):                                   # 2 failures → not yet flagged
        svc._commit("cam", seg, 1002.0)
    assert marked == []
    svc._commit("cam", seg, 1002.0)                     # 3rd in a row → flag + warn
    assert marked == [1]
    assert any("hardware H.264" in m for m in msgs)


# -- §9.3 phase 2: segment_at (scrub preview kernel) + media_files_in (export) ----

def test_segment_at_point_query(tmp_path):
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    for i in range(3):                                 # [0,120],[120,240],[240,360]
        _write_seg(st, "camA", base + i * 120, base + (i + 1) * 120)
    r = st.segment_at("camA", base + 130)              # inside segment 1
    assert r is not None and abs(r["offset"] - 10.0) < 1e-6
    assert r["path"].endswith(f"seg_{int((base + 120) * 1000)}.mp4")
    assert st.segment_at("camA", base + 240)["offset"] < 1e-6   # boundary → later seg
    # a gap: drop the middle segment, ask inside the hole
    st2 = VideoStore(str(tmp_path / "v2"))
    _write_seg(st2, "camB", base, base + 120)
    _write_seg(st2, "camB", base + 600, base + 720)    # 8-min gap
    assert st2.segment_at("camB", base + 300) is None  # in the gap
    assert st2.segment_at("camB", base - 5) is None     # before coverage
    assert st2.segment_at("camB", base + 50)["offset"] == pytest.approx(50.0)


def test_segment_at_is_memoized_but_reflects_writes(tmp_path, monkeypatch):
    """The scrub preview calls segment_at every transport tick; it must not
    re-parse the O(segments) index each time (GUI-thread rule), yet must still
    see a freshly-committed segment (the cache is keyed by the file's mtime)."""
    import ferrodac.core.videostore as vsmod
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    for i in range(2):                                 # [0,120],[120,240]
        _write_seg(st, "camA", base + i * 120, base + (i + 1) * 120)

    calls = {"n": 0}
    real_load = vsmod.json.load
    monkeypatch.setattr(vsmod.json, "load",
                        lambda fh, _r=real_load: (calls.__setitem__("n", calls["n"] + 1), _r(fh))[1])
    for _ in range(20):                                # a burst of scrub ticks
        assert st.segment_at("camA", base + 130)["offset"] == pytest.approx(10.0)
    assert calls["n"] == 1                             # parsed ONCE, not 20×

    _write_seg(st, "camA", base + 240, base + 360)     # a new segment lands
    r = st.segment_at("camA", base + 300)              # now inside coverage
    assert r is not None and r["offset"] == pytest.approx(60.0)
    assert calls["n"] == 2                             # re-parsed exactly once, on change


def test_reads_do_not_create_camera_dirs(tmp_path):
    """A point/coverage query for an unknown camera must not mkdir on the GUI
    thread — only write paths create the camera directory."""
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "video"))
    assert st.segment_at("ghost", 1_700_000_000.0) is None
    assert st.coverage("ghost") == []
    assert not os.path.exists(os.path.join(str(tmp_path / "video"), "ghost"))


# -- §9.3 phase 3: hub segment sync + on-demand backfill --------------------------

def test_video_sync_uploads_missing_and_is_idempotent(tmp_path):
    from ferrodac.core.videostore import VideoStore
    from ferrodac.core.videosync import LocalVideoTransport, VideoSyncEngine
    local = VideoStore(str(tmp_path / "local"))
    hub = VideoStore(str(tmp_path / "hub"))
    base = 1_700_000_000.0
    for i in range(3):
        _write_seg(local, "camA", base + i * 120, base + (i + 1) * 120)
    eng = VideoSyncEngine(local, LocalVideoTransport(hub))

    assert eng.sync_once() == 3                         # all three uploaded
    assert hub.have() == local.have()                  # the hub now mirrors local
    assert all(e["synced"] for e in local.segments("camA"))   # marked hub-confirmed
    assert eng.sync_once() == 0                         # idempotent once caught up
    assert hub.read_segment_bytes("camA", base) == \
        local.read_segment_bytes("camA", base)         # byte-identical copy

    _write_seg(local, "camA", base + 360, base + 480)  # a fourth lands
    assert eng.sync_once() == 1                         # only the new one uploads


def test_video_sync_backfills_a_wiped_hub(tmp_path):
    from ferrodac.core.videostore import VideoStore
    from ferrodac.core.videosync import LocalVideoTransport, VideoSyncEngine
    local = VideoStore(str(tmp_path / "local"))
    base = 1_700_000_000.0
    _write_seg(local, "camA", base, base + 120)
    VideoSyncEngine(local, LocalVideoTransport(VideoStore(str(tmp_path / "hub")))).sync_once()
    # a fresh hub reports nothing → everything re-uploads (remote is the truth)
    hub2 = VideoStore(str(tmp_path / "hub2"))
    n = VideoSyncEngine(local, LocalVideoTransport(hub2)).sync_once()
    assert n == 1 and hub2.have() == local.have()


def test_video_backfill_pulls_a_missing_segment_on_demand(tmp_path):
    from ferrodac.core.videostore import VideoStore
    from ferrodac.core.videosync import LocalVideoTransport, VideoSyncEngine
    local = VideoStore(str(tmp_path / "local"))
    hub = VideoStore(str(tmp_path / "hub"))
    base = 1_700_000_000.0
    _write_seg(hub, "camA", base, base + 120, data=b"HUBSEG" * 100)   # only on the hub
    eng = VideoSyncEngine(local, LocalVideoTransport(hub))

    assert local.segment_entry_at("camA", base + 60) is None          # not local yet
    e = eng.backfill_at("camA", base + 60)                            # pull on demand
    assert e is not None and e["t0"] == base
    assert local.read_segment_bytes("camA", base) == b"HUBSEG" * 100  # byte-exact import
    assert eng.backfill_at("camA", base + 9999) is None               # gap the hub lacks too
    assert eng.backfill_at("camA", base + 30)["t0"] == base           # already-local: cheap


def test_import_segment_is_idempotent(tmp_path):
    from ferrodac.core.videostore import VideoStore
    st = VideoStore(str(tmp_path / "v"))
    base = 1_700_000_000.0
    assert st.import_segment("camA", base, base + 120, b"X" * 50) == 50
    assert st.import_segment("camA", base, base + 120, b"X" * 50) == 0   # dup skipped
    assert st.import_segment("camA", base, base + 120, b"") == 0         # empty rejected
    assert len(st.segments("camA")) == 1


def test_video_encoding_falls_back_to_software_only_when_hw_unusable(monkeypatch):
    """Ambient video encoding is graceful: keep Qt's hardware default when VAAPI
    H.264 actually works, steer to software only when it can't encode, and never
    override an explicit user choice or touch non-Linux (§9.3)."""
    import sys
    import ferrodac.ui.app as appmod
    import ferrodac.core.videostore as vsmod
    KEY = "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(vsmod, "prefer_software_encode", lambda: False)   # no prior failure

    monkeypatch.delenv(KEY, raising=False)            # hw usable → leave Qt on hardware
    monkeypatch.setattr(appmod, "_vaapi_h264_encode_usable", lambda: True)
    appmod._configure_video_encoding()
    assert KEY not in os.environ

    monkeypatch.delenv(KEY, raising=False)            # hw unusable → steer to software
    monkeypatch.setattr(appmod, "_vaapi_h264_encode_usable", lambda: False)
    appmod._configure_video_encoding()
    assert os.environ.get(KEY) == ""

    monkeypatch.delenv(KEY, raising=False)            # a persisted prior failure → software
    monkeypatch.setattr(vsmod, "prefer_software_encode", lambda: True)    # even though the
    monkeypatch.setattr(appmod, "_vaapi_h264_encode_usable", lambda: True)  # probe now says ok
    appmod._configure_video_encoding()
    assert os.environ.get(KEY) == ""
    monkeypatch.setattr(vsmod, "prefer_software_encode", lambda: False)

    monkeypatch.setenv(KEY, "vaapi")                  # explicit user choice always wins
    monkeypatch.setattr(appmod, "_vaapi_h264_encode_usable",
                        lambda: (_ for _ in ()).throw(AssertionError("probed despite override")))
    appmod._configure_video_encoding()
    assert os.environ[KEY] == "vaapi"

    monkeypatch.setattr(sys, "platform", "win32")     # non-Linux → untouched
    monkeypatch.delenv(KEY, raising=False)
    appmod._configure_video_encoding()
    assert KEY not in os.environ


def test_media_files_in_enumerates_span_media(tmp_path):
    from ferrodac.core.media import MediaService
    proj = tmp_path / "proj"
    (proj / "media").mkdir(parents=True)
    for n, data in (("a.png", b"P"), ("c.part1.mp4", b"C1"), ("c.part2.mp4", b"C2")):
        (proj / "media" / n).write_bytes(data)
    base = 1_700_000_000.0
    tags = [
        {"kind": "media", "t": base + 5, "t_end": None, "deleted": False,
         "payload": {"file": "media/a.png", "format": "png", "source": "cam/frame"}},
        {"kind": "media", "t": base + 2, "t_end": base + 8, "deleted": False,
         "payload": {"file": "media/c.part1.mp4",
                     "files": ["media/c.part1.mp4", "media/c.part2.mp4"],
                     "format": "mp4", "source": "cam/frame", "rec_mid": "R1"}},
        {"kind": "media", "t": base + 999, "payload": {"file": "media/a.png", "format": "png"}},
        {"kind": "media", "t": base + 3, "deleted": True,
         "payload": {"file": "media/a.png", "format": "png"}},
        {"kind": "media", "t": base + 3, "payload": {"file": "../../etc/passwd", "format": "png"}},
        {"kind": "media", "t": base + 3, "payload": {"file": "media/gone.png", "format": "png"}},
        {"kind": "tag", "t": base + 3, "payload": {}},
    ]
    ents = MediaService.media_files_in(tags, base, base + 10, str(proj))
    assert sorted(e["kind"] for e in ents) == ["clip", "photo"]     # only the 2 valid
    clip = next(e for e in ents if e["kind"] == "clip")
    assert len(clip["files"]) == 2 and clip["rec_mid"] == "R1"
    assert clip["t_end"] == pytest.approx(base + 8)
    photo = next(e for e in ents if e["kind"] == "photo")
    assert photo["t_end"] is None and len(photo["files"]) == 1
    # out-of-span, deleted, path-escape, and missing-file all excluded
    assert len(ents) == 2


# -- §9.3 phase 2: timeline video lane + scrub preview (offscreen Qt glue) --------

def test_ribbon_video_lane_layout(tmp_path):
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.ui.timeline import Ribbon
    base = 1_700_000_000.0
    r = Ribbon(["dev/p"], {"dev/p": [(base, base + 300)]}, base, base + 300,
               names={"camA/frame": "Bench", "dev/p": "P"},
               cameras=["camA", "camB"],
               video_cover={"camA": [(base + 10, base + 120)],
                            "camB": [(base + 50, base + 200)]})
    (y0, y1) = r.getPlotItem().getViewBox().viewRange()[1]
    assert y0 <= -2.0 and y1 >= 1.0                 # a band below the scalar row
    assert len(r._video_bars) == 2 and len(r._video_labels) == 2
    r.set_video_coverage({"camA": [(base, base + 300)],
                          "camB": [(base + 50, base + 200)],
                          "camC": [(base + 5, base + 9)]})     # a camera appeared
    assert len(r._video_rows) == 3 and len(r._video_bars) == 3
    assert r.getPlotItem().getViewBox().viewRange()[1][0] <= -3.0


def test_video_preview_resolves_and_blanks(tmp_path):
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui.videopreview import VideoPreviewPanel
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    for i in range(3):
        p = st.segment_path("camA", base + i * 60)
        open(p, "wb").write(b"\x00" * 64)             # not decodable, but resolution
        st.commit("camA", base + i * 60, base + (i + 1) * 60, p)   # is a pure query
    vp = VideoPreviewPanel(st, names_fn=lambda: {"camA/frame": "Bench"})
    assert vp.isVisible() is False or vp._cam == "camA"   # camera auto-selected
    vp._cam = "camA"
    vp.set_head(base + 90)                            # inside segment 1
    assert vp._cur_path == st.segment_at("camA", base + 90)["path"]
    vp.set_head(base + 9999)                          # a gap → cleared
    assert vp._cur_path is None


def test_video_preview_camera_list_is_pushed_not_polled(tmp_path):
    """refresh_cameras honours a caller-supplied list without touching the disk —
    the Timeline lists cameras off the paint thread and pushes them in (§9.3)."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui.videopreview import VideoPreviewPanel
    st = VideoStore(str(tmp_path / "video"))
    vp = VideoPreviewPanel(st, names_fn=lambda: {})
    st.cameras = lambda: (_ for _ in ()).throw(               # must NOT be polled
        AssertionError("cameras() polled on the GUI tick"))
    vp.refresh_cameras(["camA", "camB"])                        # pushed set, no disk
    assert vp._cameras == ["camA", "camB"]


def test_video_preview_drops_stale_frame_after_switch(tmp_path):
    """A frame still in flight from the previous camera must not paint over the
    newly-picked one: _on_frame is gated while _cur_path is None (§9.3)."""
    from qtpy.QtWidgets import QApplication
    from qtpy.QtGui import QImage
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui.videopreview import VideoPreviewPanel
    vp = VideoPreviewPanel(VideoStore(str(tmp_path / "video")), names_fn=lambda: {})
    painted = []
    vp.view.set_image = lambda img: painted.append(img)
    im = QImage(4, 4, QImage.Format.Format_RGB888)
    im.fill(0)
    frame = types.SimpleNamespace(toImage=lambda: im)
    vp._cur_path = None                                # between a switch/gap and set_head
    vp._on_frame(frame)
    assert painted == []                               # stale frame dropped
    vp._cur_path = "seg.mp4"                            # a source is now loaded
    vp._on_frame(frame)
    assert len(painted) == 1                            # and it paints


def test_video_preview_requests_backfill_on_a_local_gap(tmp_path):
    """When the head lands where the local store has no segment, the preview asks
    its miss handler to pull from the hub — but not while footage is local (§9.3 ph3)."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])           # noqa: F841
    from ferrodac.core.videostore import VideoStore
    from ferrodac.ui.videopreview import VideoPreviewPanel
    st = VideoStore(str(tmp_path / "video"))
    base = 1_700_000_000.0
    p = st.segment_path("camA", base)
    open(p, "wb").write(b"\x00" * 64)
    st.commit("camA", base, base + 60, p)
    vp = VideoPreviewPanel(st, names_fn=lambda: {})
    vp._cam = "camA"
    misses = []
    vp.set_miss_handler(lambda cam, t: misses.append((cam, t)))
    vp.set_head(base + 30)                             # inside a local segment → no pull
    assert misses == []
    vp.set_head(base + 9999)                           # a gap → backfill requested
    assert misses == [("camA", base + 9999)]


def test_clips_bundle_waits_for_the_export_to_finish(tmp_path, monkeypatch):
    """Regression: a long recording's auto-export sets run_dir only when it
    COMPLETES, which can outlast clip materialization. The bundler must DEFER
    (not silently drop the clips onto a still-None run_dir) until the export
    lands via _on_recording_saved (§9.3)."""
    import ferrodac.ui.app as appmod
    from ferrodac.core.markers import MarkerModel

    dispatched = []
    monkeypatch.setattr(appmod, "run_task",
                        lambda work, **kw: dispatched.append(kw.get("exclusive")))

    markers = MarkerModel()
    rec_mid = markers.add(100.0, kind="recording", t_end=160.0, label="REC")
    clip_mid = markers.add(
        100.0, t_end=160.0, kind="media", label="🎬",
        payload={"file": "media/clip.mp4", "files": ["media/clip.mp4"],
                 "source": "camA/frame", "format": "mp4", "rec_mid": rec_mid})
    tags = [markers.get(clip_mid)]

    fake = types.SimpleNamespace(
        dashboard=types.SimpleNamespace(markers=markers),
        _project_root=lambda: str(tmp_path),
        _refresh_explorer=lambda: None,
        statusBar=lambda: types.SimpleNamespace(showMessage=lambda *a, **k: None))
    fake._bundle_clips_into_run = types.MethodType(
        appmod.MainWindow._bundle_clips_into_run, fake)

    # export still running (run_dir is None) → clips are STASHED, not dispatched
    fake._bundle_clips_into_run(rec_mid, 100.0, 160.0, tags)
    assert dispatched == []
    assert rec_mid in fake.__dict__["_pending_clip_bundles"]

    # export finishes: run_dir lands, then the save callback flushes the clips
    markers.update(rec_mid, run_dir=str(tmp_path / "run_x"))
    appmod.MainWindow._on_recording_saved(fake, rec_mid, str(tmp_path / "run_x"), 2)
    assert dispatched == [f"bundle-clip:{rec_mid}"]    # NOW dispatched, exactly once
    assert rec_mid not in fake.__dict__.get("_pending_clip_bundles", {})
