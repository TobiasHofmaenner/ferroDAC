"""build_control_surface(app) — register the app's functions as control-surface verbs.

The app-side adapter that turns MainWindow / model methods into the self-describing
command/query surface an external connector drives. GUI-mutating verbs are wrapped in
`GuiBridge.post_and_wait` so they run on the GUI thread and return a result to the
(threadpool) caller; `device.set_sink` deliberately runs OFF the GUI thread (it uses
`manager.write_sync`, which may block on device I/O). This is the cheap-tier spike set —
devices, layout, projects, replay, tags, hub — reusing methods that already exist.
"""

from __future__ import annotations

import time

from ..core.control import ControlError, ControlSurface


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


def build_control_surface(app) -> ControlSurface:
    s = ControlSurface()
    gb = app._gui_bridge

    def gui(fn):
        """Run a GUI-thread handler and return its result to the (threadpool) caller."""
        return lambda payload: gb.post_and_wait(lambda: fn(payload))

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
        agent, viewer = hub.roles() if hasattr(hub, "roles") else (False, False)
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
        m = app.dashboard.markers.add(float(p.get("t") or time.time()),
                                      str(p["label"]), comment=str(p.get("comment", "")))
        return {"id": getattr(m, "id", None)}
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

    return s
