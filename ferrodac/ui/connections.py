"""External-control UI: the pairing-approval popup + the Connections manager.

PairingDialog is the human trust anchor — an external connector asks to control the
app, the user sees its name + a verification code (to confirm it's the right client)
and grants a scope (read / control / admin). ConnectionsWindow enables/disables the
loopback API, shows the port, lists paired connectors (name, scope, last seen), and
revokes them.
"""

from __future__ import annotations

import time

from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
                            QMainWindow, QPushButton, QScrollArea, QVBoxLayout, QWidget)


class PairingDialog(QDialog):
    """Approve/deny an external connector, choosing its scope."""

    def __init__(self, pairing, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ferroDAC — control-API request")
        self.setModal(True)
        self._decision = None                 # (approve: bool, scope: str | None)
        lay = QVBoxLayout(self)

        head = QLabel(f"“{pairing.name}” wants to control ferroDAC")
        head.setStyleSheet("font-size:14px; font-weight:700;")
        head.setWordWrap(True)
        lay.addWidget(head)

        code = QLabel(pairing.code)
        code.setAlignment(Qt.AlignCenter)
        code.setStyleSheet("font-size:26px; font-weight:800; letter-spacing:6px;"
                           " color:#c7d0db; margin:6px;")
        lay.addWidget(QLabel("Verification code:"))
        lay.addWidget(code)

        note = QLabel("Confirm this code matches the one shown by the connector, then "
                      "choose what it may do. You can revoke it any time in Connections.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#8b95a4; font-size:11px;")
        lay.addWidget(note)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Grant:"))
        self._scope = QComboBox()
        self._scope.addItem("Read only — observe", "read")
        self._scope.addItem("Control — read + commands", "control")
        self._scope.addItem("Admin — + destructive actions", "admin")
        i = self._scope.findData(pairing.scope)
        self._scope.setCurrentIndex(i if i >= 0 else 1)     # default: control
        srow.addWidget(self._scope, 1)
        lay.addLayout(srow)

        btns = QHBoxLayout()
        btns.addStretch(1)
        deny = QPushButton("Deny")
        deny.clicked.connect(self._deny)
        appr = QPushButton("Approve")
        appr.setDefault(True)
        appr.clicked.connect(self._approve)
        btns.addWidget(deny)
        btns.addWidget(appr)
        lay.addLayout(btns)

    def _approve(self):
        self._decision = (True, self._scope.currentData())
        self.accept()

    def _deny(self):
        self._decision = (False, None)
        self.reject()

    @property
    def decision(self):
        return self._decision


class ConnectionsWindow(QMainWindow):
    """Enable the loopback control API + manage paired connectors."""

    def __init__(self, registry, *, is_running, on_toggle, get_port, parent=None):
        super().__init__(parent)
        self._reg = registry
        self._is_running = is_running          # () -> bool
        self._on_toggle = on_toggle            # (enable: bool) -> None
        self._get_port = get_port              # () -> int
        self.setWindowTitle("ferroDAC — Connections")
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.resize(460, 560)
        self.setStyleSheet("QMainWindow,QWidget{background:#0e1116;color:#c7d0db;}"
                           "QScrollArea{border:none;}")
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self._enable = QCheckBox("Enable the local control API (127.0.0.1 only)")
        self._enable.setChecked(self._is_running())
        self._enable.toggled.connect(self._toggle)
        root.addWidget(self._enable)
        self._status = QLabel()
        self._status.setStyleSheet("color:#8b95a4; font-size:11px;")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        hdr = QLabel("Paired connectors")
        hdr.setStyleSheet("font-weight:700; margin-top:6px;")
        root.addWidget(hdr)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        self._list = QVBoxLayout(host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(6)
        self._list.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._timer = QTimer(self)             # live-refresh last-seen + new pairings
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _toggle(self, on):
        self._on_toggle(bool(on))
        self.refresh()

    def refresh(self):
        running = self._is_running()
        if self._enable.isChecked() != running:
            self._enable.blockSignals(True)
            self._enable.setChecked(running)
            self._enable.blockSignals(False)
        self._status.setText(
            f"Listening on http://127.0.0.1:{self._get_port()}  ·  connectors pair with "
            "an approval prompt. Off = the port is closed."
            if running else "Off — no port is open. Enable to let a local connector pair.")
        # rebuild the connector cards
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        conns = self._reg.list()
        for c in sorted(conns, key=lambda x: x["name"]):
            self._list.insertWidget(self._list.count() - 1, self._card(c))
        if not conns:
            empty = QLabel("No connectors paired yet.")
            empty.setStyleSheet("color:#8b95a4;")
            self._list.insertWidget(self._list.count() - 1, empty)

    def _card(self, c) -> QWidget:
        f = QFrame()
        f.setStyleSheet("QFrame{background:#171c26;border:1px solid #232a38;"
                        "border-radius:8px;}")
        lay = QHBoxLayout(f)
        seen = ("never" if not c["last_seen"]
                else f"{max(0, int(time.time() - c['last_seen']))}s ago")
        info = QLabel(f"<b>{c['name']}</b><br><span style='color:#8b95a4;font-size:11px'>"
                      f"{c['scope']} · seen {seen}</span>")
        lay.addWidget(info, 1)
        rev = QPushButton("Revoke")
        rev.clicked.connect(lambda _=False, cid=c["id"]: self._revoke(cid))
        lay.addWidget(rev)
        return f

    def _revoke(self, cid):
        self._reg.revoke(cid)
        self.refresh()

    def closeEvent(self, ev):  # noqa: N802
        self._timer.stop()
        super().closeEvent(ev)
