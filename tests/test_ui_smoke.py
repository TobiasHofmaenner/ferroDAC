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
    from qtpy.QtCore import QSettings
    s = QSettings("ferroDAC", "ferroDAC")          # redirect projects off real Documents
    s.setValue("project/root", tempfile.mkdtemp())
    s.setValue("project/active", "")
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.registry import load_builtin_drivers
    from ferrodac.ui.app import MainWindow
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
    w = _mainwindow(qapp)
    try:
        mgr = w._project_mgr
        assert mgr.active.name == "Default"               # built-in home
        assert w.windowTitle().endswith("Default")
        w._create_project("Experiment 1")                  # create → auto-switch
        assert mgr.active.name == "Experiment 1"
        assert w.projects_panel._list.count() == 2
        assert w._runs_dir().startswith(mgr.active.path)   # recordings file under it
        did = next(p.id for p in mgr.projects() if p.name == "Default")
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
        w._create_project("Exp")
        assert w.dashboard.markers.lens == {w._project_mgr.active.id}   # follows switch
    finally:
        w.close()


@pytest.mark.ui
def test_device_qualified_label(qapp):
    from ferrodac.ui.workspace import SourcePort
    assert SourcePort("u/v", "Voltage", "float", "V", "PSU 1", "device").label == "Voltage · PSU 1"
    # device already in the name → no redundant qualifier; historic → bare
    assert SourcePort("u/v", "PSU 1 Voltage", "float", "V", "PSU 1", "device").label == "PSU 1 Voltage"
    assert SourcePort("h/x", "spectrum", "trace", "", "recorded", "historic").label == "spectrum"
