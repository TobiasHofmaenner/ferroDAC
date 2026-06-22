"""The hub stores projects as real, mountable project FOLDERS (DESIGN §8.1).

A hub project is a shared experiment index — same reliable/LWW/tombstoned model
as tags — but persisted as the exact same folder layout a local project uses
(project.json, layouts/, sources.json), written by ferrodac.core.projects. So the
hub's projects dir is mountable and a project opens as-if-local. This pins that:
publish writes a real folder, a fresh hub reloads from the folders, LWW + delete
work, and the folder round-trips through the local Project class.
"""

import os
import tempfile

import pytest

pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")
from google.protobuf import json_format                      # noqa: E402
from ferrodac_contract.v1 import data_plane_pb2 as pb       # noqa: E402
from hub.core import Hub                                     # noqa: E402
from ferrodac.core.projects import Project, is_project       # noqa: E402


def _rec(pid, name, version=1, **kw):
    p = pb.Project(id=pid, name=name, version=version)
    for k, v in kw.items():
        if k == "sources":
            p.sources.extend(v)
        elif k == "windows":
            for w in v:
                p.windows.add(name=w[0], t0=w[1], t1=w[2])
        elif k == "layouts":
            for ln, blob in v.items():
                p.layouts[ln] = blob
        else:
            setattr(p, k, v)
    return p


def test_hub_writes_mountable_project_folder():
    d = tempfile.mkdtemp()
    h = Hub(projects_dir=d)
    assert h.publish_project(_rec(
        "p1", "Bakeout study", version=1,
        sources=["mg/ch1", "psu/v"],
        windows=[("ramp", 1000.0, 1600.0)],
        layouts={"overview": '{"layout": {"panels": [{"id": "a"}]}}'}))

    # it's a REAL project folder — openable by the local Project class (mountable)
    folder = os.path.join(d, "p1")
    assert is_project(folder)
    local = Project(folder)
    assert local.id == "p1" and local.name == "Bakeout study"
    assert local.source_keys() == {"mg/ch1", "psu/v"}
    assert [w["name"] for w in local.windows()] == ["ramp"]
    assert local.layouts() == ["overview"] and local.layout_panels("overview") == 1


def test_hub_reloads_projects_from_folders():
    d = tempfile.mkdtemp()
    Hub(projects_dir=d).publish_project(_rec("p1", "Exp 1", version=1,
                                             sources=["a/b"]))
    # a fresh hub (restart) rebuilds its cache by SCANNING the folders
    h2 = Hub(projects_dir=d)
    snap = {p.id: p for p in h2.project_snapshot()}
    assert set(snap) == {"p1"} and snap["p1"].name == "Exp 1"
    assert list(snap["p1"].sources) == ["a/b"]


def test_hub_project_lww_and_delete():
    d = tempfile.mkdtemp()
    h = Hub(projects_dir=d)
    h.publish_project(_rec("p1", "v1", version=1))
    h.publish_project(_rec("p1", "v2", version=2))                 # LWW edit wins
    assert not h.publish_project(_rec("p1", "stale", version=1))   # older → rejected
    assert Hub(projects_dir=d).project_snapshot()[0].name == "v2"

    # delete removes the folder; a reloaded hub no longer has it
    h.delete_project("p1", version=2)
    assert not is_project(os.path.join(d, "p1"))
    assert Hub(projects_dir=d).project_snapshot() == []


def test_local_project_round_trips_through_hub():
    """A project authored locally, published to the hub, comes back identical —
    same id/name/lens/bookmarks (the share-to-hub path serialises a folder)."""
    src = Project.create(os.path.join(tempfile.mkdtemp(), "exp"), "Round trip")
    src.set_sources([{"key": "x/y"}])
    src.add_window("interesting", 5.0, 9.0)
    rec = src.to_record()

    d = tempfile.mkdtemp()
    Hub(projects_dir=d).publish_project(json_format.ParseDict(rec, pb.Project()))
    back = Project(os.path.join(d, src.id))
    assert back.name == "Round trip" and back.source_keys() == {"x/y"}
    assert [(w["name"], w["t0"], w["t1"]) for w in back.windows()] == [("interesting", 5.0, 9.0)]


def test_no_dir_is_in_memory_only():
    h = Hub()                                    # no projects_dir configured
    assert h.publish_project(_rec("x", "ram", version=1))
    assert len(h.project_snapshot()) == 1        # works in RAM, just not persisted


def test_gitea_provision_creates_repo_and_authed_url():
    """The provisioner ensures the org+repo and returns a token-authed PUBLIC URL."""
    import urllib.error
    from hub.gitea import GiteaProvisioner
    g = GiteaProvisioner("http://gitea:3000", "tok123", org="ferrodac", user="ferrodac",
                         public_url="https://git.example.com")
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET" and path.startswith("/repos/"):
            raise urllib.error.HTTPError(path, 404, "nf", None, None)
        return 201, {}

    g._api = fake_api
    url = g.provision("proj-1")
    assert ("POST", "/orgs", {"username": "ferrodac"}) in calls
    assert ("POST", "/orgs/ferrodac/repos",
            {"name": "proj-1", "private": True, "auto_init": False}) in calls
    assert url == "https://ferrodac:tok123@git.example.com/ferrodac/proj-1.git"


def test_gitea_provision_is_defensive():
    from hub.gitea import GiteaProvisioner
    g = GiteaProvisioner("http://gitea:3000", "t")

    def boom(*a, **k):
        raise RuntimeError("gitea down")

    g._api = boom
    assert g.provision("x") == ""          # any failure → "" (hub leaves git_remote unset)


def test_hub_provisions_git_remote_once(tmp_path):
    """The transparent dial: a project with no remote gets one provisioned (once)."""
    class FakeGitea:
        def __init__(self):
            self.calls = []

        def provision(self, pid):
            self.calls.append(pid)
            return f"https://t@git/{pid}.git"

    hub = Hub(projects_dir=str(tmp_path / "p"), gitea=FakeGitea())
    assert hub.publish_project(_rec("P1", "Proj", version=1))
    assert hub._projects["P1"].git_remote == "https://t@git/P1.git"
    rec2 = _rec("P1", "Proj", version=2)
    rec2.git_remote = "https://t@git/P1.git"        # already carries a remote
    assert hub.publish_project(rec2)
    assert hub.gitea.calls == ["P1"]                # provisioned exactly once
