"""The Tag entity — a first-class, Qt-free datatype (DESIGN §7.3).

A **Tag** (historically a *Marker*) is a discrete, timestamped, semantic event:
the opposite of a Source's continuous, expendable metric. It is reliable,
editable, and merges across instances **by id, last-write-wins on `version`**,
with tombstones so deletes propagate.

This module is deliberately **Qt-free** so the net layer (`ferrodac.net`, which
runs grpc.aio in worker threads) can convert tags without importing a GUI
toolkit. The Qt model that owns a live collection of these — the `TagStore` —
lives in `markers.py` and re-exports everything here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TAG = "tag"
RECORDING = "recording"
# legacy point kinds (still parsed from old sessions)
REC_START = "record-start"
REC_STOP = "record-stop"

# origin.kind — provenance of a tag (the "who emitted it" axis)
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


def color_for(kind: str, severity: str = "info") -> str:
    """The render colour: severity tint wins, else the kind's colour, else gold."""
    return _SEVERITY_COLOR.get(severity) or _KIND_COLOR.get(kind, "#ffd54f")


def marker_to_dict(m: Marker) -> dict:
    return {"id": m.id, "t": m.t, "kind": m.kind, "label": m.label,
            "comment": m.comment, "color": m.color, "t_end": m.t_end,
            "run_dir": m.run_dir, "origin_kind": m.origin_kind,
            "origin_id": m.origin_id, "scope": m.scope, "severity": m.severity,
            "payload": m.payload, "version": m.version, "deleted": m.deleted}


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


# §7.3 vocabulary alias — the entity is a Tag; "Marker" is the legacy name.
Tag = Marker
