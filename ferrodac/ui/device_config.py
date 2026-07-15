"""Driver-supplied config GUIs — the contract a driver uses to ship a DEDICATED
config panel, beyond the declarative Options/Sinks the app renders for everyone.

Mirrors the display-`Widget` seam exactly, and for the same reason: a device driver
is Qt-free (so it can run headless on the agent), so it must NOT *be* a widget. Instead
a driver ships a SEPARATE, registered ``DeviceConfigWidget`` keyed by its ``driver``
name. Importing the widget module self-registers it (``@register_config_widget``), so
the loader picks it up for built-in AND external-plugin drivers identically — the plugin
declares the widget as one more ``module:Class`` provider entry.

The panel is AUGMENTING: the config dialog still renders the standard sections (name,
rate, declarative options, sinks); the driver panel is embedded below them. A panel can
set ``owns_options = True`` to suppress the auto-rendered options and draw them itself.

The panel never touches the device object or the manager internals — it talks through a
narrow :class:`DeviceConfigController`, the stable surface third-party panels code
against.
"""
from __future__ import annotations

from qtpy.QtWidgets import QWidget

# driver name -> DeviceConfigWidget subclass. Built-ins (below) and plugin widgets both
# add themselves via @register_config_widget; ConfigDialog looks the panel up by driver.
DEVICE_CONFIG_WIDGETS: dict = {}


def register_config_widget(driver=None):
    """Class decorator registering a :class:`DeviceConfigWidget` for a driver name.
    ``driver`` defaults to the class's ``driver`` attribute::

        @register_config_widget          # uses ShellyConfigWidget.driver
        class ShellyConfigWidget(DeviceConfigWidget):
            driver = "shelly_cloud"
    """
    def deco(cls):
        DEVICE_CONFIG_WIDGETS[driver or cls.driver] = cls
        return cls
    # allow bare @register_config_widget (driver read from the class)
    if isinstance(driver, type):
        cls, driver = driver, None
        return deco(cls)
    return deco


class DeviceConfigController:
    """The narrow, stable handle a driver config panel gets — scoped to one device.
    Routes through the manager (never the raw device), so panels stay on the same
    descriptor boundary as the rest of the UI."""

    def __init__(self, manager, instance_id: str):
        self._m = manager
        self._id = instance_id

    @property
    def instance_id(self) -> str:
        return self._id

    def descriptor(self):
        """A fresh DeviceDescriptor snapshot (identity + options + sources + status)."""
        return self._m.descriptor(self._id)

    def set_option(self, key: str, value) -> None:
        self._m.set_option(self._id, key, value)

    def set_rate(self, hz: float) -> None:
        self._m.set_rate(self._id, hz)

    def rename(self, name: str) -> None:
        self._m.rename(self._id, name)

    def check(self, on_result) -> None:
        """Run the device's connection check OFF the GUI thread; ``on_result`` is
        called with the CheckResult on the GUI thread when it completes."""
        self._m.check(self._id, on_result)


class DeviceConfigWidget(QWidget):
    """Base for a driver's dedicated config panel. Subclass, set ``driver`` to the
    target driver name, and build your UI in ``__init__`` using ``self.controller``."""

    driver = ""                 # registry key — matches the device's `driver`
    owns_options = False        # True → the dialog skips its auto-rendered Options

    def __init__(self, controller: DeviceConfigController, parent=None):
        super().__init__(parent)
        self.controller = controller

    def refresh(self, desc) -> None:
        """Re-sync from a fresh descriptor (called on build and on every active_changed).
        Default: nothing — override if the panel mirrors device state."""


# --------------------------------------------------------------------------- #
#  Built-in driver panels (importing this module registers them)
# --------------------------------------------------------------------------- #
from qtpy.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout  # noqa: E402


@register_config_widget
class ShellyConfigWidget(DeviceConfigWidget):
    """Shelly Cloud: a "Check connection" button that probes the account and reports
    whether auth worked and how many channels it will provide (the diagnostic the
    declarative server/key fields can't give on their own)."""

    driver = "shelly_cloud"

    def __init__(self, controller, parent=None):
        super().__init__(controller, parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        hdr = QLabel("Connection")
        hdr.setStyleSheet("font-weight:700; margin-top:2px;")
        lay.addWidget(hdr)
        row = QHBoxLayout()
        self._btn = QPushButton("Check connection")
        self._btn.clicked.connect(self._check)
        row.addWidget(self._btn)
        row.addStretch(1)
        lay.addLayout(row)
        self._status = QLabel("Set the server + auth key above, then check — it reports "
                              "whether auth worked and how many channels you'll get.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(self._status)

    def _check(self):
        self._btn.setEnabled(False)
        self._status.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._status.setText("Checking… (one moment — the cloud is rate-limited to ~1/s)")
        self.controller.check(self._show)

    def _show(self, result):
        self._btn.setEnabled(True)
        ok = bool(getattr(result, "ok", False))
        color = "#7fd18b" if ok else "#e0807f"
        mark = "✓" if ok else "✗"
        self._status.setStyleSheet(f"color:{color}; font-size:11px;")
        self._status.setText(f"{mark}  {getattr(result, 'summary', 'Check failed.')}")


# --------------------------------------------------------------------------- #
#  Python device: an in-app editor for the driver's poll(ctx) body.
# --------------------------------------------------------------------------- #
from qtpy.QtWidgets import QPlainTextEdit  # noqa: E402

_PY_SOURCE_HINT = (
    "Runs in-app on the poll thread — arbitrary Python, same trust as an extension "
    "(no sandbox). Define poll(ctx) -> value | dict; optional setup(ctx) and a "
    "SOURCES declaration.")


@register_config_widget("python_device")
class PythonDeviceConfigWidget(DeviceConfigWidget):
    """A "Python device" device runs a block of user Python on its poll thread; the
    code lives in an ``Option(key="code")``. The single-line option row the dialog
    renders for a normal Option can't hold a code block, so this panel *owns the
    options* (``owns_options = True`` → ConfigDialog skips its auto-rendered rows) and
    edits the code in a real multi-line editor.

    "Save & reload" commits the buffer via ``controller.set_option("code", …)`` — which
    recompiles/hot-swaps the code on the device (async_config). "Check / run once" runs
    the device's ``check()`` (poll once on the SAVED code) and shows the ``CheckResult``.
    ``last_error`` from the descriptor is surfaced so a compile/poll failure is visible
    without opening the log. ``refresh`` never clobbers an in-progress edit: it only
    resets the buffer when the editor isn't focused (so the LLM/config_set path and
    other external edits still land).
    """

    driver = "python_device"
    owns_options = True                 # ConfigDialog suppresses the generic option rows

    def __init__(self, controller, parent=None):
        super().__init__(controller, parent)
        self._shown_error = ""          # last last_error we surfaced (dedupe refreshes)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)

        hdr = QLabel("Code")
        hdr.setStyleSheet("font-weight:700; margin-top:2px;")
        lay.addWidget(hdr)

        self._editor = QPlainTextEdit()
        self._editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._editor.setMinimumHeight(240)
        self._editor.setPlaceholderText(
            "def poll(ctx):\n    return 3.14   # each returned value -> a Reading")
        self._editor.setStyleSheet(
            "QPlainTextEdit{background:#10141c;border:1px solid #232a38;border-radius:6px;"
            "font-family:'JetBrains Mono','Consolas',monospace;font-size:12px;}")
        lay.addWidget(self._editor, 1)

        self._hint = QLabel(_PY_SOURCE_HINT)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(self._hint)

        row = QHBoxLayout()
        self._save_btn = QPushButton("Save & reload")
        self._save_btn.setToolTip("Recompile and hot-swap the code on the running device")
        self._save_btn.clicked.connect(self._save)
        row.addWidget(self._save_btn)
        self._check_btn = QPushButton("Check / run once")
        self._check_btn.setToolTip("Run the SAVED code once (Save first to test edits)")
        self._check_btn.clicked.connect(self._check)
        row.addWidget(self._check_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(self._status)

    # -- actions (GUI thread) ------------------------------------------------
    def _save(self):
        # Let the next refresh re-surface whatever last_error the recompile produces.
        self._shown_error = ""
        self.controller.set_option("code", self._editor.toPlainText())
        self._set_status("Saved — reloading on the device…", ok=True)

    def _check(self):
        self._check_btn.setEnabled(False)
        self._set_status("Running once…", ok=None)
        self.controller.check(self._show)

    def _show(self, result):
        self._check_btn.setEnabled(True)
        ok = bool(getattr(result, "ok", False))
        self._set_status(getattr(result, "summary", "Check failed."), ok=ok)

    # -- sync from a fresh descriptor (build + every active_changed) ----------
    def refresh(self, desc):
        if desc is None:
            return
        code = self._code_of(desc)
        # Don't clobber an in-progress edit: only reset the buffer when the panel
        # isn't focused (external change — e.g. the LLM config_set path).
        if (code is not None and not self._editor.hasFocus()
                and code != self._editor.toPlainText()):
            self._editor.setPlainText(code)
        err = getattr(desc, "last_error", None) or ""
        if err != self._shown_error:
            self._shown_error = err
            if err:
                self._set_status(err, ok=False)
            elif not self._editor.hasFocus():
                self._set_status("", ok=None)   # error cleared

    @staticmethod
    def _code_of(desc):
        for opt in getattr(desc, "options", ()) or ():
            if getattr(opt, "key", None) == "code":
                return "" if opt.value is None else str(opt.value)
        return None

    def _set_status(self, text, ok):
        color = {True: "#7fd18b", False: "#e0807f", None: "#8b95a4"}[ok]
        mark = {True: "✓  ", False: "✗  ", None: ""}[ok]
        self._status.setStyleSheet(f"color:{color}; font-size:11px;")
        self._status.setText(f"{mark}{text}")
