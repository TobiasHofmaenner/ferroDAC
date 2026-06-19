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
