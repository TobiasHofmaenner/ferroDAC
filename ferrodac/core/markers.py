"""Session time base + the tag model (DESIGN §7.3).

A **SessionClock** gives every panel one shared time origin, so a vertical line
drawn at instant T lands at the same place in *every* chart.

A **Tag** (historically a *Marker*) is a discrete, timestamped, semantic event —
distinct from a Source's continuous *metrics*. The same primitive serves user
event tags, record start/stop bookmarks, and (Phase 6) device/processor-emitted
alarms. Unlike readings, tags are **reliable, editable and durable**: they merge
across instances **by id, last-write-wins on `version`**, with tombstones so a
delete propagates. The `TagStore` is the single source of truth; every chart and
the event log render its `changed` signal.

`Marker`/`MarkerModel` remain the canonical class names (no churn for the many
UI call sites); `Tag`/`TagStore` are aliases for the §7.3 vocabulary.
"""

from __future__ import annotations

import time
import uuid as _uuid
from dataclasses import dataclass, field

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, Signal

TAG = "tag"
RECORDING = "recording"
# legacy point kinds (still parsed from old sessions)
REC_START = "record-start"
REC_STOP = "record-stop"

# origin.kind — provenance of a tag (DESIGN §7.3); the "who emitted it" axis
ORIGIN_USER = "user"
ORIGIN_DEVICE = "device"
ORIGIN_PROCESSOR = "processor"
ORIGIN_SYSTEM = "system"

# severity — a small CLOSED enum (kind stays an open string)
SEVERITIES = ("info", "warn", "error", "critical")

_KIND_COLOR = {TAG: "#ffd54f", RECORDING: "#ff6b6b",
               REC_START: "#69db7c", REC_STOP: "#ff6b6b"}
_KIND_LABEL = {RECORDING: "REC", REC_START: "REC", REC_STOP: "STOP"}
# severity tints the marker when the kind doesn't pin a colour itself
_SEVERITY_COLOR = {"warn": "#ffa94d", "error": "#ff6b6b", "critical": "#f03e3e"}


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
    """A tag/event on the shared session clock. See module docstring + §7.3."""
    id: str                       # UUID hex — globally unique → cross-instance merge
    t: float                      # absolute epoch seconds (point, or region start)
    kind: str = TAG               # OPEN string: tag|recording|alarm|calibration|…
    label: str = ""
    comment: str = ""
    color: str = "#ffd54f"
    t_end: float | None = None    # region end (None = point, or live recording)
    run_dir: str | None = None    # for recordings: where the captured data lives
    # -- tag-overhaul fields (DESIGN §7.3) -----------------------------------
    origin_kind: str = ORIGIN_USER    # user|device|processor|system
    origin_id: str = ""               # which user/device/processor emitted it
    scope: str = "global"             # global | device:<uuid> | source:<key>
    severity: str = "info"            # info|warn|error|critical (closed enum)
    payload: dict = field(default_factory=dict)   # open machine-readable map
    version: int = 1                  # LWW: higher wins on merge by id
    deleted: bool = False             # tombstone — propagates a delete across peers

    @property
    def is_region(self) -> bool:
        return self.t_end is not None

    @property
    def duration(self) -> float:
        return (self.t_end - self.t) if self.t_end is not None else 0.0


class MarkerModel(QObject):
    """The TagStore: id-keyed, LWW-by-version, tombstoned.

    Signals:
      * ``changed``      — any mutation (UI re-render; coarse, no-arg).
      * ``tag_changed``  — a *local* create/edit, by id → the hub-sync glue
                           publishes it. NOT fired for remote ``upsert`` (so the
                           sync never echoes a tag back to the hub).
      * ``tag_removed``  — a *local* delete, by id → glue issues a DeleteTag.
    """

    changed = Signal()
    tag_changed = Signal(str)
    tag_removed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: dict[str, Marker] = {}
        self._label_counter = 0

    # -- local mutations (user/recorder; bump version + announce locally) ----
    def add(self, t: float, label: str = "", comment: str = "", kind: str = TAG,
            color: str = None, mid: str = None, t_end: float = None,
            run_dir: str = None, origin_kind: str = ORIGIN_USER,
            origin_id: str = "", scope: str = "global",
            severity: str = "info", payload: dict = None) -> str:
        if mid is None:
            mid = _uuid.uuid4().hex
        self._label_counter += 1
        color = color or _SEVERITY_COLOR.get(severity) \
            or _KIND_COLOR.get(kind, "#ffd54f")
        if not label:
            label = _KIND_LABEL.get(kind, f"T{self._label_counter}")
        m = Marker(mid, float(t), kind, label, comment, color, t_end, run_dir,
                   origin_kind, origin_id, scope, severity, dict(payload or {}))
        self._markers[mid] = m
        self._local(mid)
        return mid

    def remove(self, mid: str) -> None:
        """Tombstone (not a hard drop) so the delete propagates across peers."""
        m = self._markers.get(mid)
        if m is None or m.deleted:
            return
        m.deleted = True
        m.version += 1
        self.changed.emit()
        self.tag_removed.emit(mid)

    def move(self, mid: str, t: float) -> None:
        m = self._markers.get(mid)
        if m is not None:
            m.t = float(t)
            self._local(mid)

    def update(self, mid: str, **fields) -> None:
        m = self._markers.get(mid)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        self._local(mid)

    def _local(self, mid: str) -> None:
        """A local create/edit: bump version, re-render, and offer to the hub."""
        m = self._markers.get(mid)
        if m is not None:
            m.version += 1
        self.changed.emit()
        self.tag_changed.emit(mid)

    # -- remote merge (hub-sync glue; LWW, no re-publish) --------------------
    def upsert(self, m: Marker) -> bool:
        """Merge a tag from a peer. Last-write-wins on ``version`` (ties accept
        the incoming write). Returns True if it changed our state. Emits only
        ``changed`` (never ``tag_changed``) so it does not echo back to the hub."""
        cur = self._markers.get(m.id)
        if cur is not None and m.version < cur.version:
            return False                         # stale — ignore
        if cur is not None and m.version == cur.version and not m.deleted \
                and not cur.deleted:
            return False                         # idempotent same-version upsert
        self._markers[m.id] = m
        self.changed.emit()
        return True

    # -- queries -------------------------------------------------------------
    def get(self, mid: str) -> Marker | None:
        m = self._markers.get(mid)
        return None if (m is not None and m.deleted) else m

    def all(self) -> list[Marker]:
        return sorted((m for m in self._markers.values() if not m.deleted),
                      key=lambda m: m.t)

    def of_kind(self, kind: str) -> list[Marker]:
        return [m for m in self.all() if m.kind == kind]

    # -- serialization (persists tombstones so offline deletes still sync) ---
    def to_list(self) -> list[dict]:
        return [self._to_dict(m)
                for m in sorted(self._markers.values(), key=lambda m: m.t)]

    @staticmethod
    def _to_dict(m: Marker) -> dict:
        return {"id": m.id, "t": m.t, "kind": m.kind, "label": m.label,
                "comment": m.comment, "color": m.color, "t_end": m.t_end,
                "run_dir": m.run_dir, "origin_kind": m.origin_kind,
                "origin_id": m.origin_id, "scope": m.scope,
                "severity": m.severity, "payload": m.payload,
                "version": m.version, "deleted": m.deleted}

    @staticmethod
    def marker_from_dict(d: dict) -> "Marker | None":
        mid = d.get("id")
        if mid is None:
            return None
        return Marker(
            mid, float(d["t"]), d.get("kind", TAG), d.get("label", ""),
            d.get("comment", ""), d.get("color", "#ffd54f"),
            d.get("t_end"), d.get("run_dir"),
            d.get("origin_kind", ORIGIN_USER), d.get("origin_id", ""),
            d.get("scope", "global"), d.get("severity", "info"),
            dict(d.get("payload") or {}), int(d.get("version", 1)),
            bool(d.get("deleted", False)))

    def from_list(self, data: list[dict]) -> None:
        self._markers.clear()
        self._label_counter = 0
        for d in data or []:
            m = self.marker_from_dict(d)
            if m is not None:
                self._markers[m.id] = m
        self.changed.emit()

    def clear(self) -> None:
        if self._markers:
            self._markers.clear()
            self.changed.emit()


# §7.3 vocabulary aliases — the canonical names stay Marker/MarkerModel so the
# existing UI call sites are untouched; new (net/hub) code speaks Tag/TagStore.
Tag = Marker
TagStore = MarkerModel
