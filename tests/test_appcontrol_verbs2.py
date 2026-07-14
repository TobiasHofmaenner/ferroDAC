"""Batch 2 control-surface verbs dispatched against a REAL MainWindow.

devices (get/remove/config/rename/rate) · projects (active/info/create/rename/
set_description/backup) · replay transport (state/play/pause/go_live/seek/set_speed/
set_mode/set_width/set_grow/step) — each exercised end-to-end through
ControlSurface.dispatch against the true model objects, with scope + destructive gates
and a JSON-ability guard. Marked `ui` (they build Qt).
"""

import json
import os
import tempfile
import time
import zipfile

import pytest

from ferrodac.core.base import BaseDevice
from ferrodac.core.control import ControlError, ScopeError
from ferrodac.core.device import Interface, Option, RateControl, RateMode, Source
from ferrodac.core.projects import Project
from ferrodac.devices import fake


def assert_json_able(value):
    json.dumps(value)   # raises if a QObject/QImage/set/Enum/Path/dataclass leaked
    return value


_FULL_KEYS = {"id", "name", "path", "description", "is_hub",
              "git_remote", "version", "created", "modified"}


def _cfg_device():
    """A real BaseDevice with a settable rate + choice/text/secret options — exercises
    the true describe()/set_option/set_name/set_rate_hz contract, no hardware."""
    return BaseDevice(
        instance_id="sim:cfg:1", name="Cfg Dev", interface=Interface(kind="sim"),
        sources=[Source(id="x", name="X", unit="V")],
        options=[Option("mode", "Mode", (("a", "Alpha"), ("b", "Beta")), "a", "choice"),
                 Option("note", "Note", (), "hi", "text"),
                 Option("auth_key", "Auth key", (), "s3cr3t", "secret")],
        rate=RateControl(mode=RateMode.SETTABLE, native_hz=10.0, default_hz=2.0,
                         min_hz=0.5, max_hz=10.0),
    )


# -- devices -----------------------------------------------------------------
@pytest.mark.ui
def test_device_get_returns_full_descriptor(control_surface):
    w, s = control_surface
    psu = fake.FakePowerSupply.discover()[0]
    w.manager._active[psu.instance_id] = psu

    out = assert_json_able(
        s.dispatch("device.get", {"instance_id": psu.instance_id}, scope="read"))
    assert out["instance_id"] == psu.instance_id
    assert out["driver"] == "fake_psu" and out["model"] == "SimPSU 30-5"
    assert out["hardware_id"] == "SIM-PSU-0001" and out["active"] is True
    assert {sk["id"] for sk in out["sinks"]} == {"enable", "voltage", "current_limit"}
    assert any(src["id"] == "voltage" for src in out["sources"])

    with pytest.raises(ControlError):
        s.dispatch("device.get", {"instance_id": "nope:0"}, scope="read")


@pytest.mark.ui
def test_device_remove_is_destructive_and_retires_active(control_surface):
    w, s = control_surface
    psu = fake.FakePowerSupply.discover()[0]
    w.manager._active[psu.instance_id] = psu

    with pytest.raises(ScopeError):
        s.dispatch("device.remove", {"instance_id": psu.instance_id}, scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("device.remove", {"instance_id": psu.instance_id},
                   scope="read", confirm=True)
    assert w.manager.is_active(psu.instance_id)

    out = assert_json_able(
        s.dispatch("device.remove", {"instance_id": psu.instance_id},
                   scope="control", confirm=True))
    assert out == {"ok": True, "instance_id": psu.instance_id,
                   "removed": psu.instance_id}
    assert w.manager.is_active(psu.instance_id) is False
    assert not any(d.instance_id == psu.instance_id
                   for d in w.manager.active_descriptors())

    with pytest.raises(ControlError):
        s.dispatch("device.remove", {"instance_id": psu.instance_id},
                   scope="control", confirm=True)


@pytest.mark.ui
def test_device_config_get_serializes_schema_and_masks_secret(control_surface):
    w, s = control_surface
    dev = _cfg_device()
    w.manager._active[dev.instance_id] = dev

    out = assert_json_able(
        s.dispatch("device.config_get", {"instance_id": "sim:cfg:1"}, scope="read"))
    opts = {o["key"]: o for o in out["options"]}
    assert opts["mode"]["value"] == "a"
    assert opts["mode"]["choices"] == [["a", "Alpha"], ["b", "Beta"]]
    assert opts["note"]["kind"] == "text" and opts["note"]["value"] == "hi"
    assert opts["auth_key"]["kind"] == "secret"
    assert opts["auth_key"]["value"] == "***"          # secret never leaked in the clear
    assert out["rate"]["settable"] is True and out["rate"]["hz"] == 2.0
    assert out["rate"]["min_hz"] == 0.5 and out["rate"]["max_hz"] == 10.0

    with pytest.raises(ControlError):
        s.dispatch("device.config_get", {"instance_id": "nope:0"}, scope="read")


@pytest.mark.ui
def test_device_config_set_applies_one_option(control_surface):
    w, s = control_surface
    dev = _cfg_device()
    w.manager._active[dev.instance_id] = dev

    out = assert_json_able(
        s.dispatch("device.config_set",
                   {"instance_id": "sim:cfg:1", "option": "mode", "value": "b"},
                   scope="control"))
    assert out == {"ok": True, "instance_id": "sim:cfg:1",
                   "option": "mode", "value": "b"}
    assert next(o.value for o in w.manager.descriptor("sim:cfg:1").options
                if o.key == "mode") == "b"

    with pytest.raises(ControlError):
        s.dispatch("device.config_set",
                   {"instance_id": "sim:cfg:1", "option": "nope", "value": 1},
                   scope="control")
    with pytest.raises(ControlError):
        s.dispatch("device.config_set",
                   {"instance_id": "gone:0", "option": "mode", "value": "a"},
                   scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("device.config_set",
                   {"instance_id": "sim:cfg:1", "option": "mode", "value": "a"},
                   scope="read")
    with pytest.raises(ControlError):
        s.dispatch("device.config_set", {"instance_id": "sim:cfg:1"}, scope="control")


@pytest.mark.ui
def test_device_rename_sets_friendly_name(control_surface):
    w, s = control_surface
    dev = _cfg_device()
    w.manager._active[dev.instance_id] = dev

    out = assert_json_able(
        s.dispatch("device.rename", {"instance_id": "sim:cfg:1", "name": "Bench A"},
                   scope="control"))
    assert out == {"ok": True, "instance_id": "sim:cfg:1", "name": "Bench A"}
    assert w.manager.descriptor("sim:cfg:1").name == "Bench A"

    with pytest.raises(ControlError):
        s.dispatch("device.rename", {"instance_id": "gone:0", "name": "x"},
                   scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("device.rename", {"instance_id": "sim:cfg:1", "name": "x"},
                   scope="read")
    with pytest.raises(ControlError):
        s.dispatch("device.rename", {"instance_id": "sim:cfg:1"}, scope="control")


@pytest.mark.ui
def test_device_set_rate_clamps_and_rejects_fixed(control_surface):
    w, s = control_surface
    dev = _cfg_device()
    w.manager._active[dev.instance_id] = dev

    out = assert_json_able(
        s.dispatch("device.set_rate", {"instance_id": "sim:cfg:1", "hz": 5.0},
                   scope="control"))
    assert out == {"ok": True, "instance_id": "sim:cfg:1", "hz": 5.0}
    clamp = s.dispatch("device.set_rate", {"instance_id": "sim:cfg:1", "hz": 100.0},
                       scope="control")
    assert clamp["hz"] == 10.0

    therm = fake.FakeThermometer.discover()[0]
    w.manager._active[therm.instance_id] = therm
    with pytest.raises(ControlError):
        s.dispatch("device.set_rate", {"instance_id": therm.instance_id, "hz": 3.0},
                   scope="control")

    with pytest.raises(ControlError):
        s.dispatch("device.set_rate", {"instance_id": "gone:0", "hz": 1.0},
                   scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("device.set_rate", {"instance_id": "sim:cfg:1", "hz": 1.0},
                   scope="read")


@pytest.mark.ui
def test_device_verbs_are_self_described(control_surface):
    _w, s = control_surface
    verbs = {v["name"]: v for v in s.describe()["verbs"]}
    assert verbs["device.get"]["kind"] == "query" and verbs["device.get"]["scope"] == "read"
    assert verbs["device.config_get"]["kind"] == "query"
    assert verbs["device.config_set"]["kind"] == "command"
    assert verbs["device.config_set"]["scope"] == "control"
    assert verbs["device.rename"]["scope"] == "control"
    assert verbs["device.set_rate"]["scope"] == "control"
    assert verbs["device.remove"]["destructive"] is True
    read_verbs = {v["name"] for v in s.describe(scope="read")["verbs"]}
    assert {"device.get", "device.config_get"} <= read_verbs
    assert "device.remove" not in read_verbs and "device.config_set" not in read_verbs


# -- projects ----------------------------------------------------------------
@pytest.mark.ui
def test_project_active_returns_full_metadata(control_surface):
    w, s = control_surface
    active = w._project_mgr.active
    assert active is not None
    out = assert_json_able(s.dispatch("project.active", scope="read"))
    assert set(out) == _FULL_KEYS
    assert out["id"] == active.id
    assert out["name"] == active.name
    assert out["path"] == active.path
    assert out["is_hub"] is False


@pytest.mark.ui
def test_project_info_by_id_and_errors(control_surface):
    w, s = control_surface
    active = w._project_mgr.active
    out = assert_json_able(s.dispatch("project.info", {"id": active.id}, scope="read"))
    assert set(out) == _FULL_KEYS
    assert out["id"] == active.id and out["name"] == active.name
    with pytest.raises(ControlError):
        s.dispatch("project.info", {"id": "no-such-id"}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("project.info", {}, scope="read")


@pytest.mark.ui
def test_project_create_registers_a_local_project(control_surface):
    w, s = control_surface
    before = w._project_mgr.active.id
    path = os.path.join(tempfile.mkdtemp(), "newproj")

    out = assert_json_able(
        s.dispatch("project.create", {"path": path, "name": "New Project"},
                   scope="control"))
    assert out["name"] == "New Project" and out["id"]
    assert os.path.abspath(out["path"]) == os.path.abspath(path)
    assert os.path.isfile(os.path.join(path, "project.json"))

    assert w._project_mgr.get(out["id"]) is not None
    assert w._project_mgr.active.id == before
    listed = s.dispatch("project.list", scope="read")
    assert any(pr["id"] == out["id"] for pr in listed["projects"])
    info = s.dispatch("project.info", {"id": out["id"]}, scope="read")
    assert info["name"] == "New Project"


@pytest.mark.ui
def test_project_create_scope_and_guards(control_surface):
    _w, s = control_surface
    path = os.path.join(tempfile.mkdtemp(), "p")
    with pytest.raises(ScopeError):
        s.dispatch("project.create", {"path": path}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("project.create", {}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("project.create", {"path": os.sep}, scope="control")


@pytest.mark.ui
def test_project_rename_changes_display_name(control_surface):
    w, s = control_surface
    out = assert_json_able(
        s.dispatch("project.rename", {"name": "Renamed"}, scope="control"))
    assert out["name"] == "Renamed"
    assert w._project_mgr.active.name == "Renamed"
    assert Project(w._project_mgr.active.path).name == "Renamed"

    second = w._project_mgr.track(os.path.join(tempfile.mkdtemp(), "p2"), "Second")
    s.dispatch("project.rename", {"id": second.id, "name": "Second Renamed"},
               scope="control")
    assert w._project_mgr.get(second.id).name == "Second Renamed"
    assert w._project_mgr.active.name == "Renamed"

    with pytest.raises(ControlError):
        s.dispatch("project.rename", {"name": "   "}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("project.rename", {}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("project.rename", {"id": "nope", "name": "x"}, scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("project.rename", {"name": "x"}, scope="read")


@pytest.mark.ui
def test_project_set_description(control_surface):
    w, s = control_surface
    out = assert_json_able(
        s.dispatch("project.set_description",
                   {"description": "measuring hysteresis"}, scope="control"))
    assert out["description"] == "measuring hysteresis"
    assert w._project_mgr.active.description == "measuring hysteresis"
    assert Project(w._project_mgr.active.path).description == "measuring hysteresis"

    with pytest.raises(ControlError):
        s.dispatch("project.set_description", {}, scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("project.set_description", {"description": "x"}, scope="read")


@pytest.mark.ui
def test_project_backup_writes_a_zip(control_surface):
    w, s = control_surface
    dest = os.path.join(tempfile.mkdtemp(), "backup")   # no .zip -> verb appends it

    out = assert_json_able(s.dispatch("project.backup", {"dest": dest}, scope="control"))
    assert out["ok"] is True
    assert out["path"].endswith(".zip") and os.path.isfile(out["path"])
    assert zipfile.is_zipfile(out["path"])
    with zipfile.ZipFile(out["path"]) as z:
        assert "project.json" in z.namelist()

    out2 = assert_json_able(s.dispatch("project.backup", scope="control"))
    assert out2["path"].endswith(".zip") and os.path.isfile(out2["path"])

    with pytest.raises(ScopeError):
        s.dispatch("project.backup", {"dest": dest}, scope="read")


@pytest.mark.ui
def test_project_verbs_require_a_project(control_surface):
    w, s = control_surface
    saved = w._project_mgr
    w._project_mgr = None
    try:
        for verb, payload, sc in (
            ("project.active", None, "read"),
            ("project.info", {"id": "x"}, "read"),
            ("project.rename", {"name": "x"}, "control"),
            ("project.set_description", {"description": "x"}, "control"),
            ("project.backup", None, "control"),
        ):
            with pytest.raises(ControlError):
                s.dispatch(verb, payload, scope=sc)
    finally:
        w._project_mgr = saved


@pytest.mark.ui
def test_project_verbs_are_self_described(control_surface):
    _w, s = control_surface
    verbs = {v["name"]: v for v in s.describe()["verbs"]}
    assert verbs["project.active"]["kind"] == "query"
    assert verbs["project.active"]["scope"] == "read"
    assert verbs["project.info"]["kind"] == "query"
    for name in ("project.create", "project.rename",
                 "project.set_description", "project.backup"):
        assert verbs[name]["kind"] == "command"
        assert verbs[name]["scope"] == "control"
        assert verbs[name]["destructive"] is False
    read_verbs = {v["name"] for v in s.describe(scope="read")["verbs"]}
    assert {"project.active", "project.info"} <= read_verbs
    assert "project.create" not in read_verbs


# -- replay transport --------------------------------------------------------
@pytest.mark.ui
def test_time_state_snapshot(control_surface):
    window, surface = control_surface
    assert window.time_context is not None
    st = assert_json_able(surface.dispatch("time.state", {}, scope="read"))
    assert st["available"] is True
    assert st["mode"] in ("live", "parked", "playing")
    for k in ("head", "now", "window", "width", "grow", "speed", "rate",
              "playing", "following", "moving"):
        assert k in st
    assert isinstance(st["window"], list) and len(st["window"]) == 2
    assert st["following"] is True
    assert st["mode"] == "live"
    assert st["grow"] is True


@pytest.mark.ui
def test_pause_then_go_live(control_surface):
    window, surface = control_surface
    tc = window.time_context
    r = assert_json_able(surface.dispatch("time.pause", {}, scope="control"))
    assert tc.following is False and tc.playing is False
    assert r["moving"] is False and r["mode"] == "parked"
    assert surface.dispatch("time.state", {}, scope="read")["mode"] == "parked"
    r = assert_json_able(surface.dispatch("time.go_live", {}, scope="control"))
    assert tc.following is True
    assert r["following"] is True and r["mode"] == "live"
    assert r["speed"] == 1.0


@pytest.mark.ui
def test_seek_parks_then_play_is_replay(control_surface):
    window, surface = control_surface
    tc = window.time_context
    target = time.time() - 3600.0
    r = assert_json_able(surface.dispatch("time.seek", {"t": target}, scope="control"))
    assert tc.following is False and tc.playing is False
    assert abs(tc.head - target) < 5.0
    assert r["following"] is False and r["mode"] == "parked"
    r = assert_json_able(surface.dispatch("time.play", {}, scope="control"))
    assert tc.playing is True and tc.following is False
    assert r["playing"] is True and r["mode"] == "playing"


@pytest.mark.ui
def test_seek_requires_t(control_surface):
    _, surface = control_surface
    with pytest.raises(ControlError):
        surface.dispatch("time.seek", {}, scope="control")


@pytest.mark.ui
def test_set_speed(control_surface):
    window, surface = control_surface
    tc = window.time_context
    r = assert_json_able(surface.dispatch("time.set_speed", {"speed": 4}, scope="control"))
    assert tc.speed == 4.0 and r["speed"] == 4.0
    with pytest.raises(ControlError):
        surface.dispatch("time.set_speed", {"speed": 0}, scope="control")
    with pytest.raises(ControlError):
        surface.dispatch("time.set_speed", {}, scope="control")


@pytest.mark.ui
def test_set_width(control_surface):
    window, surface = control_surface
    tc = window.time_context
    r = assert_json_able(surface.dispatch("time.set_width", {"seconds": 120}, scope="control"))
    assert tc.width == 120.0 and r["width"] == 120.0
    with pytest.raises(ControlError):
        surface.dispatch("time.set_width", {"seconds": 0}, scope="control")


@pytest.mark.ui
def test_set_grow(control_surface):
    window, surface = control_surface
    tc = window.time_context
    r = assert_json_able(surface.dispatch("time.set_grow", {"on": False}, scope="control"))
    assert tc.grow is False and r["grow"] is False
    r = assert_json_able(surface.dispatch("time.set_grow", {"on": True}, scope="control"))
    assert tc.grow is True and r["grow"] is True
    with pytest.raises(ControlError):
        surface.dispatch("time.set_grow", {}, scope="control")


@pytest.mark.ui
def test_set_mode(control_surface):
    window, surface = control_surface
    tc = window.time_context
    surface.dispatch("time.seek", {"t": time.time() - 3600.0}, scope="control")
    r = assert_json_able(surface.dispatch("time.set_mode", {"mode": "replay"}, scope="control"))
    assert tc.playing is True and tc.following is False and r["mode"] == "playing"
    r = assert_json_able(surface.dispatch("time.set_mode", {"mode": "live"}, scope="control"))
    assert tc.following is True and r["mode"] == "live"
    with pytest.raises(ControlError):
        surface.dispatch("time.set_mode", {"mode": "bogus"}, scope="control")


@pytest.mark.ui
def test_step(control_surface):
    window, surface = control_surface
    tc = window.time_context
    surface.dispatch("time.set_width", {"seconds": 600}, scope="control")
    surface.dispatch("time.seek", {"t": time.time() - 7200.0}, scope="control")
    h0 = tc.head
    r = assert_json_able(surface.dispatch("time.step", {"dir": "back"}, scope="control"))
    assert abs(tc.head - (h0 - 300.0)) < 1.0
    assert tc.following is False and r["following"] is False
    h1 = tc.head
    surface.dispatch("time.step", {"forward": True}, scope="control")
    assert abs(tc.head - (h1 + 300.0)) < 1.0
    with pytest.raises(ControlError):
        surface.dispatch("time.step", {"dir": "sideways"}, scope="control")


@pytest.mark.ui
def test_transport_commands_need_control_scope(control_surface):
    _, surface = control_surface
    with pytest.raises(ScopeError):
        surface.dispatch("time.pause", {}, scope="read")
