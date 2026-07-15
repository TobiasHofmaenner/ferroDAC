"""Batch 1 control-surface verbs dispatched against a REAL MainWindow.

sources / layout-routing / tags / docs — each verb is exercised end-to-end through
ControlSurface.dispatch (scope + destructive gates included) against the true model
objects, asserting the real return contracts and that nothing non-JSON-able leaks.
Marked `ui` (they build Qt) so the lightweight CI gate can skip them.
"""

import json
import os

import pytest

from ferrodac.core.control import ControlError, ScopeError
from ferrodac.core.reading import Reading
from ferrodac.ui.workspace import SourcePort


def assert_json_able(value):
    json.dumps(value)   # raises if a QObject/QImage/set/dataclass leaked through
    return value


# -- sources -----------------------------------------------------------------
@pytest.mark.ui
def test_source_list_serializes_the_real_port_catalog(control_surface):
    w, s = control_surface
    w.dashboard._sources["dev0/temp"] = SourcePort(
        "dev0/temp", "Temp", "float", "K", "PSU 1", "device")
    w.dashboard._sources["cam0/frame"] = SourcePort(
        "cam0/frame", "Frame", "image", "", "Cam 0", "device")

    listing = assert_json_able(s.dispatch("source.list", scope="read"))
    assert isinstance(listing, list)
    by_key = {e["key"]: e for e in listing}
    assert "dev0/temp" in by_key and "cam0/frame" in by_key
    assert by_key["dev0/temp"] == {
        "key": "dev0/temp", "name": "Temp", "dtype": "float", "unit": "K",
        "origin": "PSU 1", "kind": "device", "online": True}
    assert by_key["cam0/frame"]["dtype"] == "image"


@pytest.mark.ui
def test_source_read_joins_value_with_meta_and_guards_non_scalars(control_surface):
    w, s = control_surface
    w.dashboard._sources["dev0/temp"] = SourcePort(
        "dev0/temp", "Temp", "float", "K", "PSU 1", "device")
    w.dashboard._sources["cam0/frame"] = SourcePort(
        "cam0/frame", "Frame", "image", "", "Cam 0", "device")

    empty = assert_json_able(s.dispatch("source.read", {"key": "dev0/temp"}, scope="read"))
    assert empty == {"key": "dev0/temp", "name": "Temp", "unit": "K",
                     "dtype": "float", "value": None, "t": None}

    # Reading(device, source, t, value) -> key "device/source"
    w.dashboard.engine.publish(Reading("dev0", "temp", 123.0, 3.5))
    w.dashboard.engine.publish(Reading("cam0", "frame", 124.0, object()))  # image payload
    w.dashboard.engine.bus.drain()

    one = assert_json_able(s.dispatch("source.read", {"key": "dev0/temp"}, scope="read"))
    assert one["value"] == 3.5 and one["t"] == 123.0
    assert one["unit"] == "K" and one["dtype"] == "float"

    # an image source's Reading.value is NOT JSON-able — the verb must return a type
    # descriptor, never the raw payload; assert_json_able proves nothing leaked
    img = assert_json_able(s.dispatch("source.read", {"key": "cam0/frame"}, scope="read"))
    assert img["value"] == {"type": "image"}

    allv = assert_json_able(s.dispatch("source.read", scope="read"))
    keys = {e["key"] for e in allv}
    assert "dev0/temp" in keys and "cam0/frame" not in keys

    with pytest.raises(ControlError):
        s.dispatch("source.read", {"key": "nope/nope"}, scope="read")


@pytest.mark.ui
def test_source_read_nan_reads_as_null_not_a_crash(control_surface):
    # a float source whose latest reading is NaN (e.g. an offline channel) must read
    # as JSON null — a raw NaN would 500 the API response (Starlette allow_nan=False).
    w, s = control_surface
    w.dashboard._sources["dev0/t"] = SourcePort(
        "dev0/t", "T", "float", "K", "Dev", "device")
    w.dashboard.engine.publish(Reading("dev0", "t", 100.0, float("nan")))
    w.dashboard.engine.bus.drain()
    out = s.dispatch("source.read", {"key": "dev0/t"}, scope="read")
    assert out["value"] is None                 # NaN -> null, not a non-finite float
    w.dashboard.engine.publish(Reading("dev0", "t", 101.0, float("inf")))
    w.dashboard.engine.bus.drain()
    assert s.dispatch("source.read", {"key": "dev0/t"}, scope="read")["value"] is None


# -- layout routing ----------------------------------------------------------
@pytest.mark.ui
def test_layout_route_and_routes_dispatch(control_surface):
    w, s = control_surface
    db = w.dashboard
    db._sources["dev/temp"] = SourcePort(
        "dev/temp", "Temperature", "float", "°C", "dev", "device")
    pid = db.add_panel("chart")

    out = assert_json_able(
        s.dispatch("layout.route",
                   {"source_key": "dev/temp", "sink_key": pid}, scope="control"))
    assert out["ok"] is True and out["attached"] is True
    assert out["routed"] == [pid]                    # sorted list, JSON-able (not a set)
    assert pid in db.routed("dev/temp")

    routes = assert_json_able(s.dispatch("layout.routes", scope="read"))
    assert routes.get("dev/temp") == [pid]

    with pytest.raises(ScopeError):
        s.dispatch("layout.route",
                   {"source_key": "dev/temp", "sink_key": pid}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("layout.route", {"source_key": "dev/temp"}, scope="control")

    out2 = assert_json_able(
        s.dispatch("layout.unroute",
                   {"source_key": "dev/temp", "sink_key": pid}, scope="control"))
    assert out2["ok"] is True and out2["routed"] == [] and out2["attached"] is False
    assert pid not in db.routed("dev/temp")
    assert "dev/temp" not in s.dispatch("layout.routes", scope="read")


@pytest.mark.ui
def test_layout_remove_panel_is_destructive_and_drops_routes(control_surface):
    w, s = control_surface
    db = w.dashboard
    db._sources["dev/temp"] = SourcePort(
        "dev/temp", "Temperature", "float", "°C", "dev", "device")
    pid = db.add_panel("chart")
    s.dispatch("layout.route",
               {"source_key": "dev/temp", "sink_key": pid}, scope="control")
    assert pid in db.routed("dev/temp")

    # destructive: rejected without confirm, even at the required control scope
    with pytest.raises(ScopeError):
        s.dispatch("layout.remove_panel", {"panel_id": pid}, scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("layout.remove_panel", {"panel_id": pid}, scope="read", confirm=True)

    out = assert_json_able(
        s.dispatch("layout.remove_panel", {"panel_id": pid},
                   scope="control", confirm=True))
    assert out == {"ok": True, "removed": pid}
    assert db.panel(pid) is None
    assert pid not in db.routed("dev/temp")

    with pytest.raises(ControlError):
        s.dispatch("layout.remove_panel", {"id": "nope-999"},
                   scope="control", confirm=True)


@pytest.mark.ui
def test_layout_verbs_are_self_described(control_surface):
    _w, s = control_surface
    verbs = {v["name"]: v for v in s.describe()["verbs"]}
    assert verbs["layout.route"]["kind"] == "command"
    assert verbs["layout.route"]["scope"] == "control"
    assert verbs["layout.routes"]["kind"] == "query"
    assert verbs["layout.routes"]["scope"] == "read"
    assert verbs["layout.remove_panel"]["destructive"] is True
    read_verbs = {v["name"] for v in s.describe(scope="read")["verbs"]}
    assert "layout.remove_panel" not in read_verbs
    assert "layout.routes" in read_verbs


@pytest.mark.ui
def test_layout_rename_panel(control_surface):
    w, s = control_surface
    pid = w.dashboard.add_panel("chart")
    out = assert_json_able(
        s.dispatch("layout.rename_panel", {"panel_id": pid, "title": "Battery V"},
                   scope="control"))
    assert out == {"ok": True, "panel_id": pid, "title": "Battery V"}
    assert w.dashboard.panel(pid).title == "Battery V"          # canonical title updated
    got = {p["id"]: p["title"] for p in s.dispatch("layout.get", scope="read")["panels"]}
    assert got[pid] == "Battery V"                              # persisted in the layout

    with pytest.raises(ControlError):                           # title required
        s.dispatch("layout.rename_panel", {"panel_id": pid}, scope="control")
    with pytest.raises(ControlError):                           # unknown panel
        s.dispatch("layout.rename_panel", {"panel_id": "nope", "title": "x"}, scope="control")
    with pytest.raises(ScopeError):
        s.dispatch("layout.rename_panel", {"panel_id": pid, "title": "x"}, scope="read")


@pytest.mark.ui
def test_export_csv_and_window(control_surface, tmp_path):
    w, s = control_surface
    if getattr(w, "resolver", None) is None:
        pytest.skip("no resolver/durable store in this build")
    w.dashboard._sources["dev/x"] = SourcePort(
        "dev/x", "X", "float", "V", "Dev", "device")

    # export.csv (read) -> data.csv text over a window
    out = assert_json_able(
        s.dispatch("export.csv", {"t0": 1000.0, "t1": 1010.0}, scope="read"))
    assert out["t0"] == 1000.0 and out["t1"] == 1010.0
    assert isinstance(out["csv"], str)
    if out["csv"]:
        assert "time_epoch_s" in out["csv"]                # header written

    # export.window (control) -> a self-contained bundle on disk
    dest = str(tmp_path / "bundle")
    out2 = assert_json_able(
        s.dispatch("export.window", {"t0": 1000.0, "t1": 1010.0, "dest": dest},
                   scope="control"))
    assert out2["dest"] == dest and out2["manifest"].get("ferrodac_export")
    assert os.path.isfile(os.path.join(dest, "manifest.json"))

    with pytest.raises(ScopeError):                        # export.window is control
        s.dispatch("export.window", {"dest": dest}, scope="read")
    verbs = {v["name"]: v for v in s.describe()["verbs"]}
    assert verbs["export.csv"]["kind"] == "query" and verbs["export.csv"]["scope"] == "read"
    assert verbs["export.window"]["scope"] == "control"


# -- tags --------------------------------------------------------------------
@pytest.mark.ui
def test_tag_update_edits_metadata_against_real_markers(control_surface):
    w, s = control_surface
    mid = w.dashboard.markers.add(1000.0, label="old", comment="", severity="info")
    out = assert_json_able(s.dispatch(
        "tag.update",
        {"id": mid, "label": "new", "comment": "hi",
         "severity": "warn", "color": "#123456"}, scope="control"))
    assert out["id"] == mid
    assert out["label"] == "new" and out["comment"] == "hi"
    assert out["severity"] == "warn" and out["color"] == "#123456"
    m = w.dashboard.markers.get(mid)
    assert m.label == "new" and m.comment == "hi"
    assert m.severity == "warn" and m.color == "#123456"


@pytest.mark.ui
def test_tag_update_partial_leaves_other_fields(control_surface):
    w, s = control_surface
    mid = w.dashboard.markers.add(1000.0, label="keep", comment="orig", severity="info")
    s.dispatch("tag.update", {"id": mid, "comment": "changed"}, scope="control")
    m = w.dashboard.markers.get(mid)
    assert m.comment == "changed" and m.label == "keep" and m.severity == "info"


@pytest.mark.ui
def test_tag_update_scope_and_error_contracts(control_surface):
    w, s = control_surface
    mid = w.dashboard.markers.add(1000.0, label="x")
    with pytest.raises(ScopeError):
        s.dispatch("tag.update", {"id": mid, "label": "y"}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("tag.update", {"label": "y"}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("tag.update", {"id": "nope", "label": "y"}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("tag.update", {"id": mid}, scope="control")
    with pytest.raises(ControlError):
        s.dispatch("tag.update", {"id": mid, "severity": "boom"}, scope="control")


@pytest.mark.ui
def test_tag_update_allows_editing_immutable_tag_metadata(control_surface):
    w, s = control_surface
    # a MEDIA tag is immutable (its TIME is pinned) — but its metadata stays editable
    mid = w.dashboard.markers.add(1000.0, label="photo", kind="media")
    assert w.dashboard.markers.get(mid).immutable is True
    out = s.dispatch("tag.update", {"id": mid, "label": "renamed"}, scope="control")
    assert out["label"] == "renamed"
    m = w.dashboard.markers.get(mid)
    assert m.label == "renamed" and m.immutable is True


@pytest.mark.ui
def test_tag_remove_tombstones_against_real_markers(control_surface):
    w, s = control_surface
    mid = w.dashboard.markers.add(1000.0, label="doomed")
    out = assert_json_able(
        s.dispatch("tag.remove", {"id": mid}, scope="control", confirm=True))
    assert out == {"ok": True, "id": mid}
    # get() hides it (tombstone) but raw() still holds it with deleted=True
    assert w.dashboard.markers.get(mid) is None
    assert w.dashboard.markers.raw(mid).deleted is True


@pytest.mark.ui
def test_tag_remove_is_destructive_and_scoped(control_surface):
    w, s = control_surface
    mid = w.dashboard.markers.add(1000.0, label="doomed")
    with pytest.raises(ScopeError):
        s.dispatch("tag.remove", {"id": mid}, scope="control")
    assert w.dashboard.markers.get(mid) is not None
    with pytest.raises(ScopeError):
        s.dispatch("tag.remove", {"id": mid}, scope="read", confirm=True)
    with pytest.raises(ControlError):
        s.dispatch("tag.remove", {"id": "nope"}, scope="control", confirm=True)


# -- docs --------------------------------------------------------------------
@pytest.mark.ui
def test_doc_append_get_list_roundtrip_on_readme(control_surface):
    w, s = control_surface
    proj = w._project_mgr.active
    assert proj is not None

    out = assert_json_able(
        s.dispatch("doc.append", {"text": "\n## Observation\nspike at t=3\n"},
                   scope="control"))
    assert out["ok"] is True and out["name"] == "README.md"
    assert os.path.isfile(proj.readme_path)

    got = assert_json_able(s.dispatch("doc.get", {"name": "README.md"}, scope="read"))
    assert got["name"] == "README.md" and "spike at t=3" in got["text"]

    s.dispatch("doc.append", {"text": "## Observation 2\n"}, scope="control")
    got2 = s.dispatch("doc.get", {"name": "README.md"}, scope="read")
    assert "spike at t=3" in got2["text"] and "Observation 2" in got2["text"]

    listing = assert_json_able(s.dispatch("doc.list", scope="read"))
    assert any(d["name"] == "README.md" and d["kind"] == "readme" for d in listing)


@pytest.mark.ui
def test_doc_get_reads_docs_folder_guards_escape_and_scope(control_surface):
    w, s = control_surface
    proj = w._project_mgr.active

    ref = os.path.join(proj.docs_dir, "protocol.md")
    with open(ref, "w", encoding="utf-8") as fh:
        fh.write("step 1: calibrate\n")

    listing = s.dispatch("doc.list", scope="read")
    entry = next(d for d in listing if d["name"] == "protocol.md")
    assert entry["kind"] == "doc" and entry["ext"] == "md" and entry["path"] == ref

    got = assert_json_able(
        s.dispatch("doc.get", {"name": "docs/protocol.md"}, scope="read"))
    assert got["text"] == "step 1: calibrate\n"
    got2 = s.dispatch("doc.get", {"path": ref}, scope="read")
    assert got2["text"] == "step 1: calibrate\n"

    with pytest.raises(ControlError):
        s.dispatch("doc.get", {"name": "../../../../etc/passwd"}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("doc.get", {"name": "nope.md"}, scope="read")
    with pytest.raises(ScopeError):
        s.dispatch("doc.append", {"text": "x"}, scope="read")


@pytest.mark.ui
def test_doc_append_to_named_docs_file_stays_guarded(control_surface):
    w, s = control_surface
    proj = w._project_mgr.active
    out = assert_json_able(
        s.dispatch("doc.append", {"name": "docs/notes.md", "text": "line 1\n"},
                   scope="control"))
    assert out == {"ok": True, "name": "docs/notes.md"}
    assert os.path.isfile(os.path.join(proj.docs_dir, "notes.md"))
    got = s.dispatch("doc.get", {"name": "docs/notes.md"}, scope="read")
    assert got["text"] == "line 1\n"
    with pytest.raises(ControlError):
        s.dispatch("doc.append", {"name": "../evil.md", "text": "x"}, scope="control")


@pytest.mark.ui
def test_docs_require_an_active_project(control_surface):
    w, s = control_surface
    saved = w._project_mgr
    w._project_mgr = None
    try:
        for verb, payload, sc in (("doc.list", None, "read"),
                                  ("doc.get", {"name": "README.md"}, "read"),
                                  ("doc.append", {"text": "x"}, "control")):
            with pytest.raises(ControlError):
                s.dispatch(verb, payload, scope=sc)
    finally:
        w._project_mgr = saved
