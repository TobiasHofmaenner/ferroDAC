"""Shared fixtures for control-surface verb tests.

`control_surface` builds ferrodac.ui.appcontrol.build_control_surface against a REAL
offscreen MainWindow (the same construction as test_ui_smoke._mainwindow), so a
dispatched verb exercises the true method signatures + JSON-serialization path — not a
hand-rolled fake that could re-encode a contract wrongly (the class of bug this batch
was built to avoid).
"""

import tempfile
import types

import pytest


@pytest.fixture
def control_surface(qapp):
    """(window, surface): a real MainWindow + a ControlSurface built on it, ready to
    `surface.dispatch(...)`.

    The surface is built with a SYNCHRONOUS GuiBridge shim so gui()-wrapped handlers
    run inline on the test (== GUI) thread AND propagate exceptions — the real
    post_and_wait would deadlock when called from the GUI thread. The window keeps its
    real GuiBridge; only the surface's gui() closure captures the shim.
    """
    from ferrodac.core.engine import Engine
    from ferrodac.core.manager import DeviceManager
    from ferrodac.core.registry import load_builtin_drivers
    from ferrodac.ui.app import MainWindow
    from ferrodac.ui.appcontrol import build_control_surface

    d = tempfile.mkdtemp()
    MainWindow._app_dir = lambda self, _d=d: _d          # isolate store/projects/tags
    engine = Engine()
    manager = DeviceManager(load_builtin_drivers(), engine=engine, registry=None)
    w = MainWindow(manager, engine)

    real_bridge = w._gui_bridge
    w._gui_bridge = types.SimpleNamespace(post_and_wait=lambda fn, **kw: fn())
    surface = build_control_surface(w)                   # captures the sync shim
    w._gui_bridge = real_bridge                          # window keeps the real one
    try:
        yield w, surface
    finally:
        w.close()
