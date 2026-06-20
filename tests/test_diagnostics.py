"""The crash / threading diagnostics turn a bare segfault (or an off-thread Qt
call) into a readable trace (ferrodac.diagnostics). Run in subprocesses so the
process-global handlers + a deliberate SIGSEGV don't touch the test runner.
"""

import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(prog: str):
    return subprocess.run([sys.executable, "-c", prog],
                          capture_output=True, text=True, cwd=_ROOT)


def test_faulthandler_dumps_python_traceback_on_segfault():
    prog = ('import sys; sys.path.insert(0, ".");'
            'from ferrodac.diagnostics import _install_faulthandler as f; f("");'
            'import faulthandler; faulthandler._sigsegv()')   # std-lib SIGSEGV trigger
    r = _run(prog)
    assert r.returncode != 0                              # killed by the signal
    assert "Segmentation fault" in r.stderr               # …but now with a trace
    assert "most recent call first" in r.stderr           # the Python stack


@pytest.mark.ui
def test_qt_thread_guard_flags_off_thread_timer():
    pytest.importorskip("qtpy")
    prog = (
        'import os, sys, threading, time;'
        'os.environ["QT_QPA_PLATFORM"] = "offscreen"; sys.path.insert(0, ".");'
        'from qtpy.QtWidgets import QApplication; app = QApplication([]);'
        'from ferrodac.diagnostics import install; install("");'
        'from qtpy.QtCore import QTimer;'
        'threading.Thread(target=lambda: QTimer().start(50),'
        ' name="hub-projects", daemon=True).start(); time.sleep(0.4)')
    out = _run(prog).stderr
    # the exact bug: a QTimer started on a raw worker thread → flagged with origin
    assert "Timers can only be used" in out
    assert "off the GUI thread: True" in out
    assert "hub-projects" in out                          # the offending thread named
