"""Regression tests for the Project model + registry (ferrodac.core.projects)."""

import json
import os
import shutil
import tempfile

from ferrodac.core.projects import Project, ProjectManager, is_project


def test_create_and_layout_scan():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "exp1"), "Experiment 1", "a description")
    assert is_project(p.path) and p.id and p.name == "Experiment 1"
    # project.json holds META only; the subfolders exist
    meta = json.load(open(os.path.join(p.path, "project.json")))
    assert meta["name"] == "Experiment 1" and "id" in meta and "layouts" not in meta
    for sub in ("layouts", "docs", "reports"):
        assert os.path.isdir(os.path.join(p.path, sub))

    # the folder IS the list — drop a layout file, it shows up on scan
    assert p.layouts() == []
    open(p.layout_path("overview"), "w").write("{}")
    assert p.layouts() == ["overview"]
    # parsed for the Explorer: panel count (0 here / unparseable → 0)
    assert p.layout_panels("overview") == 0
    json.dump({"layout": {"panels": [{"id": "a"}, {"id": "b"}]}},
              open(p.layout_path("rich"), "w"))
    assert p.layout_panels("rich") == 2


def test_curated_sources_round_trip():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "exp1"), "Experiment 1")
    assert p.sources() == [] and p.source_keys() == set()    # nothing curated yet
    p.set_sources([{"key": "dev/p1"}, {"key": "dev/temp"}])
    assert p.source_keys() == {"dev/p1", "dev/temp"}         # the lens selection
    # persists to sources.json (NOT into project.json — meta stays clean)
    assert json.load(open(os.path.join(p.path, "sources.json")))["sources"][0]["key"] == "dev/p1"
    assert "sources" not in json.load(open(os.path.join(p.path, "project.json")))
    # a fresh handle re-reads the selection from disk
    assert Project(p.path).source_keys() == {"dev/p1", "dev/temp"}


def test_recordings_scan_and_parse():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "exp1"), "Experiment 1")
    assert p.recordings() == []                              # nothing recorded yet

    def _run(name, t0, t1, nsrc, exported, ntags=0):
        run = os.path.join(p.reports_dir, name)
        os.makedirs(run)
        man = {"t0": t0, "t1": t1, "exported_at": exported,
               "sources": [{"key": f"s{i}"} for i in range(nsrc)]}
        if ntags:
            man["tags"] = ntags
        json.dump(man, open(os.path.join(run, "manifest.json"), "w"))

    _run("run_A", 1000.0, 1060.0, 3, "2026-06-19T10:00:00Z", ntags=2)
    _run("run_B", 2000.0, 2030.0, 1, "2026-06-19T12:00:00Z")
    os.makedirs(os.path.join(p.reports_dir, "not_a_run"))   # no manifest → ignored
    recs = p.recordings()
    assert [r["name"] for r in recs] == ["run_B", "run_A"]   # newest (exported_at) first
    a = next(r for r in recs if r["name"] == "run_A")
    assert a["sources"] == 3 and a["tags"] == 2 and a["t1"] - a["t0"] == 60.0


def test_registry_track_create_adopt_and_active():
    d = tempfile.mkdtemp()
    reg = os.path.join(d, "projects.json")
    mgr = ProjectManager(reg)
    assert mgr.projects() == []

    # ensure_default creates a built-in Default + registers it as active
    default = mgr.ensure_default(default_dir=os.path.join(d, "default_proj"))
    assert default.name == "Default" and mgr.active.id == default.id
    assert os.path.exists(reg)

    # track a NEW folder (create there) and an EXISTING project folder (adopt)
    p1 = mgr.track(os.path.join(d, "exp1"), "Experiment 1")
    existing = Project.create(os.path.join(d, "elsewhere"), "Adopted")
    p2 = mgr.track(existing.path)                 # adopt — same id, no name needed
    assert p2.id == existing.id and p2.name == "Adopted"
    assert {p.name for p in mgr.projects()} == {"Default", "Experiment 1", "Adopted"}

    # active persists in the registry; a fresh manager reloads paths + active
    mgr.set_active(p1.id)
    mgr2 = ProjectManager(reg)
    assert {p.name for p in mgr2.projects()} == {"Default", "Experiment 1", "Adopted"}
    assert mgr2.active.id == p1.id


def test_legacy_root_migration():
    d = tempfile.mkdtemp()
    legacy = os.path.join(d, "legacy")
    Project.create(os.path.join(legacy, "old1"), "Old 1")
    Project.create(os.path.join(legacy, "old2"), "Old 2")
    mgr = ProjectManager(os.path.join(d, "projects.json"))
    mgr.ensure_default(default_dir=os.path.join(d, "def"), legacy_root=legacy)
    # adopts the projects already in the legacy root instead of making a Default
    assert {p.name for p in mgr.projects()} == {"Old 1", "Old 2"}


def test_missing_folder_is_dropped():
    d = tempfile.mkdtemp()
    reg = os.path.join(d, "projects.json")
    p = ProjectManager(reg).track(os.path.join(d, "p1"), "P1")
    shutil.rmtree(p.path)                          # folder removed out from under us
    assert ProjectManager(reg).projects() == []    # reload silently drops it
