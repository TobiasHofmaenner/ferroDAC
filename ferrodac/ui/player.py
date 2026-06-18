"""A minimal, dockable transport for the shared replay head (DESIGN §7.4).

The Timeline window is the full scrubber; this is the always-available player in
the main dashboard, so the head is controllable (play / pause / step / jump to
now) without opening the Timeline. It's a pure controller + view of the shared
`TimeContext` — the app owns the heartbeat that ticks the clock, so this never
double-drives it.
"""

from __future__ import annotations

import time

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy import QtWidgets

_MUTED = "#7f8a99"


class PlayerBar(QtWidgets.QWidget):
    """⏮ step back · ▶/⏸ · ⏭ step forward · ● Now · speed · position readout."""

    def __init__(self, time_context, parent=None):
        super().__init__(parent)
        self.tc = time_context
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(6)
        mk = lambda t, fn, tip="": (b := QtWidgets.QToolButton(text=t),
                                    b.setToolTip(tip), b.clicked.connect(fn), b)[0]
        lay.addWidget(mk("⏮", self._back, "Step back"))
        self._play_btn = mk("▶", self._toggle_play, "Play / pause")
        lay.addWidget(self._play_btn)
        lay.addWidget(mk("⏭", self._fwd, "Step forward"))
        self._now_btn = QtWidgets.QToolButton(text="● Now", checkable=True)
        self._now_btn.setToolTip("Jump to live")
        self._now_btn.clicked.connect(lambda: self._set_live(self._now_btn.isChecked()))
        lay.addWidget(self._now_btn)
        self._speed = QtWidgets.QComboBox()
        self._speed.addItems(["1×", "4×", "30×", "120×"])
        self._speed.setCurrentText("30×")
        self._speed.currentTextChanged.connect(
            lambda t: setattr(self.tc, "speed", float(t.rstrip("×"))))
        lay.addWidget(self._speed)
        lay.addStretch(1)
        self._readout = QtWidgets.QLabel("")
        self._readout.setStyleSheet(f"color:{_MUTED};")
        lay.addWidget(self._readout)
        self._tc_unsub = self.tc.subscribe(self._sync)
        self._sync()

    # -- controls (drive the shared head) ------------------------------------
    def _back(self):
        self.tc.park(self.tc.head - self.tc.width / 2)

    def _fwd(self):
        self.tc.park(self.tc.head + self.tc.width / 2)    # park clamps to now

    def _toggle_play(self):
        if self.tc.playing:
            self.tc.playing = False
        else:
            if self.tc.following:                         # nothing ahead → park first
                self.tc.park(self.tc.head)
            self.tc.playing = True
        self._sync()

    def _set_live(self, on):
        if on:
            self.tc.follow_now()
        elif self.tc.following:
            self.tc.park(self.tc.head)
        self._sync()

    # -- view (reflect the shared head) --------------------------------------
    def _sync(self):
        self._play_btn.setText("⏸" if self.tc.playing else "▶")
        self._now_btn.blockSignals(True)
        self._now_btn.setChecked(self.tc.following)
        self._now_btn.blockSignals(False)
        txt = f"{self.tc.speed:.0f}×"
        if self._speed.currentText() != txt:
            i = self._speed.findText(txt)
            if i >= 0:
                self._speed.blockSignals(True)
                self._speed.setCurrentIndex(i)
                self._speed.blockSignals(False)
        if self.tc.following:
            self._readout.setText("● LIVE")
        else:
            dt = time.time() - self.tc.head
            tag = f"-{dt / 60:.1f} min" if dt > 1 else "now"
            extra = f" · ▶ {self.tc.rate:.0f}×" if self.tc.playing else ""
            self._readout.setText(
                time.strftime("%H:%M:%S", time.localtime(self.tc.head)) + f"  {tag}{extra}")

    def closeEvent(self, ev):
        try:
            self._tc_unsub()
        except Exception:
            pass
        super().closeEvent(ev)
