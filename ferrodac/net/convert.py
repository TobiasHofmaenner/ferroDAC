"""Conversions between the app's Qt-free dataclasses and the wire contract.

Needs only protobuf (not grpcio), so it imports/tests on the locked host too.
The wire identity is the device **uuid** (data-plane identity); the app's
Reading keys on the device **instance_id**, so the agent supplies the uuid.
"""

from __future__ import annotations

import numbers

import numpy as np
from ferrodac_contract.v1 import data_plane_pb2 as pb

from ..core.device import DeviceDescriptor, SinkKind
from ..core.reading import Reading
from ..core.tag import (Marker, ORIGIN_USER, ORIGIN_DEVICE, ORIGIN_PROCESSOR,
                        ORIGIN_SYSTEM)
from ..core.trace import Trace

_DTYPE_TO_PROTO = {
    "float": pb.SCALAR, "bool": pb.BOOLEAN, "trace": pb.TRACE,
    "string": pb.TEXT, "enum": pb.TEXT, "image": pb.IMAGE,
    "video": pb.VIDEO, "waveform": pb.WAVEFORM,
}
_DTYPE_FROM_PROTO = {
    pb.SCALAR: "float", pb.BOOLEAN: "bool", pb.TRACE: "trace",
    pb.TEXT: "string", pb.IMAGE: "image", pb.VIDEO: "video",
    pb.WAVEFORM: "waveform",
}
_SINKKIND_TO_PROTO = {
    SinkKind.ACTION: pb.ACTION, SinkKind.SETPOINT: pb.SETPOINT,
    SinkKind.TOGGLE: pb.TOGGLE, SinkKind.ENUM: pb.ENUM,
}


def _app_version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return ""


def _tolist(a) -> list:
    if a is None:
        return []
    fn = getattr(a, "tolist", None)
    return fn() if fn else list(a)


# --- descriptor (agent side) ----------------------------------------------- #
def descriptor_to_proto(d: DeviceDescriptor) -> pb.DeviceDescriptor:
    sources = [pb.SourcePort(id=s.id, name=s.name, unit=s.unit or "",
                             dtype=_DTYPE_TO_PROTO.get(s.dtype, pb.SCALAR))
               for s in d.sources]
    sinks = []
    for sk in d.sinks:
        p = sk.params[0] if sk.params else None
        sp = pb.SinkPort(
            id=sk.id, name=sk.name,
            kind=_SINKKIND_TO_PROTO.get(sk.kind, pb.SINK_KIND_UNSPECIFIED),
            unit=(p.unit if p else ""),
            options=[str(o) for o in (p.options if p else ())])
        if p is not None and p.minimum is not None:
            sp.min = float(p.minimum)
        if p is not None and p.maximum is not None:
            sp.max = float(p.maximum)
        sinks.append(sp)
    return pb.DeviceDescriptor(
        uuid=d.uuid or d.instance_id, instance_id=d.instance_id, name=d.name,
        driver=d.driver, hardware_id=d.hardware_id or "",
        firmware=d.firmware or "", software_version=_app_version(),
        online=True, sources=sources, sinks=sinks)


# --- trace ------------------------------------------------------------------ #
def trace_to_proto(t: Trace) -> pb.Trace:
    mt = pb.Trace(x=_tolist(t.x), y=_tolist(t.y),
                  x_label=t.x_label or "", x_unit=t.x_unit or "",
                  y_label=t.y_label or "", y_unit=t.y_unit or "")
    if t.x_lo is not None:
        mt.x_lo = float(t.x_lo)
    if t.x_hi is not None:
        mt.x_hi = float(t.x_hi)
    if t.sigma is not None:
        mt.sigma.extend(_tolist(t.sigma))
    return mt


def trace_from_proto(mt: pb.Trace) -> Trace:
    return Trace(
        x=np.asarray(mt.x, dtype=float), y=np.asarray(mt.y, dtype=float),
        x_label=mt.x_label, x_unit=mt.x_unit,
        y_label=mt.y_label, y_unit=mt.y_unit,
        x_lo=mt.x_lo if mt.HasField("x_lo") else None,
        x_hi=mt.x_hi if mt.HasField("x_hi") else None,
        sigma=np.asarray(mt.sigma, dtype=float) if len(mt.sigma) else None)


# --- reading ---------------------------------------------------------------- #
def _status_to_proto(status: int):
    return pb.OK if not status else pb.ERROR


def reading_to_proto(r: Reading, device_uuid: str) -> pb.Reading:
    m = pb.Reading(device_uuid=device_uuid, source_id=r.source, t=float(r.t),
                   status=_status_to_proto(r.status), partial=bool(r.partial))
    v = r.value
    if isinstance(v, Trace):
        m.trace.CopyFrom(trace_to_proto(v))
    elif isinstance(v, (bool, np.bool_)):
        m.boolean = bool(v)
    elif isinstance(v, numbers.Real):
        m.scalar = float(v)
    elif isinstance(v, str):
        m.text = v
    return m


def reading_from_proto(m: pb.Reading) -> Reading:
    which = m.WhichOneof("payload")
    if which == "trace":
        value = trace_from_proto(m.trace)
    elif which == "boolean":
        value = m.boolean
    elif which == "scalar":
        value = m.scalar
    elif which == "text":
        value = m.text
    else:
        value = float("nan")
    status = 0 if m.status in (pb.OK, pb.STATUS_UNSPECIFIED) else 1
    return Reading(device=m.device_uuid, source=m.source_id, t=m.t,
                   value=value, status=status, partial=m.partial)


# --- tag (own reliable channel, §7.3) --------------------------------------- #
_ORIGIN_TO_PROTO = {
    ORIGIN_USER: pb.TAG_ORIGIN_USER, ORIGIN_DEVICE: pb.TAG_ORIGIN_DEVICE,
    ORIGIN_PROCESSOR: pb.TAG_ORIGIN_PROCESSOR, ORIGIN_SYSTEM: pb.TAG_ORIGIN_SYSTEM,
}
_ORIGIN_FROM_PROTO = {v: k for k, v in _ORIGIN_TO_PROTO.items()}
_SEVERITY_TO_PROTO = {
    "info": pb.TAG_INFO, "warn": pb.TAG_WARN,
    "error": pb.TAG_ERROR, "critical": pb.TAG_CRITICAL,
}
_SEVERITY_FROM_PROTO = {v: k for k, v in _SEVERITY_TO_PROTO.items()}


def tag_to_proto(m: Marker) -> pb.Tag:
    t = pb.Tag(
        id=m.id, t=float(m.t), kind=m.kind or "", label=m.label or "",
        comment=m.comment or "", color=m.color or "",
        origin_kind=_ORIGIN_TO_PROTO.get(m.origin_kind, pb.TAG_ORIGIN_USER),
        origin_id=m.origin_id or "", scope=m.scope or "global",
        severity=_SEVERITY_TO_PROTO.get(m.severity, pb.TAG_INFO),
        version=int(m.version), deleted=bool(m.deleted))
    if m.t_end is not None:
        t.t_end = float(m.t_end)
    # payload values go on the wire as strings (map<string,string>)
    for k, v in (m.payload or {}).items():
        t.payload[str(k)] = v if isinstance(v, str) else str(v)
    t.projects.extend(m.projects or [])
    return t


def tag_from_proto(t: pb.Tag) -> Marker:
    return Marker(
        id=t.id, t=t.t,
        kind=t.kind or "tag", label=t.label, comment=t.comment,
        color=t.color or "#ffd54f",
        t_end=t.t_end if t.HasField("t_end") else None,
        origin_kind=_ORIGIN_FROM_PROTO.get(t.origin_kind, ORIGIN_USER),
        origin_id=t.origin_id, scope=t.scope or "global",
        severity=_SEVERITY_FROM_PROTO.get(t.severity, "info"),
        payload=dict(t.payload), projects=list(t.projects),
        version=int(t.version), deleted=bool(t.deleted))
