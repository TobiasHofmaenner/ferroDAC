"""Select the Qt binding before qtpy is imported. Prefer PySide6, fall back to
any other importable binding so the app runs wherever a Qt is installed."""

from __future__ import annotations

import os

_CANDIDATES = [
    ("pyside6", "PySide6.QtWidgets"),
    ("pyqt6", "PyQt6.QtWidgets"),
    ("pyside2", "PySide2.QtWidgets"),
    ("pyqt5", "PyQt5.QtWidgets"),
]


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def select_binding() -> str | None:
    if os.environ.get("QT_API"):
        return os.environ["QT_API"]
    for api, module in _CANDIDATES:
        if _importable(module):
            os.environ["QT_API"] = api
            return api
    return None


BINDING = select_binding()
