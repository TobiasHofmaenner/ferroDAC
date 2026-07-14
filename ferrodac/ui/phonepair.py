"""Phone-pairing dialog + LAN helpers for the phone companion (upload-from-phone).

The user picks "Connect a phone…", we start the companion web server on the LAN and
show a QR code they scan with a phone camera. The QR encodes a CAPABILITY URL — the
companion's base URL plus a pre-shared key (``/enter?k=<psk>``) that authenticates the
phone. Everything is plain HTTP on the local network, so a persistent UNENCRYPTED
warning is always shown.

``lan_ip`` and ``qr_pixmap`` are pure helpers (stdlib + segno); ``PairPhoneDialog`` is
the only Qt piece. segno is a pure-Python, frozen-friendly QR encoder; if it is missing
the QR degrades to a text hint so pairing still works by typing the URL by hand.
"""

from __future__ import annotations

import io
import socket

from qtpy.QtCore import Qt
from qtpy.QtGui import QPixmap
from qtpy.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

# Shown verbatim on the phone page too — keep the wording in sync.
WARN_TEXT = ("⚠ Unencrypted connection — anyone on this network could view what you "
             "send. Fine on a trusted network.")


def lan_ip() -> str:
    """Best-effort LAN IPv4 of this machine — the address a phone on the same WiFi can
    reach. Opens a throwaway UDP socket 'toward' a public IP so the OS selects the right
    outbound interface; no packet is actually sent. Falls back to loopback on error."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def qr_pixmap(data: str, *, scale: int = 6, border: int = 4) -> QPixmap:
    """Render ``data`` as a QR-code ``QPixmap`` via segno (pure-Python). Returns a NULL
    QPixmap only when segno is unavailable — callers should fall back to a text label.
    Needs a running QGuiApplication (as any QPixmap does)."""
    try:
        import segno
    except Exception:                       # noqa: BLE001 — optional/frozen dep
        return QPixmap()
    buf = io.BytesIO()
    segno.make(data).save(buf, kind="png", scale=scale, border=border)
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm


class PairPhoneDialog(QDialog):
    """Show a scannable QR + URL to open the phone companion, with Regenerate/Revoke.

    ``url`` is the companion BASE url (e.g. ``http://192.168.1.20:8000``); the capability
    URL the phone opens is ``url + '/enter?k=' + psk``. ``on_regenerate()`` should revoke
    the old key, mint a fresh psk and RETURN it (the dialog re-renders the QR);
    ``on_revoke()`` tears the pairing down. Both are plain callables so this dialog stays
    Qt-only and logic-free. Non-modal + delete-on-close, mirroring ConnectionsWindow — the
    caller connects ``finished`` to stop the server.
    """

    def __init__(self, parent=None, *, url, psk, on_regenerate, on_revoke):
        super().__init__(parent)
        self._base = str(url).rstrip("/")
        self._psk = psk
        self._on_regenerate = on_regenerate
        self._on_revoke = on_revoke
        self.setWindowTitle("ferroDAC — Connect a phone")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setStyleSheet("QDialog{background:#0e1116;color:#c7d0db;}"
                           "QLabel{color:#c7d0db;}"
                           "QPushButton{background:#171c26;border:1px solid #232a38;"
                           "border-radius:6px;padding:6px 12px;color:#c7d0db;}"
                           "QPushButton:hover{background:#1d2430;}")

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        head = QLabel("Scan to connect a phone")
        head.setStyleSheet("font-size:15px; font-weight:700;")
        root.addWidget(head)

        instr = QLabel("Scan with your phone camera, on the same WiFi.")
        instr.setStyleSheet("color:#8b95a4; font-size:12px;")
        instr.setWordWrap(True)
        root.addWidget(instr)

        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.setMinimumSize(260, 260)
        self._qr.setStyleSheet("background:#ffffff; border-radius:8px; padding:8px;")
        root.addWidget(self._qr, 0, Qt.AlignCenter)

        self._url = QLabel()
        self._url.setAlignment(Qt.AlignCenter)
        self._url.setWordWrap(True)
        self._url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._url.setStyleSheet("font-family:monospace; color:#7fb0ff; font-size:12px;")
        root.addWidget(self._url)

        warn = QLabel(WARN_TEXT)
        warn.setWordWrap(True)
        warn.setStyleSheet("background:#3a2a12; color:#f2c66b; border:1px solid #6b4f1f;"
                           "border-radius:8px; padding:8px; font-weight:600;")
        root.addWidget(warn)

        btns = QHBoxLayout()
        regen = QPushButton("Regenerate")
        regen.clicked.connect(self._regenerate)
        revoke = QPushButton("Revoke")
        revoke.clicked.connect(self._revoke)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(regen)
        btns.addWidget(revoke)
        btns.addStretch(1)
        btns.addWidget(close)
        root.addLayout(btns)

        self._render()

    # -- helpers -------------------------------------------------------------
    def _cap_url(self) -> str:
        return f"{self._base}/enter?k={self._psk}"

    def _render(self) -> None:
        cap = self._cap_url()
        pm = qr_pixmap(cap)
        if pm.isNull():
            self._qr.setText("QR unavailable —\ntype the URL below on your phone.")
            self._qr.setStyleSheet("color:#333; background:#ffffff; border-radius:8px;"
                                   "padding:8px;")
        else:
            self._qr.setPixmap(pm)
        self._url.setText(cap)

    def _regenerate(self) -> None:
        try:
            new_psk = self._on_regenerate()
        except Exception:                   # noqa: BLE001 — a failed regen ≠ crash
            new_psk = None
        if new_psk:
            self._psk = new_psk
            self._render()

    def _revoke(self) -> None:
        try:
            self._on_revoke()
        except Exception:                   # noqa: BLE001
            pass
        self.accept()                       # the QR is dead now → close
