"""Pytest bootstrap for the whole repo.

Makes the existing ad-hoc self-tests and the server's gRPC e2e scripts runnable
under one `pytest` invocation: puts the repo root, the hub package and the
generated contract stubs on the path, and forces headless Qt so UI smoke tests
run without a display.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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
    yield app
