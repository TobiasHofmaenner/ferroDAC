"""Regression tests for the Project model + registry (ferrodac.core.projects)."""

import json
import os
import shutil
import tempfile

from ferrodac.core.projects import (
    HubProject,
    Project,
    ProjectManager,
    is_project,
)


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


def test_docs_scan():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "exp1"), "Experiment 1")
    assert p.docs() == []                                    # nothing dropped yet
    open(os.path.join(p.docs_dir, "protocol.md"), "w").write("# notes")
    open(os.path.join(p.docs_dir, "datasheet.pdf"), "w").write("%PDF")
    os.makedirs(os.path.join(p.docs_dir, "subdir"))         # dirs are ignored
    docs = p.docs()
    assert [x["name"] for x in docs] == ["datasheet.pdf", "protocol.md"]   # sorted, files only
    assert {x["ext"] for x in docs} == {"pdf", "md"}


def test_saved_windows_round_trip():
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "exp1"), "Experiment 1")
    assert p.windows() == []
    p.add_window("bakeout", 1000.0, 1600.0)
    p.add_window("pumpdown", 2000.0, 2300.0)
    assert [w["name"] for w in p.windows()] == ["bakeout", "pumpdown"]
    # same name replaces (no dupes); persists to project.json meta
    p.add_window("bakeout", 1100.0, 1700.0)
    assert len(p.windows()) == 2
    assert next(w for w in p.windows() if w["name"] == "bakeout")["t0"] == 1100.0
    assert Project(p.path).windows() == p.windows()         # reloads from disk
    p.remove_window("bakeout")
    assert [w["name"] for w in p.windows()] == ["pumpdown"]


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


# -- hub projects (client side: synced records cached as mountable folders) --
def _rec(pid, name, version=1, sources=None, windows=None, deleted=False):
    return {"id": pid, "name": name, "version": version, "deleted": deleted,
            "sources": sources or [], "windows": windows or [], "layouts": {}}


def test_apply_hub_record_materialises_mountable_cache():
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "projects.json"),
                         hub_cache_dir=os.path.join(d, "hub_cache"))
    p = mgr.apply_hub_record(_rec("h1", "Shared exp", sources=["mg/ch1"],
                                  windows=[{"name": "bk", "t0": 1.0, "t1": 2.0}]))
    assert isinstance(p, HubProject) and p.is_hub is True
    assert mgr.get("h1") is p and p in mgr.projects()      # merged with local
    # the cache is a REAL project folder (mountable / openable as local)
    assert is_project(p.path) and Project(p.path).source_keys() == {"mg/ch1"}
    assert [w["name"] for w in p.windows()] == ["bk"]


def test_apply_hub_record_lww():
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "projects.json"))
    mgr.apply_hub_record(_rec("h1", "v1", version=1))
    mgr.apply_hub_record(_rec("h1", "v3", version=3))         # newer wins
    mgr.apply_hub_record(_rec("h1", "stale", version=2))      # older ignored
    assert mgr.get("h1").name == "v3"
    # a tombstone drops it
    assert mgr.apply_hub_record(_rec("h1", "", version=4, deleted=True)) is None
    assert mgr.get("h1") is None


def test_clear_hub_falls_back_to_local_active():
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "projects.json"))
    local = mgr.track(os.path.join(d, "loc"), "Local")
    mgr.apply_hub_record(_rec("h1", "Hub one"))
    mgr.set_active("h1")
    assert mgr.active.id == "h1"
    mgr.clear_hub()                                  # disconnect → hub projects vanish
    assert mgr.get("h1") is None and mgr.active.id == local.id   # fell back to local


def test_hubproject_bump_and_share_to_hub():
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "projects.json"))
    h = mgr.apply_hub_record(_rec("h1", "Exp", version=2))
    h.set_sources([{"key": "a/b"}])
    rec = h.bump()                                   # a local edit → publish this
    assert rec["version"] == 3 and rec["sources"] == ["a/b"]
    # promote a local project: share_to_hub yields its record (same id)
    local = mgr.track(os.path.join(d, "loc"), "Local")
    local.add_window("w", 1.0, 5.0)
    shared = mgr.share_to_hub(local.id)
    assert shared["id"] == local.id and shared["windows"][0]["name"] == "w"


def test_record_carries_git_remote():
    """The shared repo URL travels in the project record (§8.2 — the hub indexes it)."""
    d = tempfile.mkdtemp()
    p = Project.create(os.path.join(d, "p"), "P")
    p.set_git_remote("https://example/repo.git")
    rec = p.to_record()
    assert rec["git_remote"] == "https://example/repo.git"
    q = Project(os.path.join(d, "q"))
    q.apply_record(rec)
    assert q.git_remote == "https://example/repo.git"


def test_projects_dedup_local_working_copy_over_hub_cache():
    """A LOCAL checkout (same id) hides the hub CACHE entry — your clone is the copy."""
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "registry.json"),
                         hub_cache_dir=os.path.join(d, "hub"))
    rec = {"id": "X", "name": "Shared", "version": 1, "sources": [], "windows": [],
           "layouts": {}, "deleted": False, "git_remote": "u"}
    mgr.apply_hub_record(rec)
    assert len(mgr.projects()) == 1 and mgr.projects()[0].is_hub
    # materialise a LOCAL folder with the same id (what a clone would have) + track it
    local = Project(os.path.join(d, "clone"))
    local.apply_record(rec)
    mgr.track(os.path.join(d, "clone"))
    projs = mgr.projects()
    assert len(projs) == 1 and projs[0].is_hub is False     # local working copy wins
    assert mgr.get("X").is_hub is False


def test_is_on_hub_recognises_local_working_copy():
    """A LOCAL clone of a shared project is still 'on the hub' (collab-eligible),
    even though is_hub is False after the dedup."""
    d = tempfile.mkdtemp()
    mgr = ProjectManager(os.path.join(d, "reg.json"), hub_cache_dir=os.path.join(d, "hub"))
    rec = {"id": "X", "name": "S", "version": 1, "sources": [], "windows": [],
           "layouts": {}, "deleted": False}
    mgr.apply_hub_record(rec)
    assert mgr.is_on_hub("X")
    local = Project(os.path.join(d, "clone"))
    local.apply_record(rec)
    mgr.track(os.path.join(d, "clone"))               # working copy (same id)
    assert mgr.get("X").is_hub is False               # local copy wins the dedup
    assert mgr.is_on_hub("X")                          # …but it's still shared on the hub
    assert not mgr.is_on_hub("nope")
