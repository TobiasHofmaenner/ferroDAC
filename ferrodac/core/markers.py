"""Session time base + the marker model.

A **SessionClock** gives every panel one shared time origin, so a vertical line
drawn at instant T lands at the same place in *every* chart.

A **Marker** is a point in time with a note. The same primitive serves event
**tags** and record **start/stop** bookmarks; the `MarkerModel` is the single
source of truth, so all charts that render it stay in sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, Signal

TAG = "tag"
RECORDING = "recording"
# legacy point kinds (still parsed from old sessions)
REC_START = "record-start"
REC_STOP = "record-stop"

_KIND_COLOR = {TAG: "#ffd54f", RECORDING: "#ff6b6b",
               REC_START: "#69db7c", REC_STOP: "#ff6b6b"}
_KIND_LABEL = {RECORDING: "REC", REC_START: "REC", REC_STOP: "STOP"}


class SessionClock:
    """Maps absolute epoch time ↔ seconds since the session started."""

    def __init__(self, t0: float | None = None):
        self.t0 = time.time() if t0 is None else t0

    def rel(self, t: float) -> float:
        return t - self.t0

    def abs(self, x: float) -> float:
        return self.t0 + x

    def now(self) -> float:
        return time.time()


@dataclass
class Marker:
    id: str
    t: float                 # absolute epoch seconds (point, or region start)
    kind: str = TAG          # tag | recording
    label: str = ""
    comment: str = ""
    color: str = "#ffd54f"
    t_end: float | None = None    # region end (None = point, or live recording)
    run_dir: str | None = None    # for recordings: where the captured data lives

    @property
    def is_region(self) -> bool:
        return self.t_end is not None

    @property
    def duration(self) -> float:
        return (self.t_end - self.t) if self.t_end is not None else 0.0


class MarkerModel(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: dict[str, Marker] = {}
        self._counter = 0

    def add(self, t: float, label: str = "", comment: str = "", kind: str = TAG,
            color: str = None, mid: str = None, t_end: float = None,
            run_dir: str = None) -> str:
        if mid is None:
            self._counter += 1
            mid = f"m{self._counter}"
        else:
            tail = mid[1:] if mid.startswith("m") else ""
            if tail.isdigit():
                self._counter = max(self._counter, int(tail))
        color = color or _KIND_COLOR.get(kind, "#ffd54f")
        if not label:
            label = _KIND_LABEL.get(kind, f"T{self._counter}")
        self._markers[mid] = Marker(mid, float(t), kind, label, comment, color,
                                    t_end, run_dir)
        self.changed.emit()
        return mid

    def remove(self, mid: str) -> None:
        if self._markers.pop(mid, None) is not None:
            self.changed.emit()

    def move(self, mid: str, t: float) -> None:
        m = self._markers.get(mid)
        if m is not None:
            m.t = float(t)
            self.changed.emit()

    def update(self, mid: str, **fields) -> None:
        m = self._markers.get(mid)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        self.changed.emit()

    def get(self, mid: str) -> Marker | None:
        return self._markers.get(mid)

    def all(self) -> list[Marker]:
        return sorted(self._markers.values(), key=lambda m: m.t)

    def of_kind(self, kind: str) -> list[Marker]:
        return [m for m in self.all() if m.kind == kind]

    # -- serialization -------------------------------------------------------
    def to_list(self) -> list[dict]:
        return [{"id": m.id, "t": m.t, "kind": m.kind, "label": m.label,
                 "comment": m.comment, "color": m.color,
                 "t_end": m.t_end, "run_dir": m.run_dir} for m in self.all()]

    def from_list(self, data: list[dict]) -> None:
        self._markers.clear()
        self._counter = 0
        for d in data or []:
            mid = d.get("id")
            if mid is None:
                continue
            tail = mid[1:] if mid.startswith("m") else ""
            if tail.isdigit():
                self._counter = max(self._counter, int(tail))
            self._markers[mid] = Marker(
                mid, float(d["t"]), d.get("kind", TAG), d.get("label", ""),
                d.get("comment", ""), d.get("color", "#ffd54f"),
                d.get("t_end"), d.get("run_dir"))
        self.changed.emit()

    def clear(self) -> None:
        if self._markers:
            self._markers.clear()
            self.changed.emit()
