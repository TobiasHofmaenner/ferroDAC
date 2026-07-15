"""build_control_surface(app) — register the app's functions as control-surface verbs.

The app-side adapter that turns MainWindow / model methods into the self-describing
command/query surface an external connector drives. GUI-mutating verbs are wrapped in
`GuiBridge.post_and_wait` so they run on the GUI thread and return a result to the
(threadpool) caller; `device.set_sink` deliberately runs OFF the GUI thread (it uses
`manager.write_sync`, which may block on device I/O). This is the cheap-tier spike set —
devices, layout, projects, replay, tags, hub — reusing methods that already exist.
"""

from __future__ import annotations

import base64
import binascii
import math
import os
import time

from ..core.control import ControlError, ControlSurface
from ..core.media import MediaError
from ..core.tag import SEVERITIES, marker_to_dict

# The photo categories a companion (phone) upload can be filed under — a fixed,
# closed set ('generic' is the catch-all). Shared with the companion server.
MEDIA_CATEGORIES = ("setup", "sample", "result", "generic")


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
            v = float(val)
        except (TypeError, ValueError):
            return {"type": "float"}
        # JSON has no NaN/Infinity, and Starlette's JSONResponse uses allow_nan=False
        # (a raw NaN would 500 the whole response) — a non-finite reading reads as null.
        return v if math.isfinite(v) else None
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


def _json_scalar(v):
    """Coerce an option/choice value to a strictly JSON-able scalar — a driver could
    stash a non-str/number/bool (or numpy scalar) as an option value; never leak it raw."""
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _option_dict(o) -> dict:
    """Serialize a device configuration Option — schema + current value (choice/text/
    secret). A SECRET's value is masked ('***'): keys/passwords never cross the control
    surface in the clear (config_set can still WRITE a secret — it is write-only)."""
    kind = getattr(o, "kind", "choice")
    val = "***" if kind == "secret" and o.value else o.value
    return {"key": o.key, "name": o.name, "kind": kind,
            "value": _json_scalar(val),
            "choices": [[_json_scalar(c[0]), c[1]] for c in (o.choices or ())]}


def _project_dict(project) -> dict:
    """Serialize a Project — meta + on-disk location, all primitives (no Enum/Path/QObject)."""
    return {"id": project.id, "name": project.name, "path": project.path,
            "description": project.description, "is_hub": bool(project.is_hub),
            "git_remote": project.git_remote, "version": int(project.version),
            "created": project.meta.get("created", ""),
            "modified": project.meta.get("modified", "")}


def _project_by_id(app, pid: str):
    """The tracked project with id `pid`, or a ControlError (also the None-manager guard)."""
    pm = getattr(app, "_project_mgr", None)
    proj = pm.get(str(pid)) if pm is not None else None
    if proj is None:
        raise ControlError(f"no such project: {pid!r}")
    return proj


def _target_project(app, p: dict):
    """The project named by payload['id'], or the active one when 'id' is omitted."""
    pid = p.get("id")
    return _project_by_id(app, pid) if pid else _active_project(app)


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

    def _rename_panel(p):
        pid = p.get("panel_id") or p.get("id")
        if not pid:
            raise ControlError("layout.rename_panel needs 'panel_id'")
        if "title" not in p:
            raise ControlError("layout.rename_panel needs 'title'")
        pid, title = str(pid), str(p["title"])
        panel = app.dashboard.panel(pid)
        if panel is None:
            raise ControlError(f"unknown panel: {pid}")
        panel.set_display_name(title)                # sets panel.title (persisted) + plot title
        app.dashboard.area.set_panel_title(panel, title)   # the dock / tab title
        return {"ok": True, "panel_id": pid, "title": title}
    s.register("layout.rename_panel", gui(_rename_panel),
               description="Rename a panel's display title (the chart/dock/tab name shown "
                           "in layout.get). Persists in the layout.",
               params={"panel_id": {"type": "string"}, "id": {"type": "string"},
                       "title": {"type": "string", "required": True}},
               returns="{ok, panel_id, title}")

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

    # -- devices: get / remove / config / rename / rate ----------------------
    def _device_get(p):
        iid = str(p["instance_id"])
        desc = app.manager.descriptor(iid)          # active OR available; None if unknown
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        out = _dev_dict(desc)
        out.update({
            "active": app.manager.is_active(iid),
            "model": desc.model, "firmware": desc.firmware,
            "hardware_id": desc.hardware_id, "manufacturer": desc.manufacturer,
            "rate_hz": desc.rate_hz, "primary_source": desc.primary_source,
            "last_error": desc.last_error,
        })
        return out
    s.query("device.get", gui(_device_get),
            description="Full descriptor for ONE device (active or available) — "
                        "identity, sources, sinks + lab-journal metadata.",
            params={"instance_id": {"type": "string", "required": True}},
            returns="a device.list-style dict + {active, model, firmware, "
                    "hardware_id, manufacturer, rate_hz, primary_source, last_error}")

    def _device_remove(p):     # gui-wrapped: manager.remove touches Qt (spawns a worker)
        iid = str(p["instance_id"])
        if not app.manager.is_active(iid):
            raise ControlError(f"device not active: {iid}")
        app.manager.remove(iid)          # pops _active now; stop+disconnect run off-thread
        return {"ok": True, "instance_id": iid, "removed": iid}
    s.register("device.remove", gui(_device_remove),
               description="Retire an ACTIVE device: drop it from the active set, then "
                           "stop streaming + disconnect (asynchronously, like device.add).",
               params={"instance_id": {"type": "string", "required": True}},
               returns="{ok, instance_id, removed}", destructive=True)

    def _device_config_get(p):
        iid = str(p["instance_id"])
        desc = app.manager.descriptor(iid)
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        rate = None
        if desc.rate is not None:
            r = desc.rate
            mode = getattr(r.mode, "value", str(r.mode))
            rate = {"mode": mode, "settable": mode == "settable", "hz": desc.rate_hz,
                    "min_hz": r.min_hz, "max_hz": r.max_hz,
                    "native_hz": r.native_hz, "default_hz": r.default_hz}
        return {"instance_id": iid, "name": desc.name,
                "options": [_option_dict(o) for o in desc.options], "rate": rate}
    s.query("device.config_get", gui(_device_config_get),
            description="A device's SETTABLE configuration: its options (choice/text/"
                        "secret) with current values + the sample-rate control. Secret "
                        "values are masked ('***') — never returned in the clear.",
            params={"instance_id": {"type": "string", "required": True}},
            returns="{instance_id, name, options:[{key,name,kind,value,choices}], rate}")

    def _device_config_set(p):   # OFF the GUI thread — set_option may block (cloud enum)
        iid, key = str(p["instance_id"]), str(p["option"])
        desc = app.manager.descriptor(iid)
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        keys = {o.key for o in desc.options}
        if key not in keys:                          # manager would silently no-op → guard
            raise ControlError(f"unknown option {key!r} for {iid} (one of {sorted(keys)})")
        ok, detail = app.manager.set_option_sync(iid, key, p.get("value"))
        if not ok:
            raise ControlError(detail or "config_set failed")
        cur = next((o for o in app.manager.descriptor(iid).options if o.key == key), None)
        return {"ok": True, "instance_id": iid, "option": key,
                "value": _option_dict(cur)["value"] if cur is not None else None}
    s.register("device.config_set", _device_config_set,
               description="Set ONE configuration option to a value. Returns the option's "
                           "EFFECTIVE value read back after applying (an out-of-choices "
                           "value is a silent no-op; a secret reads back masked).",
               params={"instance_id": {"type": "string", "required": True},
                       "option": {"type": "string", "required": True},
                       "value": {"type": "any"}},
               returns="{ok, instance_id, option, value}")

    def _device_rename(p):       # OFF the GUI thread (manager's sync path, like set_sink)
        iid, name = str(p["instance_id"]), str(p["name"])
        ok, detail = app.manager.rename_sync(iid, name)
        if not ok:
            raise ControlError(detail or "rename failed")
        return {"ok": True, "instance_id": iid, "name": name}
    s.register("device.rename", _device_rename,
               description="Set a device's friendly (display) name.",
               params={"instance_id": {"type": "string", "required": True},
                       "name": {"type": "string", "required": True}},
               returns="{ok, instance_id, name}")

    def _device_set_rate(p):     # OFF the GUI thread (manager's sync path)
        iid = str(p["instance_id"])
        try:
            hz = float(p["hz"])
        except (TypeError, ValueError) as exc:
            raise ControlError(f"set_rate: bad hz {p.get('hz')!r}") from exc
        desc = app.manager.descriptor(iid)
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        if not app.manager.is_active(iid):           # set_rate_sync only touches _active
            raise ControlError(f"device not active: {iid}")
        mode = getattr(getattr(desc.rate, "mode", None), "value", None)
        if mode != "settable":                       # BaseDevice.set_rate_hz no-ops otherwise
            raise ControlError(f"{iid} has a fixed sample rate (not settable)")
        ok, detail = app.manager.set_rate_sync(iid, hz)
        if not ok:
            raise ControlError(detail or "set_rate failed")
        return {"ok": True, "instance_id": iid, "hz": app.manager.descriptor(iid).rate_hz}
    s.register("device.set_rate", _device_set_rate,
               description="Set an active device's sample rate in Hz (clamped to the "
                           "driver's min/max). Errors if the driver is fixed-rate. "
                           "Returns the EFFECTIVE (clamped) rate.",
               params={"instance_id": {"type": "string", "required": True},
                       "hz": {"type": "number", "required": True}},
               returns="{ok, instance_id, hz}")

    def _device_create(p):
        kind = str(p.get("kind") or "python_device")
        if kind != "python_device":
            raise ControlError(
                f"device.create: unsupported kind {kind!r} (only 'python_device' in v1)")
        from ..devices.python_device import PythonDevice, save_def
        dev = PythonDevice.new(code=p.get("code"),
                                     name=str(p.get("name") or "Python Device"))
        # the compile result, captured BEFORE activation — add_user_device runs connect()
        # on a worker thread, and connect() clears last_error (it's also the transport-
        # error slot), which would otherwise race away a code error.
        compile_error = dev.describe().last_error
        save_def(dev.instance_id, dev.code)          # persist so it survives restart
        app.manager.add_user_device(dev, user=True)  # activate (straight into _active)
        out = _dev_dict(dev.describe())
        out["last_error"] = compile_error            # non-null if the code didn't compile
        return out
    s.register("device.create", gui(_device_create),
               description="Create a NEW user-minted device and activate it (v1: a "
                           "'python_device' — a virtual device whose channels are produced "
                           "by Python you supply). Optionally pass initial 'code' (a "
                           "poll(ctx) script; default = the starter template) + a 'name'. "
                           "Edit later with device.config_set (option 'code'). Returns the "
                           "descriptor incl. 'last_error' if the code didn't compile; then "
                           "route its source(s) — key '<uuid>/<source_id>' — with layout.route.",
               params={"kind": {"type": "string", "enum": ["python_device"]},
                       "code": {"type": "string"}, "name": {"type": "string"}},
               returns="a device descriptor {instance_id, uuid, name, driver, status, "
                       "sources, sinks, last_error}")

    def _device_set_meta(p):
        iid = str(p["instance_id"])
        desc = app.manager.descriptor(iid)
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        from ..core.devicemeta import JOURNAL_FIELDS, device_key, merge_device_info
        fields = {k: str(p[k]) for k in JOURNAL_FIELDS if k in p}
        if not fields:
            raise ControlError(
                f"device.set_meta: give at least one of {list(JOURNAL_FIELDS)}")
        store = app._device_meta()
        key = device_key(desc)
        merged = store.get(key)
        merged.update(fields)                        # partial edit — don't wipe other fields
        store.set(key, merged)
        push = getattr(app, "_push_device_records", None)
        if callable(push):
            push()                                   # re-freeze provenance into the store
        return merge_device_info(desc, store.get(key))
    s.register("device.set_meta", gui(_device_set_meta),
               description="Set a device's lab-journal metadata — the same fields the "
                           "'Notes & journal' popup edits. This writes device_meta.json AND "
                           "re-freezes the merged provenance into the data store (change-"
                           "logged). Fields: notes, manufacturer, model, serial, firmware, "
                           "cal_date, cal_due, cal_cert, asset_tag (give any subset; an empty "
                           "string clears a field). Returns the merged journal.",
               params={"instance_id": {"type": "string", "required": True},
                       "notes": {"type": "string"}, "manufacturer": {"type": "string"},
                       "model": {"type": "string"}, "serial": {"type": "string"},
                       "firmware": {"type": "string"}, "cal_date": {"type": "string"},
                       "cal_due": {"type": "string"}, "cal_cert": {"type": "string"},
                       "asset_tag": {"type": "string"}},
               returns="the merged journal (user values over device-reported)")

    def _device_get_meta(p):
        iid = str(p["instance_id"])
        desc = app.manager.descriptor(iid)
        if desc is None:
            raise ControlError(f"unknown device: {iid}")
        from ..core.devicemeta import device_key, merge_device_info
        return merge_device_info(desc, app._device_meta().get(device_key(desc)))
    s.query("device.get_meta", gui(_device_get_meta),
            description="A device's merged lab-journal info (user metadata over the "
                        "device-reported fields): name, driver, manufacturer, model, serial, "
                        "firmware, cal_date/due/cert, asset_tag, notes.",
            params={"instance_id": {"type": "string", "required": True}},
            returns="{name, driver, manufacturer, model, serial, firmware, cal_*, asset_tag, notes}")

    # -- projects: metadata + local lifecycle --------------------------------
    def _project_active(_):
        return _project_dict(_active_project(app))
    s.query("project.active", gui(_project_active),
            description="Full metadata for the active project.",
            returns="{id, name, path, description, is_hub, git_remote, version, "
                    "created, modified}")

    def _project_info(p):
        return _project_dict(_project_by_id(app, str(p["id"])))
    s.query("project.info", gui(_project_info),
            description="Full metadata for any tracked project by id.",
            params={"id": {"type": "string", "required": True}},
            returns="{id, name, path, description, is_hub, git_remote, version, "
                    "created, modified}")

    def _project_create(p):
        path = p.get("path")
        if not path:
            raise ControlError("project.create needs 'path'")
        pm = getattr(app, "_project_mgr", None)
        if pm is None:
            raise ControlError("no project manager")
        name = p.get("name")
        # track() ADOPTS an existing project folder or CREATES one, then registers it
        # (Project.create refuses a filesystem/home/system root -> ControlError). It
        # does NOT steal the active project when one is already active.
        proj = pm.track(str(path), str(name) if name else None)
        return _project_dict(proj)
    s.register("project.create", gui(_project_create),
               description="Create (or adopt) a LOCAL project folder at 'path' and track "
                           "it so it shows up in project.list. 'name' sets the display "
                           "name (defaults to the folder name). Refuses a filesystem / "
                           "home / system root.",
               params={"path": {"type": "string", "required": True},
                       "name": {"type": "string"}},
               returns="{id, name, path, description, is_hub, git_remote, version, "
                       "created, modified}")

    def _project_rename(p):
        name = p.get("name")
        if not name or not str(name).strip():
            raise ControlError("project.rename needs a non-empty 'name'")
        proj = _target_project(app, p)
        if not proj.rename(str(name)):            # False only on an empty name (guarded above)
            raise ControlError("project.rename failed")
        return _project_dict(proj)
    s.register("project.rename", gui(_project_rename),
               description="Rename a project's DISPLAY name (the folder is unchanged). "
                           "Defaults to the active project; 'id' targets another.",
               params={"id": {"type": "string"},
                       "name": {"type": "string", "required": True}},
               returns="the renamed project as a dict")

    def _project_set_description(p):
        if "description" not in p:
            raise ControlError("project.set_description needs 'description'")
        proj = _target_project(app, p)
        proj.set_meta(description=str(p["description"]))   # updates meta + saves to disk
        return _project_dict(proj)
    s.register("project.set_description", gui(_project_set_description),
               description="Set a project's description / notes. Defaults to the active "
                           "project; 'id' targets another.",
               params={"id": {"type": "string"},
                       "description": {"type": "string", "required": True}},
               returns="the updated project as a dict")

    def _project_backup(p):      # OFF the GUI thread — zip walk + git bundle may block
        proj = _target_project(app, p)
        dest = p.get("dest")
        if dest:
            dest = str(dest)
            if not dest.lower().endswith(".zip"):
                dest += ".zip"
        else:                    # default beside the project folder (Qt-free, no _app_dir)
            safe = (proj.name or "project").replace("/", "_").replace("\\", "_")
            dest = os.path.join(os.path.dirname(proj.path), f"{safe}.zip")
        from ..core.archive import archive_project
        written = archive_project(proj, dest)     # returns the zip's abspath
        return {"ok": True, "path": written}
    s.register("project.backup", _project_backup,
               description="Write a self-contained .zip backup of a project's METADATA "
                           "(docs/layouts/tags + an invisible git history bundle; NOT the "
                           "measurements). Defaults to the active project and a zip beside "
                           "its folder; 'dest' overrides the path. Runs off the GUI thread.",
               params={"id": {"type": "string"}, "dest": {"type": "string"}},
               returns="{ok, path}")

    # -- replay transport (TimeContext) --------------------------------------
    # Every COMMAND mutates the time model and TimeContext._notify() fans out to Qt
    # observers (PlayerBar) AND the ReplayController — so they MUST run on the GUI thread.
    def _require_tc():
        tc = app.time_context
        if tc is None:
            raise ControlError("no replay time context (durable store disabled)")
        return tc

    def _tc_snapshot(tc) -> dict:
        t0, t1 = tc.window                         # property -> (t0, t1) tuple
        return {"available": True, "mode": tc.mode.value,   # Mode enum -> str
                "head": tc.head, "now": time.time(), "window": [t0, t1],
                "width": tc.width, "grow": bool(tc.grow), "speed": tc.speed,
                "rate": tc.rate, "playing": bool(tc.playing),
                "following": bool(tc.following), "moving": bool(tc.moving)}

    def _time_state(_):
        tc = app.time_context
        if tc is None:
            return {"available": False}
        return _tc_snapshot(tc)
    s.query("time.state", gui(_time_state),
            description="Full replay-transport snapshot: mode (live|parked|playing), the "
                        "head + wall-clock now, window [t0,t1], width, grow, speed, and "
                        "playing/following/moving. {available:false} when replay is off.",
            returns="{available, mode, head, now, window:[t0,t1], width, grow, speed, "
                    "rate, playing, following, moving}")

    def _time_play(_):
        tc = _require_tc()
        tc.play()                          # resumes live at the edge, else replays forward
        return _tc_snapshot(tc)
    s.register("time.play", gui(_time_play),
               description="Resume motion: live if the head is at the live edge, else "
                           "replay forward from where it is parked.",
               returns="the transport snapshot")

    def _time_pause(_):
        tc = _require_tc()
        tc.pause()                         # freeze the head (stop live-follow AND replay)
        return _tc_snapshot(tc)
    s.register("time.pause", gui(_time_pause),
               description="Freeze the head where it is (stop live-follow and replay).",
               returns="the transport snapshot")

    def _time_go_live(_):
        tc = _require_tc()
        tc.follow_now()                    # jump to now + follow; also resets speed to 1x
        return _tc_snapshot(tc)
    s.register("time.go_live", gui(_time_go_live),
               description="Jump the head to now and follow the live edge (resets speed "
                           "to 1x).",
               returns="the transport snapshot")

    def _time_seek(p):
        tc = _require_tc()
        tc.park(float(p["t"]))             # stops live-follow + replay; clamps to now; nav+
        return _tc_snapshot(tc)
    s.register("time.seek", gui(_time_seek),
               description="Jump the head to t (unix seconds), stopping live-follow. "
                           "Clamped to now; the landed window re-streams.",
               params={"t": {"type": "number", "required": True}},
               returns="the transport snapshot")

    def _time_set_speed(p):
        tc = _require_tc()
        spd = float(p["speed"])
        if spd <= 0:
            raise ControlError("time.set_speed: speed must be > 0")
        tc.speed = spd                     # no setter — a plain attribute (like PlayerBar)
        return _tc_snapshot(tc)
    s.register("time.set_speed", gui(_time_set_speed),
               description="Set the replay speed multiplier (e.g. 1, 4, 30, 120).",
               params={"speed": {"type": "number", "required": True}},
               returns="the transport snapshot")

    def _time_set_mode(p):
        tc = _require_tc()
        mode = str(p["mode"]).lower()
        if mode == "live":
            tc.follow_now()
        elif mode == "replay":
            tc.play()                      # snaps to live if already at the edge
        else:
            raise ControlError("time.set_mode: mode must be 'live' or 'replay'")
        return _tc_snapshot(tc)
    s.register("time.set_mode", gui(_time_set_mode),
               description="Set the transport mode: 'live' follows now, 'replay' plays "
                           "the head forward (snaps to live if already at the live edge).",
               params={"mode": {"type": "string", "required": True,
                                "enum": ["live", "replay"]}},
               returns="the transport snapshot")

    def _time_set_width(p):
        tc = _require_tc()
        secs = float(p["seconds"])
        if secs <= 0:
            raise ControlError("time.set_width: seconds must be > 0")
        tc.set_width(secs)                 # clamps to >= 1e-3 internally
        return _tc_snapshot(tc)
    s.register("time.set_width", gui(_time_set_width),
               description="Set the visible time-window width in seconds.",
               params={"seconds": {"type": "number", "required": True}},
               returns="the transport snapshot")

    def _time_set_grow(p):
        tc = _require_tc()
        if "on" not in p:
            raise ControlError("time.set_grow needs 'on'")
        tc.set_grow(bool(p["on"]))         # grow from a pinned anchor vs fixed-width slide
        return _tc_snapshot(tc)
    s.register("time.set_grow", gui(_time_set_grow),
               description="Toggle the window mode: grow from a pinned start (on) vs a "
                           "fixed width that slides (off).",
               params={"on": {"type": "boolean", "required": True}},
               returns="the transport snapshot")

    def _time_step(p):
        tc = _require_tc()
        if "forward" in p:
            forward = bool(p["forward"])
        else:
            d = str(p.get("dir", "forward")).lower()
            if d not in ("back", "forward"):
                raise ControlError("time.step: dir must be 'back' or 'forward'")
            forward = d == "forward"
        half = tc.width / 2.0              # half the width — matches the PlayerBar step
        tc.park(tc.head + (half if forward else -half))   # parks the head; clamps to now
        return _tc_snapshot(tc)
    s.register("time.step", gui(_time_step),
               description="Nudge the parked head by half a window backward/forward. Give "
                           "dir=back|forward (or forward=true/false). Parks; clamps to now.",
               params={"dir": {"type": "string", "enum": ["back", "forward"]},
                       "forward": {"type": "boolean"}},
               returns="the transport snapshot")

    # -- media (phone-companion uploads; rides the same file+tag substrate) ---
    # A photo is a FILE (written VERBATIM) + an immutable media tag. GUI-wrapped:
    # markers.add emits Qt signals; the byte write is small. Reuses the app's
    # _media_service() (built against the ACTIVE project, app.py:921). Accepts
    # raw 'data' bytes (the in-process companion path) OR base64 'data_b64' (HTTP).
    def _media_add_photo(p):
        data = p.get("data")
        if data is None:
            b64 = p.get("data_b64")
            if not b64:
                raise ControlError("media.add_photo needs 'data' (bytes) or 'data_b64'")
            try:
                data = base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ControlError(f"media.add_photo: invalid base64 ({exc})") from exc
        if not isinstance(data, (bytes, bytearray)):
            raise ControlError("media.add_photo: 'data' must be raw bytes")
        category = str(p.get("category") or "generic")
        if category not in MEDIA_CATEGORIES:
            raise ControlError(
                f"media.add_photo: unknown category {category!r} "
                f"(one of {list(MEDIA_CATEGORIES)})")
        try:
            return app._media_service().add_photo(
                bytes(data), category=category, label=str(p.get("label", "")),
                comment=str(p.get("comment", "")), ext=str(p.get("ext", "jpg")))
        except MediaError as exc:                 # no active project / empty data
            raise ControlError(str(exc)) from exc
    s.register("media.add_photo", gui(_media_add_photo),
               description="Add a photo to the ACTIVE project (the phone companion's "
                           "upload path): store the image VERBATIM as a media file plus an "
                           "immutable media tag — bytes written as-is, NOT re-encoded. Give "
                           "'data_b64' (base64) or raw 'data' bytes. 'category' is one of "
                           "setup/sample/result/generic (default generic); 'label'/'comment' "
                           "annotate the tag; 'ext' is the image type (jpg/jpeg/png/webp).",
               params={"data_b64": {"type": "string"}, "data": {"type": "any"},
                       "category": {"type": "string", "enum": list(MEDIA_CATEGORIES)},
                       "label": {"type": "string"}, "comment": {"type": "string"},
                       "ext": {"type": "string"}},
               returns="{tag_id, path, relpath, t, category, file}")

    s.query("media.categories", lambda _: list(MEDIA_CATEGORIES),
            description="The photo categories a media upload can be filed under "
                        "(setup/sample/result/generic).",
            returns="[category, ...]")

    # -- guidance (procedural playbooks — the HOW, alongside /describe's WHAT) --
    # Qt-free plain text: like doc.*, register DIRECTLY (off the GUI thread).
    from ..guidance import GuidanceLibrary
    _guide = GuidanceLibrary()

    s.query("guidance.list", lambda _: _guide.list(),
            description="List the procedural PLAYBOOKS the app ships (the HOW to "
                        "/describe's WHAT): each is a step-by-step recipe over existing "
                        "verbs. Returns the index only — call guidance.get for a body. "
                        "CONSULT THIS before attempting any multi-step task.",
            returns="[{id, title, when_to_use, tags, source}]")

    def _guidance_get(p):
        pid = p.get("id")
        if not pid:
            raise ControlError("guidance.get needs 'id'")
        pb = _guide.get(str(pid))
        if pb is None:
            raise ControlError(f"no such playbook: {pid!r}")
        return pb
    s.query("guidance.get", _guidance_get,
            description="Get ONE playbook in full: its steps, verbs_used, and a "
                        "copy-pasteable skeleton. Get the id from guidance.list.",
            params={"id": {"type": "string", "required": True}},
            returns="{id, title, when_to_use, tags, verbs_used, body, source}")

    return s
