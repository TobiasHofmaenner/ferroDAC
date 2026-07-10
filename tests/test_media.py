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
