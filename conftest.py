"""Pytest bootstrap for the whole repo.

Makes the existing ad-hoc self-tests and the server's gRPC e2e scripts runnable
under one `pytest` invocation: puts the repo root, the hub package and the
generated contract stubs on the path, and forces headless Qt so UI smoke tests
run without a display.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Pin the SAME Qt binding the app uses (ferrodac._qtbinding prefers PySide6), so
# tests exercise the real binding — and so QtWebEngine (PySide6-only here) is found.
os.environ.setdefault("QT_API", "pyside6")

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT,
           os.path.join(_ROOT, "server"),        # hub.* (server-side servicer)
           os.path.join(_ROOT, "server", "gen"),  # ferrodac_contract.* stubs
           os.path.join(_ROOT, "server", "tests")):  # the e2e scripts, by name
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import pytest


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the UI smoke tests."""
    from qtpy.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    # Mirror the app's segfault guard (ferrodac-segfault-gc): cyclic GC of a
    # QObject-with-timer on a non-GUI thread corrupts Qt → SIGSEGV. UI tests spin
    # real worker threads (hub agent/viewer/docs sync), so without this the cyclic
    # collector can fire on one of them and crash teardown. gc.disable() + a
    # GUI-thread collector is exactly what app.main() installs.
    try:
        from ferrodac.diagnostics import install_gui_thread_gc
        install_gui_thread_gc()
    except Exception:                       # noqa: BLE001 — guard is best-effort
        pass
    yield app


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """QtWebEngine's C++ teardown during interpreter finalisation segfaults on
    headless runners even though every test passed (the crash is purely in Qt
    finalisation, after the summary). Once the session is over and reported, exit
    immediately with the real status, skipping that crashy teardown — but ONLY when
    a QApplication was actually created (UI runs), so non-Qt jobs exit normally."""
    try:
        from qtpy.QtWidgets import QApplication
        if QApplication.instance() is None:
            return
    except Exception:                       # noqa: BLE001 — no Qt → normal exit
        return
    import os
    import sys
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:                       # noqa: BLE001
        pass
    os._exit(int(exitstatus))
