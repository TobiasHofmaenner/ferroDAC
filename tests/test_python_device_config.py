"""Headless tests for the Python-source config panel (offscreen Qt).

These never touch a real device: the panel talks only through a
``DeviceConfigController`` surface, so a tiny fake controller is enough to assert the
wiring — Save writes the ``code`` option, Check calls ``controller.check``, and
``refresh`` mirrors the descriptor (code + last_error) into the widget.
"""
import types

import pytest

pytest.importorskip("qtpy")


class _FakeController:
    """Records the two calls the panel is allowed to make."""

    def __init__(self):
        self.options = []      # list of (key, value) from set_option
        self.checks = []       # list of on_result callbacks from check

    def set_option(self, key, value):
        self.options.append((key, value))

    def check(self, on_result):
        self.checks.append(on_result)


def _widget(qapp):
    from ferrodac.ui.device_config import PythonDeviceConfigWidget
    return PythonDeviceConfigWidget(_FakeController())


@pytest.mark.ui
def test_registered_owns_options(qapp):
    from ferrodac.ui.device_config import (DEVICE_CONFIG_WIDGETS,
                                           PythonDeviceConfigWidget)
    assert DEVICE_CONFIG_WIDGETS["python_device"] is PythonDeviceConfigWidget
    assert PythonDeviceConfigWidget.owns_options is True
    assert PythonDeviceConfigWidget.driver == "python_device"


@pytest.mark.ui
def test_builds_against_fake_controller(qapp):
    w = _widget(qapp)
    try:
        assert w._editor is not None
        assert w._save_btn is not None and w._check_btn is not None
    finally:
        w.deleteLater()


@pytest.mark.ui
def test_save_sets_code_option(qapp):
    w = _widget(qapp)
    try:
        code = "def poll(ctx):\n    return 1.0\n"
        w._editor.setPlainText(code)
        w._save_btn.click()                       # exercise the real signal wiring
        assert w.controller.options == [("code", code)]
    finally:
        w.deleteLater()


@pytest.mark.ui
def test_check_calls_controller_check(qapp):
    w = _widget(qapp)
    try:
        w._check_btn.click()
        assert len(w.controller.checks) == 1
        cb = w.controller.checks[0]
        assert callable(cb)
        # the on_result callback runs on the GUI thread and updates the status
        cb(types.SimpleNamespace(ok=True, summary="Ran OK · 1 source"))
        assert "1 source" in w._status.text()
    finally:
        w.deleteLater()


@pytest.mark.ui
def test_refresh_loads_code_when_unfocused(qapp):
    w = _widget(qapp)
    try:
        desc = types.SimpleNamespace(
            options=[types.SimpleNamespace(key="code", value="poll = lambda c: 2")],
            last_error=None)
        w.refresh(desc)
        assert w._editor.toPlainText() == "poll = lambda c: 2"
    finally:
        w.deleteLater()


@pytest.mark.ui
def test_refresh_surfaces_last_error(qapp):
    w = _widget(qapp)
    try:
        desc = types.SimpleNamespace(
            options=[types.SimpleNamespace(key="code", value="x")],
            last_error="NameError: name 'q' is not defined")
        w.refresh(desc)
        assert "NameError" in w._status.text()
        # a second identical refresh is a no-op (dedupe), doesn't raise
        w.refresh(desc)
    finally:
        w.deleteLater()
