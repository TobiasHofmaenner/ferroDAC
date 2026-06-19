"""Regression tests for the Project model (ferrodac.core.projects). Qt-free."""

import json
import os
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


def test_manager_scan_default_and_active():
    root = tempfile.mkdtemp()
    mgr = ProjectManager(root)
    assert mgr.projects() == []
    # ensure_default makes one so the app always has a home
    d = mgr.ensure_default()
    assert d.name == "Default"
    assert [p.name for p in ProjectManager(root).scan()] == ["Default"]   # on disk

    # create more + activate
    e = mgr.create("Experiment 1")
    assert {p.name for p in mgr.projects()} == {"Default", "Experiment 1"}
    assert mgr.set_active(e.id) and mgr.active.id == e.id
    assert not mgr.set_active("nonexistent-id")

    # a fresh manager rediscovers them from disk; active resets until re-set
    mgr2 = ProjectManager(root)
    mgr2.scan()
    assert {p.name for p in mgr2.projects()} == {"Default", "Experiment 1"}
    assert mgr2.active is None and mgr2.set_active(e.id)


def test_unique_folder_names():
    root = tempfile.mkdtemp()
    mgr = ProjectManager(root)
    a = mgr.create("My Run")
    b = mgr.create("My Run")            # same display name → distinct folders + ids
    assert a.path != b.path and a.id != b.id
    assert len(ProjectManager(root).scan()) == 2
