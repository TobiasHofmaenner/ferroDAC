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


# -- §9 stage c: documentation clips per Record span -----------------------------

class _FakeCam:
    """Duck-typed camera for ClipService: records start/stop calls; whether a
    file 'lands' is controlled by the test (QMediaRecorder finalises async)."""

    def __init__(self, data_id, name, enabled=True, streaming=True):
        self.data_id, self.name = data_id, name
        self.clips_enabled, self._streaming = enabled, streaming
        self.started_path, self.stopped = None, False

    def start_clip(self, path):
        if not self._streaming:
            return False
        self.started_path = path
        return True

    def stop_clip(self):
        self.stopped = True


def _clip_rig(tmp_path, cams):
    from ferrodac.core.media import ClipService
    markers = MarkerModel()
    svc = ClipService(devices=lambda: cams, markers=markers,
                      media_dir=lambda: str(tmp_path / "media"),
                      names=lambda: {})
    return svc, markers


def test_clips_start_only_on_opted_in_streaming_cameras(tmp_path):
    cams = [_FakeCam("camA", "Bench"), _FakeCam("camB", "Chamber", enabled=False),
            _FakeCam("camC", "Scope", streaming=False)]
    svc, _ = _clip_rig(tmp_path, cams)
    n = svc.on_record_start(time.time())
    assert n == 1 and svc.running
    assert cams[0].started_path and cams[0].started_path.endswith(".mp4")
    assert "media" in cams[0].started_path
    assert cams[1].started_path is None                # opted out
    assert cams[2].started_path is None                # not streaming


def test_finalize_tags_only_files_that_landed(tmp_path):
    cams = [_FakeCam("camA", "Bench"), _FakeCam("camB", "Chamber")]
    svc, markers = _clip_rig(tmp_path, cams)
    t0 = time.time() - 30
    svc.on_record_start(t0)
    t1 = time.time()
    entries = svc.on_record_stop(t1)
    assert len(entries) == 2 and all(c.stopped for c in cams)
    assert not svc.running
    # camA's encoder "worked": its file landed; camB's never appeared
    os.makedirs(os.path.dirname(cams[0].started_path), exist_ok=True)
    with open(cams[0].started_path, "wb") as fh:
        fh.write(b"\x00" * 2048)
    tagged, failed = svc.finalize(entries)
    assert len(tagged) == 1 and len(failed) == 1
    assert failed[0]["key"] == "camB/frame"            # named, not silent

    m = markers.get(tagged[0]["tag_id"])               # the span tag
    assert m.kind == MEDIA and m.is_region
    assert m.t == pytest.approx(t0) and m.t_end == pytest.approx(t1)
    assert m.payload["format"] == "mp4"
    assert m.payload["clip"] == "documentation"        # §9.1: labelled as such
    assert m.label.startswith("🎬")
    assert len(markers.visible()) == 1                 # no ghost tag for camB


def test_double_start_is_ignored(tmp_path):
    cams = [_FakeCam("camA", "Bench")]
    svc, _ = _clip_rig(tmp_path, cams)
    assert svc.on_record_start(time.time()) == 1
    assert svc.on_record_start(time.time()) == 0       # stray second start: no-op


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
