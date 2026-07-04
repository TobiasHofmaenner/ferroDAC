"""A project must never be a system/home ROOT (the '/home became a git repo' bug):
new projects create a dedicated subfolder, and system roots are refused at every
layer. Qt-free — runs in the fast CI gate.
"""
import os

import pytest

from ferrodac.core.projects import (Project, ProjectManager, is_project,
                                     unsafe_project_dir)


def test_unsafe_dirs_flagged():
    home = os.path.abspath(os.path.expanduser("~"))
    for bad in ("/", "/home", "/usr", "/etc", "/tmp", home, os.path.dirname(home)):
        assert unsafe_project_dir(bad), f"{bad} should be flagged"


def test_normal_dirs_ok(tmp_path):
    assert unsafe_project_dir(str(tmp_path / "My Project")) == ""
    assert unsafe_project_dir(str(tmp_path)) == ""


def test_create_refuses_system_root():
    with pytest.raises(ValueError):
        Project.create("/home", "Boom")            # would git-repo the whole /home
    with pytest.raises(ValueError):
        Project.create(os.path.expanduser("~"), "Boom")


def test_create_makes_a_real_project_in_a_subfolder(tmp_path):
    dest = str(tmp_path / "proj")
    p = Project.create(dest, "Proj")
    assert is_project(dest) and p.name == "Proj"


def test_commit_refuses_unsafe_path():
    from ferrodac.core.projectgit import ProjectRepo
    assert ProjectRepo("/home").commit("x") is None    # never scans /home


def test_registry_drops_unsafe_entries(tmp_path, monkeypatch):
    import json
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": ["/home", str(tmp_path / "good")],
                               "active": ""}))
    Project.create(str(tmp_path / "good"), "Good")
    mgr = ProjectManager(str(reg))
    paths = [p.path for p in mgr.projects()]
    assert "/home" not in paths and str(tmp_path / "good") in paths
