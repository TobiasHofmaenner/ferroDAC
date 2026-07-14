"""build_control_surface(app) — register the app's functions as control-surface verbs.

The app-side adapter that turns MainWindow / model methods into the self-describing
command/query surface an external connector drives. GUI-mutating verbs are wrapped in
`GuiBridge.post_and_wait` so they run on the GUI thread and return a result to the
(threadpool) caller; `device.set_sink` deliberately runs OFF the GUI thread (it uses
`manager.write_sync`, which may block on device I/O). This is the cheap-tier spike set —
devices, layout, projects, replay, tags, hub — reusing methods that already exist.
"""

from __future__ import annotations

import os
import time

from ..core.control import ControlError, ControlSurface
from ..core.tag import SEVERITIES, marker_to_dict


# -- serializers (verbs must return JSON-able results) ----------------------
def _dev_dict(d) -> dict:
    status = getattr(d.status, "value", None) or str(getattr(d, "status", ""))
    return {
        "instance_id": d.instance_id, "uuid": getattr(d, "uuid", None),
        "name": d.name, "driver": d.driver, "status": status,
        "sources": [{"id": s.id, "name": s.name, "unit": s.unit} for s in d.sources],
        "sinks": [{"id": s.id, "name": s.name,
                   "kind": getattr(s.kind, "value", str(s.kind))} for s in d.sinks],
    }


def _source_port_dict(sp) -> dict:
    """Serialize a SourcePort (workspace.py) for the source catalog."""
    return {"key": sp.key, "name": sp.name, "dtype": sp.dtype, "unit": sp.unit,
            "origin": sp.origin, "kind": sp.kind, "online": bool(sp.online)}


def _reading_value(dtype: str, reading):
    """JSON-safe current value for a source, keyed off its declared dtype: a scalar
    (float/bool) is coerced to a plain number/bool (a numpy scalar isn't JSON-able);
    a trace/image payload isn't JSON-able so we return a {'type': dtype} descriptor,
    never the raw Reading.value. A missing reading yields None."""
    if reading is None:
        return None
    val = reading.value
    if dtype == "bool":
        try:
            return bool(val)
        except Exception:                        # noqa: BLE001
            return {"type": "bool"}
    if dtype == "float":
        try:
            return float(val)
        except (TypeError, ValueError):
            return {"type": "float"}
    return {"type": dtype or "unknown"}          # trace / image / action / unknown


def _active_project(app):
    """The active Project, or a ControlError if there's none."""
    pm = getattr(app, "_project_mgr", None)
    project = getattr(pm, "active", None) if pm is not None else None
    if project is None:
        raise ControlError("no active project")
    return project


def _doc_path(project, name: str) -> str:
    """Resolve a doc name/relpath to an absolute path INSIDE the project folder,
    raising a ControlError on any '..' / absolute / symlink escape (realpath-based)."""
    base = os.path.realpath(project.path)
    target = os.path.realpath(os.path.join(base, name))
    if target != base and not target.startswith(base + os.sep):
        raise ControlError(f"path escapes project: {name!r}")
    return target


def build_control_surface(app) -> ControlSurface:
    s = ControlSurface()
    gb = app._gui_bridge

    def gui(fn):
        """Run a GUI-thread handler and return its result to the (threadpool) caller.
        reraise=True so a handler's ControlError surfaces to the connector instead of
        being swallowed by the GUI marshal (which would return a silent null)."""
        return lambda payload: gb.post_and_wait(lambda: fn(payload), reraise=True)

    # -- queries (read) ------------------------------------------------------
    def _device_list(_):
        return {"active": [_dev_dict(d) for d in app.manager.active_descriptors()],
                "available": [_dev_dict(d) for d in app.manager.available_descriptors()]}
    s.query("device.list", gui(_device_list), description="List active + available devices.",
            returns="{active: [...], available: [...]}")

    def _project_list(_):
        pm = getattr(app, "_project_mgr", None)
        active = getattr(pm.active, "id", None) if pm is not None else None
        projs = pm.projects() if pm is not None else []
        return {"active": active,
                "projects": [{"id": p.id, "name": p.name} for p in projs]}
    s.query("project.list", gui(_project_list), description="List projects + the active one.",
            returns="{active: id, projects: [{id, name}]}")

    s.query("layout.get", gui(lambda _: app.dashboard.export_layout()),
            description="The current layout (panels + routes) as a dict.")

    def _time_window(_):
        tc = app.time_context
        if tc is None:
            return {"available": False}
        t0, t1 = tc.window
        return {"t0": t0, "t1": t1, "mode": getattr(tc.mode, "value", str(tc.mode))}
    s.query("time.window", _time_window, description="The current replay time window + mode.")

    s.query("tag.list", lambda _: app.dashboard.markers.to_list(),
            description="All tags/markers.")

    def _hub_status(_):
        hub = app.hub
        agent, viewer = hub.roles if hasattr(hub, "roles") else (False, False)
        return {"connected": bool(getattr(hub, "connected", False)),
                "addr": getattr(hub, "addr", ""), "agent": agent, "viewer": viewer}
    s.query("hub.status", lambda _: _hub_status(_), description="Hub connection state.")

    # -- commands (control) --------------------------------------------------
    def _hub_connect(p):
        addr = p.get("addr")
        if not addr:
            raise ControlError("hub.connect needs 'addr'")
        app.hub.connect(str(addr), bool(p.get("as_agent", True)),
                        bool(p.get("as_viewer", True)))
        return {"ok": True, "addr": addr}
    s.register("hub.connect", gui(_hub_connect), description="Connect to a hub.",
               params={"addr": {"type": "string", "required": True},
                       "as_agent": {"type": "boolean"}, "as_viewer": {"type": "boolean"}},
               returns="{ok, addr}")

    s.register("hub.disconnect", gui(lambda p: (app.hub.disconnect(), {"ok": True})[1]),
               description="Disconnect from the hub.")

    def _device_add(p):
        iid = p.get("instance_id")
        if not iid:
            raise ControlError("device.add needs 'instance_id'")
        app.manager.add(str(iid), user=True)
        return {"ok": True, "instance_id": iid}
    s.register("device.add", gui(_device_add),
               description="Onboard an available (discovered) device.",
               params={"instance_id": {"type": "string", "required": True}})

    def _device_set_sink(p):     # OFF the GUI thread — write_sync may block on I/O
        for k in ("instance_id", "sink_id"):
            if k not in p:
                raise ControlError(f"device.set_sink needs '{k}'")
        ok, detail = app.manager.write_sync(str(p["instance_id"]), str(p["sink_id"]),
                                            p.get("value"))
        if not ok:
            raise ControlError(detail or "write failed")
        return {"ok": True}
    s.register("device.set_sink", _device_set_sink,
               description="Set a device control sink (setpoint/toggle/enum/action). "
                           "value: number/bool/str, or omit for an ACTION.",
               params={"instance_id": {"type": "string", "required": True},
                       "sink_id": {"type": "string", "required": True},
                       "value": {"type": "any"}})

    s.register("layout.add_panel",
               gui(lambda p: {"panel_id": app.dashboard.add_panel(str(p.get("kind", "chart")))}),
               description="Add a dashboard panel (kind: chart/numeric/waterfall/…).",
               params={"kind": {"type": "string"}}, returns="{panel_id}")

    def _park_window(p):
        tc = app.time_context
        if tc is None:
            raise ControlError("no replay time context")
        tc.park_window(float(p["t0"]), float(p["t1"]))
        return {"ok": True}
    s.register("time.park_window", gui(_park_window),
               description="Scrub the timeline to a window [t0,t1] (unix seconds).",
               params={"t0": {"type": "number", "required": True},
                       "t1": {"type": "number", "required": True}})

    def _tag_add(p):
        if "label" not in p:
            raise ControlError("tag.add needs 'label'")
        mid = app.dashboard.markers.add(float(p.get("t") or time.time()),
                                        str(p["label"]), comment=str(p.get("comment", "")))
        return {"id": mid}
    s.register("tag.add", gui(_tag_add), description="Add a tag/marker at a time (default now).",
               params={"label": {"type": "string", "required": True},
                       "comment": {"type": "string"}, "t": {"type": "number"}})

    def _project_switch(p):
        if "id" not in p:
            raise ControlError("project.switch needs 'id'")
        app._switch_project(str(p["id"]))
        return {"ok": True, "active": str(p["id"])}
    s.register("project.switch", gui(_project_switch),
               description="Switch the active project.",
               params={"id": {"type": "string", "required": True}})

    # -- sources (read) ------------------------------------------------------
    def _source_list(_):
        return [_source_port_dict(sp) for sp in app.dashboard.source_ports()]
    s.query("source.list", gui(_source_list),
            description="List every source port (channel) — scalar/trace/image, "
                        "routed or not, local/historic/remote.",
            returns="[{key, name, dtype, unit, origin, kind, online}]")

    def _latest_readings():
        # engine.latest() is a thread-safe COPY key->Reading; DERIVED/processor
        # outputs ride the (possibly distinct) data_bus, which the engine misses.
        readings = app.dashboard.engine.latest()
        bus = app.dashboard.data_bus
        if bus is not app.dashboard.engine:
            readings.update(bus.latest())
        return readings

    def _source_entry(key, sp, latest):
        r = latest.get(key)
        return {"key": key, "name": sp.name, "unit": sp.unit, "dtype": sp.dtype,
                "value": _reading_value(sp.dtype, r),
                "t": (r.t if r is not None else None)}

    def _source_read(p):
        latest = _latest_readings()
        key = p.get("key")
        if key is not None:
            sp = app.dashboard._sources.get(str(key))
            if sp is None:
                raise ControlError(f"unknown source: {key!r}")
            return _source_entry(str(key), sp, latest)
        # omit key => every SCALAR source's value (trace/image are bulk — read by key)
        return [_source_entry(k, sp, latest)
                for k, sp in app.dashboard._sources.items()
                if sp.dtype in ("float", "bool")]
    s.query("source.read", gui(_source_read),
            description="A source's current value joined with its {name,unit,dtype}. "
                        "Omit 'key' for every scalar source's value; a trace/image "
                        "source returns a {'type': dtype} descriptor, not the raw payload.",
            params={"key": {"type": "string"}},
            returns="{key,name,unit,dtype,value,t} — or a list when 'key' is omitted")

    # -- layout routing ------------------------------------------------------
    def _set_route(p, on):
        for k in ("source_key", "sink_key"):
            if k not in p:
                raise ControlError(f"layout.{'route' if on else 'unroute'} needs '{k}'")
        src, sink = str(p["source_key"]), str(p["sink_key"])
        app.dashboard.set_route(src, sink, on)          # returns None
        routed = sorted(app.dashboard.routed(src))      # set -> JSON-able sorted list
        # a chart may REFUSE a dimensionally-incompatible source; after a refusal (or
        # an unroute) sink_key won't be in routed() — so 'attached' is the true effect.
        return {"ok": True, "source_key": src, "routed": routed,
                "attached": sink in routed}
    s.register("layout.route", gui(lambda p: _set_route(p, True)),
               description="Route a source into a sink (chart/processor/device). A chart "
                           "may REFUSE a dimensionally-incompatible source: then 'attached' "
                           "is false and 'routed' won't contain sink_key.",
               params={"source_key": {"type": "string", "required": True},
                       "sink_key": {"type": "string", "required": True}},
               returns="{ok, source_key, routed, attached}")
    s.register("layout.unroute", gui(lambda p: _set_route(p, False)),
               description="Remove a source->sink route.",
               params={"source_key": {"type": "string", "required": True},
                       "sink_key": {"type": "string", "required": True}},
               returns="{ok, source_key, routed, attached}")

    s.query("layout.routes",
            gui(lambda _: app.dashboard.export_layout().get("routes", {})),
            description="The desired source->sink route map {source_key: [sink_key,...]}.",
            returns="{source_key: [sink_key,...]}")

    def _remove_panel(p):
        pid = p.get("panel_id") or p.get("id")          # accept either key
        if not pid:
            raise ControlError("layout.remove_panel needs 'panel_id'")
        pid = str(pid)
        if app.dashboard.panel(pid) is None:            # remove_panel no-ops on unknown
            raise ControlError(f"unknown panel: {pid}")
        app.dashboard.remove_panel(pid)                 # drops the panel + routes to it
        return {"ok": True, "removed": pid}
    s.register("layout.remove_panel", gui(_remove_panel),
               description="Remove a dashboard panel and any routes attached to it.",
               params={"panel_id": {"type": "string"}, "id": {"type": "string"}},
               returns="{ok, removed}", destructive=True)

    # -- tags: edit + delete (delete is destructive) -------------------------
    def _tag_update(p):
        mid = str(p["id"])
        markers = app.dashboard.markers
        if markers.get(mid) is None:                    # get() hides tombstones
            raise ControlError(f"tag.update: no tag {mid!r}")
        # whitelist the editable fields (update() blindly setattr()s anything given)
        # and include a key ONLY if the caller sent it (an omitted param must not
        # overwrite the stored value with None).
        fields = {}
        for k in ("label", "comment", "color"):
            if k in p:
                fields[k] = str(p[k])
        if "severity" in p:
            sev = str(p["severity"])
            if sev not in SEVERITIES:
                raise ControlError(
                    f"tag.update: bad severity {sev!r} (one of {list(SEVERITIES)})")
            fields["severity"] = sev
        if not fields:
            raise ControlError(
                "tag.update: nothing to change (give label/comment/severity/color)")
        markers.update(mid, **fields)
        return marker_to_dict(markers.get(mid))
    s.register("tag.update", gui(_tag_update),
               description="Edit a tag's metadata (label/comment/severity/color). The "
                           "tag's TIME is unchanged; immutable tags stay metadata-editable.",
               params={"id": {"type": "string", "required": True},
                       "label": {"type": "string"}, "comment": {"type": "string"},
                       "severity": {"type": "string", "enum": list(SEVERITIES)},
                       "color": {"type": "string"}},
               returns="the updated tag as a dict")

    def _tag_remove(p):
        mid = str(p["id"])
        markers = app.dashboard.markers
        if markers.get(mid) is None:                    # already gone / tombstoned
            raise ControlError(f"tag.remove: no tag {mid!r}")
        markers.remove(mid)                             # tombstone (deleted=True, ver+1)
        return {"ok": True, "id": mid}
    s.register("tag.remove", gui(_tag_remove),
               description="Delete a tag. Tombstoned (not hard-dropped) so the delete "
                           "propagates to peers over hub sync.",
               params={"id": {"type": "string", "required": True}},
               returns="{ok, id}", destructive=True)

    # -- docs (project lab-journal + docs/ references; file-as-truth) --------
    # NOT gui()-wrapped: no Qt objects, just (possibly-blocking) disk I/O — like
    # device.set_sink, run off the GUI thread. Active project via _project_mgr.active.
    def _doc_list(_):
        project = _active_project(app)
        out = []
        readme = project.readme_path
        if os.path.isfile(readme):
            out.append({"name": "README.md", "path": readme,
                        "ext": "md", "kind": "readme"})
        for d in project.docs():
            out.append({"name": d["name"], "path": d["path"],
                        "ext": d.get("ext", ""), "kind": "doc"})
        return out
    s.query("doc.list", _doc_list,
            description="List the active project's documents (the README lab-journal "
                        "+ docs/ reference files).",
            returns="[{name, path, ext, kind}]")

    def _doc_get(p):
        name = p.get("name") or p.get("path")
        if not name:
            raise ControlError("doc.get needs 'name'")
        project = _active_project(app)
        path = _doc_path(project, str(name))
        if not os.path.isfile(path):
            raise ControlError(f"no such doc: {name!r}")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except UnicodeDecodeError as exc:               # a binary attachment isn't text
            raise ControlError(f"{name!r} is not a text document") from exc
        return {"name": str(name), "text": text}
    s.query("doc.get", _doc_get,
            description="Read a project document's text by name/relpath "
                        "(README.md, or a docs/ file).",
            params={"name": {"type": "string"}, "path": {"type": "string"}},
            returns="{name, text}")

    def _doc_append(p):
        if "text" not in p:
            raise ControlError("doc.append needs 'text'")
        project = _active_project(app)
        name = p.get("name")
        if name:
            path = _doc_path(project, str(name))
        else:
            path = project.ensure_readme() or project.readme_path   # lab-journal default
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(str(p["text"]))
        except OSError as exc:
            raise ControlError(f"doc.append failed: {exc}") from exc
        return {"ok": True,
                "name": str(name) if name else os.path.relpath(path, project.path)}
    s.register("doc.append", _doc_append,
               description="Append text to a project document (defaults to the README "
                           "lab-journal; creates it if missing).",
               params={"text": {"type": "string", "required": True},
                       "name": {"type": "string"}},
               returns="{ok, name}")

    return s
