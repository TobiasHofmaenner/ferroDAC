"""UI smoke tests (offscreen Qt).

Not pixel-level — these construct the real widgets and exercise the paths that
have actually broken in the past (window build, replay reset, the time-axis
waterfall, device-qualified labels). Fast, headless, and they fail loudly on an
import/wiring regression. Marked `ui` so the lightweight CI gate can skip Qt.
"""

import types

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytest.importorskip("pyqtgraph")


def _mainwindow(qapp):
    import tempfile
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.registry import load_builtin_drivers
    from ferrodac.ui.app import MainWindow
    d = tempfile.mkdtemp()
    MainWindow._app_dir = lambda self, _d=d: _d     # isolate store/projects/tags/etc.
    engine = Engine()
    manager = DeviceManager(load_builtin_drivers(), engine=engine, registry=None)
    return MainWindow(manager, engine)


@pytest.mark.ui
def test_mainwindow_constructs(qapp):
    w = _mainwindow(qapp)
    try:
        assert w.time_context.grow is True          # default: grow from launch
        assert w.replay.playback.store is w.resolver  # replay reads via the resolver
    finally:
        w.close()


@pytest.mark.ui
def test_open_timeline_and_tick(qapp):
    w = _mainwindow(qapp)
    try:
        w._open_timeline()
        qapp.processEvents()
        w._timeline_win._live_tick()                # exercises the source-sync path
        qapp.processEvents()
    finally:
        w.close()


def _scan(t, x):
    from ferrodac.core.trace import Trace
    return types.SimpleNamespace(
        key="k", t=t, value=Trace(x=x, y=np.exp(-((x - 18) ** 2))), partial=False)


@pytest.mark.ui
def test_waterfall_time_axis_window(qapp):
    from qtpy.QtCore import QRectF
    from ferrodac.ui.panels import WaterfallPanel
    p = WaterfallPanel()
    p.add_source("k", types.SimpleNamespace(name="spec", unit="", dtype="trace"))
    p._src_key = "k"
    x = np.linspace(1, 50, 64)
    t0 = 1_000_000.0
    p.clear_history()
    p.set_window(t0, t0 + 3600)                     # Y range = the timeline window
    p.feed([_scan(t0 + i * 120, x) for i in range(30)])   # sparse → real gaps
    # the image is PLACED over the window in time (deterministic, no layout needed)
    rect: QRectF = p.img.mapRectToView(p.img.boundingRect())
    lo, hi = sorted((rect.top(), rect.bottom()))
    assert abs(lo - t0) < 1 and abs(hi - (t0 + 3600)) < 1, "waterfall Y not mapped to the window"


@pytest.mark.ui
def test_waterfall_hold_vs_discrete(qapp):
    from ferrodac.ui.panels import _time_binned
    y = np.ones(8, dtype=np.float32)
    scans = [(1000.0 + i * 10, y) for i in range(8)]    # regular 10 s cadence
    img_hold, _ = _time_binned(scans, 1000, 1100, 100, hold=True)
    img_disc, _ = _time_binned(scans, 1000, 1100, 100, hold=False)
    filled_hold = int(np.sum(np.any(np.isfinite(img_hold), axis=1)))
    filled_disc = int(np.sum(np.any(np.isfinite(img_disc), axis=1)))
    assert filled_hold > 60 and filled_disc == 8     # hold continuous, discrete = 1/scan
    # a real outage (>> local cadence) stays blank under hold
    scans2 = [(1000.0 + i * 10, y) for i in range(5)] + [(1700.0 + i * 10, y) for i in range(5)]
    img2, _ = _time_binned(scans2, 1000, 1800, 400, hold=True)
    assert np.all(np.isnan(img2[int((1350 - 1000) / 800 * 400)]))


@pytest.mark.ui
def test_projects_default_create_switch(qapp):
    import tempfile
    w = _mainwindow(qapp)
    try:
        mgr = w._project_mgr
        assert mgr.active.name == "Default"               # built-in home
        assert w.windowTitle().endswith("Default")
        # track a project in a chosen folder (what _add_project does post-dialog)
        p = mgr.track(tempfile.mkdtemp(), "Experiment 1")
        w.projects_panel.refresh()
        w._switch_project(p.id)
        assert mgr.active.name == "Experiment 1"
        assert w.projects_panel._list.count() == 2
        assert w._runs_dir().startswith(mgr.active.path)   # recordings file under it
        did = next(pp.id for pp in mgr.projects() if pp.name == "Default")
        w._switch_project(did)
        assert mgr.active.name == "Default"
    finally:
        w.close()


@pytest.mark.ui
def test_tag_project_lens(qapp):
    from ferrodac.core.markers import MarkerModel
    ms = MarkerModel()
    ms.default_projects = ["pA"]                      # new tags file under the active
    ms.add(1.0, label="in A")                         # → pA by default
    b = ms.add(2.0, label="in B", projects=["pB"])
    ms.add(3.0, label="unfiled", projects=[])
    assert {m.label for m in ms.visible()} == {"in A", "in B", "unfiled"}  # no lens
    ms.set_lens(["pA"])                               # active-project lens
    assert {m.label for m in ms.visible()} == {"in A", "unfiled"}   # B hidden, unfiled kept
    ms.set_lens(None)
    assert len(ms.visible()) == 3                     # widen → all
    ms.add_to_project(b, "pA")                        # re-file B into A
    ms.set_lens(["pA"])
    assert "in B" in {m.label for m in ms.visible()}
    assert len(ms.all()) == 3                         # the catalog is never filtered


@pytest.mark.ui
def test_project_sets_tag_lens(qapp):
    w = _mainwindow(qapp)
    try:
        assert w.dashboard.markers.lens == {w._project_mgr.active.id}   # active lens
        w._set_tag_lens_all(True)
        assert w.dashboard.markers.lens is None                        # show all
        w._set_tag_lens_all(False)
        import tempfile
        p = w._project_mgr.track(tempfile.mkdtemp(), "Exp")
        w._switch_project(p.id)
        assert w.dashboard.markers.lens == {w._project_mgr.active.id}   # follows switch
    finally:
        w.close()


@pytest.mark.ui
def test_project_sets_source_lens(qapp):
    import tempfile
    w = _mainwindow(qapp)
    try:
        # a fresh project with no curation = no lens (Sources shows everything)
        assert w.dashboard.source_lens is None
        # curate two channels on the active project → the lens narrows the view
        w._project_mgr.active.set_sources([{"key": "a/x"}, {"key": "b/y"}])
        w._apply_source_lens()
        assert w.dashboard.source_lens == {"a/x", "b/y"}
        # 'All' overrides the lens without forgetting the selection
        w._set_source_lens_all(True)
        assert w.dashboard.source_lens is None
        w._set_source_lens_all(False)
        assert w.dashboard.source_lens == {"a/x", "b/y"}
        # switching to an un-curated project clears the lens (not blank-by-default)
        p = w._project_mgr.track(tempfile.mkdtemp(), "Exp")
        w._switch_project(p.id)
        assert w.dashboard.source_lens is None
    finally:
        w.close()


@pytest.mark.ui
def test_layout_add_and_autosave(qapp):
    import os
    import tempfile
    w = _mainwindow(qapp)
    try:
        p = w._project_mgr.active
        # _on_add_layout writes a named file (no picker) + makes it the live one
        path = p.layout_path("Overview")
        w._write_session(path)
        w._active_layout_path = path
        assert "Overview" in p.layouts()
        # a named layout open → autosave writes IT too, not just working.json
        os.remove(path)
        w._do_autosave()
        assert os.path.exists(path) and os.path.exists(p.working_path)
        # opening another layout re-binds the live, autosaving target
        other = p.layout_path("Other")
        w._write_session(other)
        w._open_layout(other)
        assert w._active_layout_path == other
        # switching projects drops the binding (the new one isn't in a layout yet)
        q = w._project_mgr.track(tempfile.mkdtemp(), "Q")
        w._switch_project(q.id)
        assert w._active_layout_path is None
    finally:
        w.close()


@pytest.mark.ui
def test_project_explorer_groups(qapp):
    import json
    import os
    w = _mainwindow(qapp)
    try:
        ex = w.project_explorer
        p = w._project_mgr.active
        # the explorer follows the active project and exposes its three groups
        assert ex._label.text() == p.name
        assert ex._layout_cards(p) == [] and ex._recording_cards(p) == []
        # drop a layout + a recording bundle on disk → the scan picks them up
        open(p.layout_path("overview"), "w").write("{}")
        run = os.path.join(p.reports_dir, "run_x")
        os.makedirs(run)
        json.dump({"t0": 1000.0, "t1": 1030.0, "sources": [{"key": "a"}]},
                  open(os.path.join(run, "manifest.json"), "w"))
        ex.refresh()                                  # what _switch_project/record call
        assert len(ex._layout_cards(p)) == 1
        assert len(ex._recording_cards(p)) == 1
        # curated channels surface as their own group
        p.set_sources([{"key": "dev/p1"}])
        assert len(ex._channel_cards(p)) == 1
    finally:
        w.close()


@pytest.mark.ui
def test_project_docs_and_bookmarks(qapp):
    import os
    w = _mainwindow(qapp)
    try:
        ex = w.project_explorer
        p = w._project_mgr.active
        assert ex._doc_cards(p) == [] and ex._window_cards(p) == []
        # a reference file dropped in docs/ shows up as a card
        open(os.path.join(p.docs_dir, "notes.txt"), "w").write("hi")
        ex.refresh()
        assert len(ex._doc_cards(p)) == 1
        # bookmark a window (model path; the UI prompts for the name) → card appears
        p.add_window("bakeout", 1000.0, 1600.0)
        ex.refresh()
        assert len(ex._window_cards(p)) == 1
        # jumping a bookmark parks the timeline on it (re-streams that slice)
        tc = w.time_context
        nav0 = tc.nav
        w._jump_to_window(1000.0, 1600.0)
        assert abs(tc.window[0] - 1000.0) < 1 and abs(tc.window[1] - 1600.0) < 1
        assert tc.following is False and tc.nav == nav0 + 1
        w._remove_bookmark("bakeout")
        assert ex._window_cards(p) == []
    finally:
        w.close()


@pytest.mark.ui
def test_events_split_recordings_and_tags(qapp):
    from ferrodac.ui.app import CollapsibleGroup, EventsPanel
    from ferrodac.core.markers import MarkerModel
    from ferrodac.core.tag import RECORDING
    ms = MarkerModel()
    r = ms.add(100.0, kind=RECORDING, label="REC")    # a slice (span)
    ms.update(r, t_end=160.0)
    ms.add(120.0, label="note")                       # a point in time
    clock = types.SimpleNamespace(rel=lambda t: t)
    panel = EventsPanel(ms, clock)
    try:
        titles = [panel._layout.itemAt(i).widget()._btn.text()
                  for i in range(panel._layout.count())
                  if isinstance(panel._layout.itemAt(i).widget(), CollapsibleGroup)]
        # two distinct sections, not one flat list
        assert any(t.startswith("Recordings") for t in titles)
        assert any(t.startswith("Tags") for t in titles)
    finally:
        panel.deleteLater()


@pytest.mark.ui
def test_zoom_recording_parks_window(qapp):
    """Zoom on a recording parks the timeline ON its span (and flags navigation)
    so the controller re-streams that slice — not just pans the charts there."""
    import time as _time
    from ferrodac.core.tag import RECORDING
    w = _mainwindow(qapp)
    try:
        tc = w.time_context
        assert tc is not None                         # data plane up in tests
        now = _time.time()
        ms = w.dashboard.markers
        r = ms.add(now - 500, kind=RECORDING, label="REC")
        ms.update(r, t_end=now - 200)
        nav0 = tc.nav
        w._zoom_recording(r)
        t0, t1 = tc.window
        assert abs(t0 - (now - 500)) < 1 and abs(t1 - (now - 200)) < 1   # window on the span
        assert tc.following is False                  # parked (not live-following)
        assert tc.nav == nav0 + 1                     # navigation → controller reloads
    finally:
        w.close()


@pytest.mark.ui
def test_jump_to_tag_parks_centered(qapp):
    """The tag card's ⌖ jump parks a window of the current width centred on the
    point (and flags navigation) so the controller re-streams that slice."""
    import time as _time
    w = _mainwindow(qapp)
    try:
        tc = w.time_context
        tc.set_width(300.0)
        now = _time.time()
        ms = w.dashboard.markers
        t = now - 1000
        mid = ms.add(t, label="anomaly")              # a point in time
        nav0 = tc.nav
        w._jump_to_tag(mid)
        t0, t1 = tc.window
        assert t0 <= t <= t1                          # the tag is inside the window
        assert abs((t0 + t1) / 2 - t) < 2             # …centred on it
        assert abs((t1 - t0) - 300.0) < 2             # current width preserved
        assert tc.following is False and tc.nav == nav0 + 1   # parked + navigated
    finally:
        w.close()


@pytest.mark.ui
def test_ribbon_whole_window_drag_translates(qapp):
    """Dragging the WHOLE region commits as a translation ("move"), not a head
    park — so the window keeps its width instead of collapsing onto one line."""
    from ferrodac.ui.timeline import Ribbon
    r = Ribbon(["k"], {"k": []}, 0.0, 1000.0)
    r.getPlotItem().getViewBox().setXRange(0, 1000, padding=0)
    seen = []
    r.windowChanged.connect(lambda a, b, mode: seen.append((a, b, mode)))

    def drag_to(a, b):                                # emulate a release at (a,b)
        r.region.blockSignals(True)
        r.region.setRegion((a, b))
        r.region.blockSignals(False)
        r._on_region_done()

    r.set_window(400.0, 700.0)
    drag_to(200.0, 500.0)                             # both edges −200 → translate
    a, b, mode = seen[-1]
    assert mode == "move" and abs((b - a) - 300.0) < 1e-6   # width preserved, no collapse
    r.set_window(200.0, 500.0)
    drag_to(200.0, 350.0)                             # head in → front
    assert seen[-1][2] == "front"
    r.set_window(200.0, 500.0)
    drag_to(300.0, 500.0)                             # tail in → back
    assert seen[-1][2] == "back"


@pytest.mark.ui
def test_ribbon_min_window_is_zoom_relative(qapp):
    """The timeline window can't be dragged shut (head onto tail), but the floor is
    a fraction of the VISIBLE span — zoom in and you can make a finer window."""
    from ferrodac.ui.timeline import Ribbon
    r = Ribbon(["k"], {"k": []}, 0.0, 1000.0)
    vb = r.getPlotItem().getViewBox()
    vb.setXRange(0, 1000, padding=0)
    r.set_window(200.0, 800.0)
    r.region.setRegion((200.0, 200.5))               # collapse the head onto the tail
    a, b = r.region.getRegion()
    assert b - a >= 0.029 * 1000                      # floored to ~3% of the 1000-wide view
    vb.setXRange(0, 100, padding=0)                   # zoom in 10×
    r.set_window(40.0, 60.0)
    r.region.setRegion((40.0, 40.1))
    a2, b2 = r.region.getRegion()
    assert 0.029 * 100 <= (b2 - a2) < (b - a)         # finer floor when zoomed in


@pytest.mark.ui
def test_timeline_respects_channel_lens(qapp):
    """The Timeline's source list shows the project's curated channels (like the
    Sources panel), with an "All" toggle to widen to everything."""
    from qtpy.QtCore import Qt
    from ferrodac.ui.workspace import SourcePort
    w = _mainwindow(qapp)
    try:
        for key in ("mg/ch1", "mg/ch2", "psu/v"):       # three live channels
            w.dashboard._sources[key] = SourcePort(key, key.split("/")[-1],
                                                    "float", "", "dev", "device")
        w._project_mgr.active.set_sources([{"key": "mg/ch1"}])   # curate one
        w._open_timeline()
        qapp.processEvents()
        tl = w._timeline_win
        shown = {tl._src_list.item(i).data(Qt.UserRole)
                 for i in range(tl._src_list.count())}
        assert shown == {"mg/ch1"}                       # lens: only the curated channel
        tl._all_chk.setChecked(True)                     # "All" → widen to everything
        qapp.processEvents()
        shown_all = {tl._src_list.item(i).data(Qt.UserRole)
                     for i in range(tl._src_list.count())}
        assert {"mg/ch1", "mg/ch2", "psu/v"} <= shown_all
    finally:
        w.close()


@pytest.mark.ui
def test_timeline_opens_on_parked_window(qapp):
    """Opening the Timeline while parked (e.g. after Zoom-to-recording) keeps that
    window and frames the ribbon on it — it must not snap back to the live edge."""
    import time as _time
    w = _mainwindow(qapp)
    try:
        tc = w.time_context
        now = _time.time()
        tc.park_window(now - 500, now - 200)          # where Zoom-to-recording lands
        a, b = tc.window
        w._open_timeline()
        qapp.processEvents()
        assert tc.following is False                   # didn't jump back to live
        assert abs(tc.window[0] - a) < 1 and abs(tc.window[1] - b) < 1   # same window
        # the ribbon view frames the parked window (region on screen, not off to the side)
        (vx0, vx1), _ = w._timeline_win.ribbon.getPlotItem().getViewBox().viewRange()
        assert vx0 <= a + 1 and vx1 >= b - 1
    finally:
        w.close()


@pytest.mark.ui
def test_autorange_ignores_markers(qapp):
    """The "A" auto-range fits the DATA — tags/recordings are annotations and must
    not drag the time axis open (ignoreBounds on the marker items)."""
    from ferrodac.ui.panels import ChartPanel
    from ferrodac.core.markers import MarkerModel
    from ferrodac.core.tag import RECORDING
    p = ChartPanel()
    src = types.SimpleNamespace(name="p", label="p", unit="mbar", dtype="float")
    p.add_source("k", src)
    p.feed([types.SimpleNamespace(key="k", t=1000.0 + i, value=1.0 + i, status=0)
            for i in range(11)])                     # data lives in t ∈ [1000, 1010]
    ms = MarkerModel()
    ms.add(50000.0, label="far tag")                 # a point far in the future
    r = ms.add(60000.0, kind=RECORDING, label="REC")  # a span far away too
    ms.update(r, t_end=61000.0)
    p.attach_session(types.SimpleNamespace(), ms)
    vb = p.plot.getViewBox()
    vb.autoRange()
    (xlo, xhi), _ = vb.viewRange()
    assert xhi < 2000, "marker dragged the time axis open"   # ~1010, not 50000/60000
    assert xlo > 500


@pytest.mark.ui
def test_zoom_time_uses_correct_axis(qapp):
    """Zoom-to-recording / jump-to-tag frames each panel's OWN time axis: a chart's
    X, a waterfall's Y (its X is m/z) — not blindly X everywhere (which jammed
    epoch time onto the waterfall's m/z axis and missed the target)."""
    from ferrodac.ui.panels import ChartPanel, WaterfallPanel
    c = ChartPanel()
    c.add_source("k", types.SimpleNamespace(name="p", label="p", unit="", dtype="float"))
    c.feed([types.SimpleNamespace(key="k", t=1000.0 + i, value=1.0 + i, status=0)
            for i in range(11)])
    c.zoom_time(1002.0, 1006.0)
    (cx0, cx1), _ = c.plot.getViewBox().viewRange()
    assert cx0 <= 1002.5 and cx1 >= 1005.5            # chart: X framed to the time window
    wf = WaterfallPanel()
    wf.add_source("k", types.SimpleNamespace(name="spec", unit="", dtype="trace"))
    wf._src_key = "k"
    x = np.linspace(1, 50, 64)
    t0 = 1_000_000.0
    wf.set_window(t0, t0 + 600)
    wf.feed([_scan(t0 + i * 30, x) for i in range(11)])
    wf.zoom_time(t0 + 100, t0 + 300)
    (wx0, wx1), (wy0, wy1) = wf.plot.getViewBox().viewRange()
    assert wx1 < 1000                                 # m/z axis untouched (NOT epoch time)
    assert abs(wy0 - (t0 + 100)) < 30 and abs(wy1 - (t0 + 300)) < 30   # Y framed to the window


@pytest.mark.ui
def test_waterfall_markers_draggable_and_levels_lock(qapp):
    """On a waterfall the time axis is Y: tag/recording markers are draggable there
    (drag retimes them), and a user-set colour range isn't reset by every tick."""
    from ferrodac.ui.panels import WaterfallPanel
    from ferrodac.core.markers import MarkerModel
    from ferrodac.core.tag import RECORDING
    p = WaterfallPanel()
    p.add_source("k", types.SimpleNamespace(name="spec", unit="", dtype="trace"))
    p._src_key = "k"
    x = np.linspace(1, 50, 32)
    t0 = 1_000_000.0
    p.set_window(t0, t0 + 600)
    p.feed([_scan(t0 + i * 30, x) for i in range(11)])
    ms = MarkerModel()
    tag = ms.add(t0 + 100, label="note")
    rec = ms.add(t0 + 200, kind=RECORDING, label="REC")
    ms.update(rec, t_end=t0 + 400)
    p.attach_session(types.SimpleNamespace(), ms)
    line, region = p._marker_lines[tag], p._marker_lines[rec]
    assert line.movable and region.movable                # draggable on the time (Y) axis
    line.setValue(t0 + 150); p._on_marker_drag(tag)       # drag the tag → retime it
    assert abs(ms.get(tag).t - (t0 + 150)) < 1e-6
    region.setRegion((t0 + 250, t0 + 500)); p._on_region_drag(rec)   # drag the span edges
    assert abs(ms.get(rec).t - (t0 + 250)) < 1e-6 and abs(ms.get(rec).t_end - (t0 + 500)) < 1e-6
    # a data tick must NOT snap the markers back (they sync on change, not per tick)
    p.feed([_scan(t0 + 330, x)])
    assert abs(line.value() - (t0 + 150)) < 1e-6
    # the colour range, once the user sets it, survives data ticks (no reset)
    p._bar.setLevels((0.2, 0.8)); p._on_levels_changed()
    p.feed([_scan(t0 + 360, x)])
    assert p._levels_locked and tuple(round(v, 2) for v in p.img.levels) == (0.2, 0.8)


@pytest.mark.ui
def test_waterfall_autorange_ignores_markers(qapp):
    """Same as the chart, but the waterfall carries markers on its TIME (Y) axis —
    a far tag/recording must not drag that axis open on "A"."""
    from ferrodac.ui.panels import WaterfallPanel
    from ferrodac.core.markers import MarkerModel
    from ferrodac.core.tag import RECORDING
    p = WaterfallPanel()
    p.add_source("k", types.SimpleNamespace(name="spec", unit="", dtype="trace"))
    p._src_key = "k"
    x = np.linspace(1, 50, 64)
    t0 = 1_000_000.0
    p.feed([_scan(t0 + i * 30, x) for i in range(11)])   # scans over t ∈ [t0, t0+300]
    ms = MarkerModel()
    ms.add(t0 + 100_000.0, label="far tag")              # ~28 h later
    r = ms.add(t0 + 200_000.0, kind=RECORDING, label="REC")
    ms.update(r, t_end=t0 + 201_000.0)
    p.attach_session(types.SimpleNamespace(), ms)
    vb = p.plot.getViewBox()
    vb.autoRange()
    _, (ylo, yhi) = vb.viewRange()
    assert yhi < t0 + 5000, "marker dragged the time (Y) axis open"   # ~t0+300, not +200000
    assert ylo > t0 - 5000


@pytest.mark.ui
def test_hub_project_incoming_appears_and_clears(qapp):
    """A project record arriving from the hub materialises into the ProjectManager
    (as a hub project) and shows in the Projects dock; disconnect drops it."""
    w = _mainwindow(qapp)
    try:
        mgr = w._project_mgr
        assert w.hub._project_mgr is mgr            # the app wired hub-project sync
        n0 = len(mgr.projects())
        rec = {"id": "hubX", "name": "Shared X", "version": 1,
               "sources": ["mg/ch1"], "windows": [], "layouts": {}, "deleted": False}
        w.hub._on_project_gui(rec)                  # the queued _project signal path
        hp = mgr.get("hubX")
        assert hp is not None and hp.is_hub and hp.name == "Shared X"
        assert len(mgr.projects()) == n0 + 1
        labels = [w.projects_panel._list.item(i).text()
                  for i in range(w.projects_panel._list.count())]
        assert any("Shared X" in t for t in labels)
        # a newer version edits in place (LWW); a stale one is ignored
        w.hub._on_project_gui({**rec, "name": "Shared X2", "version": 2})
        assert mgr.get("hubX").name == "Shared X2"
        # disconnect → hub projects vanish (not available offline)
        mgr.clear_hub()
        assert mgr.get("hubX") is None
    finally:
        w.close()


@pytest.mark.ui
def test_hub_layout_live_sync(qapp):
    """An OPEN named layout on a hub project syncs live (autosave republishes the
    record); a working-layout-only autosave stays local (not in the shared record)."""
    w = _mainwindow(qapp)
    try:
        mgr = w._project_mgr
        w.hub._viewer = types.SimpleNamespace(stop=lambda: None)
        pushed = []
        w.hub.publish_project = lambda rec: pushed.append(rec)
        hp = mgr.apply_hub_record({"id": "h1", "name": "H", "version": 1,
                                   "sources": [], "windows": [], "layouts": {},
                                   "deleted": False})
        mgr.set_active("h1")
        # no named layout open → working autosave does NOT push (working stays local)
        w._active_layout_path = None
        w._do_autosave()
        assert pushed == []
        # a named layout open → autosave writes it AND pushes a version-bumped record
        w._active_layout_path = hp.layout_path("shared")
        v = hp.version
        w._do_autosave()
        assert pushed and pushed[-1]["version"] == v + 1
        assert "shared" in pushed[-1]["layouts"]      # the live layout blob went up
    finally:
        w.close()


@pytest.mark.ui
def test_hub_project_share_and_republish(qapp):
    """Sharing a local project MOVES it to the hub (publishes its record, untracks
    the local entry); editing a hub project republishes a version-bumped record."""
    import tempfile
    w = _mainwindow(qapp)
    try:
        mgr = w._project_mgr
        w.hub._viewer = types.SimpleNamespace(stop=lambda: None)   # pretend connected
        pushed = []
        w.hub.publish_project = lambda rec: pushed.append(rec)
        local = mgr.track(tempfile.mkdtemp(), "ToShare")
        local.add_window("w", 1.0, 2.0)
        w._share_project(local.id)
        hp = mgr.get(local.id)
        assert hp is not None and hp.is_hub                  # now a ☁ project
        assert local.id not in mgr._by_id                    # local entry untracked
        assert pushed[-1]["id"] == local.id
        assert pushed[-1]["windows"][0]["name"] == "w"       # the lens/bookmarks went up
        # an edit to the active hub project pushes a bumped record
        mgr.set_active(hp.id)
        before = hp.version
        w._republish_active_if_hub()
        assert pushed[-1]["version"] == before + 1
    finally:
        w.close()


def test_editor_args_template():
    from ferrodac.ui.app import _editor_args
    assert _editor_args("konsole -e nvim {file}", "/a/b.md") == \
        ["konsole", "-e", "nvim", "/a/b.md"]
    assert _editor_args("code", "/a/b.md") == ["code", "/a/b.md"]        # appended
    assert _editor_args("gvim '{file}'", "/x y.md") == ["gvim", "/x y.md"]  # path w/ space
    assert _editor_args("", "/a/b.md") == []                             # blank → none


@pytest.mark.ui
def test_open_doc_external_uses_configured_command(qapp, monkeypatch):
    """↗ Open externally runs the CONFIGURED editor command directly (no OS chooser)."""
    from qtpy.QtCore import QSettings
    w = _mainwindow(qapp)
    try:
        QSettings("ferroDAC", "ferroDAC").setValue("editor/command", "konsole -e nvim {file}")
        launched = {}
        monkeypatch.setattr("subprocess.Popen", lambda args, **kw: launched.update(args=args))
        w._open_doc_external("/proj/README.md")
        assert launched["args"] == ["konsole", "-e", "nvim", "/proj/README.md"]
        # blank command → falls back to the OS open (no subprocess)
        QSettings("ferroDAC", "ferroDAC").setValue("editor/command", "")
        launched.clear()
        revealed = {}
        monkeypatch.setattr(w, "_reveal_path", lambda p: revealed.update(p=p))
        w._open_doc_external("/proj/README.md")
        assert launched == {} and revealed["p"] == "/proj/README.md"
    finally:
        QSettings("ferroDAC", "ferroDAC").remove("editor/command")
        w.close()


@pytest.mark.ui
def test_gui_thread_gc(qapp):
    """The segfault fix: automatic GC is disabled (so it never runs on a worker
    thread and frees a QObject-with-timer cross-thread), and collection is drained
    from a GUI-thread timer instead."""
    import gc
    from ferrodac.diagnostics import install_gui_thread_gc
    was = gc.isenabled()
    timer = None
    try:
        timer = install_gui_thread_gc(500)
        assert not gc.isenabled()       # no cyclic GC on a worker thread, ever
        assert timer.isActive()         # …collected on the GUI thread instead
    finally:
        if timer is not None:
            timer.stop()
        if was:
            gc.enable()


@pytest.mark.ui
def test_docs_dock_is_lazy(qapp):
    """The Docs dock exists but its QtWebEngine view is NOT created until shown —
    so launch + the UI suite don't spin up Chromium per window."""
    w = _mainwindow(qapp)
    try:
        assert hasattr(w, "docs_dock")
        assert not w.docs_dock.isVisible()        # hidden by default
        assert w._docs_view is None               # lazy: no WebEngine instantiated
    finally:
        w.close()


@pytest.mark.ui
def test_docs_dock_renders_active_readme(qapp):
    """Opening the Docs dock lazily builds the view, bootstraps the active project's
    README.md, and renders it (the integration the user sees)."""
    pytest.importorskip("qtpy.QtWebEngineWidgets")
    import os
    import time
    w = _mainwindow(qapp)
    try:
        w._ensure_docs_view()                     # what first-show triggers
        assert w._docs_view is not None
        p = w._project_mgr.active
        assert os.path.exists(os.path.join(p.path, "README.md"))   # bootstrapped
        out = {"html": ""}
        end = time.time() + 30
        while time.time() < end:
            w._docs_view.view.page().runJavaScript(
                "var d=document.getElementById('doc'); d?d.innerHTML:''",
                lambda h: out.__setitem__("html", h or ""))
            for _ in range(20):
                qapp.processEvents()
                time.sleep(0.02)
            if p.name in out["html"]:
                break
        assert p.name in out["html"], "active project README did not render"
    finally:
        w.close()


@pytest.mark.ui
def test_device_qualified_label(qapp):
    from ferrodac.ui.workspace import SourcePort
    assert SourcePort("u/v", "Voltage", "float", "V", "PSU 1", "device").label == "Voltage · PSU 1"
    # device already in the name → no redundant qualifier; historic → bare
    assert SourcePort("u/v", "PSU 1 Voltage", "float", "V", "PSU 1", "device").label == "PSU 1 Voltage"
    assert SourcePort("h/x", "spectrum", "trace", "", "recorded", "historic").label == "spectrum"
