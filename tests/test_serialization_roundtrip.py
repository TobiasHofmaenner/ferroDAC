"""Round-trip serialization tests for the hand-rolled tier — the serialization audit
(docs/AUDIT-SERIALIZATION-2026-07.md #7) found near-zero round-trip coverage here, so a
field added to the model but dropped by a serializer would ship silently. These convert
that class of drift into a red test. Qt-free where possible.

Also pins the two writers hardened to be atomic (Project.save, the session autosave):
a crash mid-write must not truncate the file (tmp+os.replace)."""
import json
import os
import tempfile

from ferrodac.core.tag import Marker, marker_from_dict, marker_to_dict
from ferrodac.core.projects import Project


def test_marker_disk_roundtrip_preserves_every_field_and_payload_types():
    m = Marker(
        id="abc123", t=1000.5, kind="calibration", label="cal run", comment="notes",
        color="#abcdef", t_end=1200.0, run_dir="/data/run7", origin_kind="device",
        origin_id="dev:1", scope="source:dev/p", severity="warn",
        payload={"count": 5, "ratio": 0.5, "ok": True, "note": "x"},   # NON-string values
        projects=["p1", "p2"], version=3, deleted=False)
    back = marker_from_dict(marker_to_dict(m))
    assert back == m                                     # dataclass __eq__ over all fields
    # the DISK form must NOT string-coerce payload values (the wire form does — audit #3)
    assert back.payload == {"count": 5, "ratio": 0.5, "ok": True, "note": "x"}
    assert type(back.payload["count"]) is int


def test_marker_from_dict_tolerates_a_minimal_and_a_broken_record():
    assert marker_from_dict({"id": "x", "t": 1.0}).kind == "tag"    # defaults fill in
    assert marker_from_dict({}) is None                              # no id → None, no crash


def test_project_record_roundtrip():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "p"), "My Project")
    p.set_meta(description="desc", origin_id="o1")
    p.set_git_remote("https://git/o/p.git")
    p.set_sources([{"key": "dev/a"}, {"key": "dev/b"}])
    rec = p.to_record()
    q = Project(os.path.join(d, "q"))
    q.apply_record(rec)
    assert q.id == p.id and q.name == "My Project" and q.description == "desc"
    assert q.git_remote == "https://git/o/p.git"
    assert q.source_keys() == {"dev/a", "dev/b"}
    assert q.version == p.version


def test_project_save_is_atomic():
    """Project.save uses tmp+os.replace — no partial file, no leftover .tmp, valid JSON."""
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "p"), "P")
    p.set_meta(description="hardened")
    meta_path = os.path.join(p.path, "project.json")
    assert not os.path.exists(meta_path + ".tmp")        # temp cleaned up by os.replace
    with open(meta_path, encoding="utf-8") as fh:
        on_disk = json.load(fh)                          # complete + parseable
    assert on_disk["description"] == "hardened" and on_disk["id"] == p.id
