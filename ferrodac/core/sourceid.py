"""Source identity resolution — ONE place that turns a source key into a rich,
device-qualified display, for LIVE and HISTORIC sources alike.

A source key is ``"<device-id>/<channel>"`` (device-id = uuid|instance_id). Live
sources carry their device on the ``SourcePort`` (``origin``); historic sources
carry it in the store's per-device provenance record (``device_record_at``). This
module unifies both so the Timeline, charts, widgets and the lab journal all show
the same label — e.g. ``"ch1 · Sim Gauge A"`` — instead of a bare ``"ch1"``.

Qt-free and dependency-light so it's unit-testable headless and usable from the
store/writer side. Old stores (no device record) degrade to the bare channel tail,
exactly as before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# store dtype tag -> the display dtype the dashboard routes on
_DTYPE_MAP = {"scalar": "float", "trace": "trace", "bool": "bool"}


def compose_label(channel: str, device: str) -> str:
    """The device-qualified flat label: ``"{channel} · {device}"`` when the device is
    known and not already named in the channel; else the bare channel. (Same rule as
    ``SourcePort.label`` — kept here so historic + live agree.)"""
    dev = (device or "").strip()
    chan = channel or ""
    if dev and dev.lower() not in chan.lower():
        return f"{chan} · {dev}"
    return chan


@dataclass
class SourceInfo:
    key: str
    device_id: str
    device_name: str           # "" when unknown (old store / no record)
    channel_name: str
    unit: str
    dtype: str                 # "float" | "trace" | "bool"
    kind: str                  # "device" | "remote" | "historic" | "virtual"
    is_derived: bool = False   # processor output (not persisted)
    device_meta: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return compose_label(self.channel_name, self.device_name)


def resolve_source(key, *, live_ports=None, store=None, now=None) -> SourceInfo:
    """Resolve a source key to a `SourceInfo`, live first then historic.

    - live: a `SourcePort` in `live_ports` → its name/origin/kind (already qualified).
    - historic: the store's `source_meta` + `device_record_at` give the channel + device.
    - fallback (no record): device_name = "" → label degrades to the bare channel tail.
    """
    device_id = key.split("/", 1)[0]
    port = (live_ports or {}).get(key)
    if port is not None:
        kind = getattr(port, "kind", "device")
        dev = (getattr(port, "origin", "") or "").strip()
        if kind not in ("device", "remote", "historic"):
            dev = ""                       # match SourcePort.label: no qual for virtual/input
        return SourceInfo(
            key, device_id, dev, getattr(port, "name", "") or device_id,
            getattr(port, "unit", ""), getattr(port, "dtype", "float"), kind,
            bool(getattr(port, "proc_id", "")), {})

    name, unit, dtype = (store.source_meta(key) if store is not None
                         else ("", "", "scalar"))
    channel = name if (name and name != key) else key.rsplit("/", 1)[-1]
    rec = {}
    if store is not None:
        rec = store.device_record_at(device_id, time.time() if now is None else now)
    return SourceInfo(key, device_id, (rec.get("name") or "").strip(), channel,
                      unit, _DTYPE_MAP.get(dtype, "float"), "historic", False, rec)
