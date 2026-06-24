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
