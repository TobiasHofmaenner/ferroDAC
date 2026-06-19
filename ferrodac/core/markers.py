"""Session time base + the tag model (DESIGN §7.3).

A **SessionClock** gives every panel one shared time origin, so a vertical line
drawn at instant T lands at the same place in *every* chart.

The **Tag** entity itself (and its constants) lives in the Qt-free `core.tag`
module — re-exported here so existing imports keep working. This module adds the
Qt model on top: the **TagStore** (`MarkerModel`), a live, id-keyed collection
that merges last-write-wins by version, tombstones deletes, and signals charts /
the event log to re-render. Class names stay `Marker`/`MarkerModel` (no churn for
the many UI call sites); `Tag`/`TagStore` are the §7.3 aliases.
"""

from __future__ import annotations

import time
import uuid as _uuid

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtCore import QObject, Signal

# Re-export the Qt-free entity + constants so `from ..core.markers import …`
# (Marker, RECORDING, ORIGIN_*, …) keeps resolving exactly as before.
from .tag import (  # noqa: F401
    TAG, RECORDING, REC_START, REC_STOP,
    ORIGIN_USER, ORIGIN_DEVICE, ORIGIN_PROCESSOR, ORIGIN_SYSTEM,
    SEVERITIES, Marker, Tag, color_for, marker_from_dict, marker_to_dict,
    _KIND_LABEL, _SEVERITY_COLOR, _KIND_COLOR)


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
        self.default_projects: list = []     # new tags are filed under these (the
        #                                      active project) unless add() overrides

    # -- local mutations (user/recorder; bump version + announce locally) ----
    def add(self, t: float, label: str = "", comment: str = "", kind: str = TAG,
            color: str = None, mid: str = None, t_end: float = None,
            run_dir: str = None, origin_kind: str = ORIGIN_USER,
            origin_id: str = "", scope: str = "global",
            severity: str = "info", payload: dict = None,
            projects: list = None) -> str:
        if mid is None:
            mid = _uuid.uuid4().hex
        self._label_counter += 1
        color = color or color_for(kind, severity)
        if not label:
            label = _KIND_LABEL.get(kind, f"T{self._label_counter}")
        projs = list(projects) if projects is not None else list(self.default_projects)
        m = Marker(mid, float(t), kind, label, comment, color, t_end, run_dir,
                   origin_kind, origin_id, scope, severity, dict(payload or {}), projs)
        self._markers[mid] = m
        self._local(mid)
        return mid

    def add_to_project(self, mid: str, pid: str) -> None:
        m = self._markers.get(mid)
        if m is not None and pid and pid not in m.projects:
            m.projects = list(m.projects) + [pid]
            self._local(mid)

    def remove_from_project(self, mid: str, pid: str) -> None:
        m = self._markers.get(mid)
        if m is not None and pid in (m.projects or []):
            m.projects = [p for p in m.projects if p != pid]
            self._local(mid)

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

    def raw(self, mid: str) -> "Marker | None":
        """The marker by id INCLUDING tombstones (unlike get()). The hub-sync
        glue needs this to publish a just-deleted tag's tombstone."""
        return self._markers.get(mid)

    def snapshot(self) -> list[Marker]:
        """Every marker, tombstones included — what to push to a hub on connect
        so it converges on both our live tags and our offline deletes."""
        return list(self._markers.values())

    # -- serialization (persists tombstones so offline deletes still sync) ---
    def to_list(self) -> list[dict]:
        return [marker_to_dict(m)
                for m in sorted(self._markers.values(), key=lambda m: m.t)]

    def from_list(self, data: list[dict]) -> None:
        self._markers.clear()
        self._label_counter = 0
        for d in data or []:
            m = marker_from_dict(d)
            if m is not None:
                self._markers[m.id] = m
        self.changed.emit()

    def clear(self) -> None:
        if self._markers:
            self._markers.clear()
            self.changed.emit()


# §7.3 vocabulary alias — the canonical model name stays MarkerModel so existing
# UI call sites are untouched; new (net/hub) code speaks TagStore.
TagStore = MarkerModel
