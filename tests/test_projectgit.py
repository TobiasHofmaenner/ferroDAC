"""Local project git history (DESIGN §8.2). Qt-free, offline."""
import os


def test_projectrepo_lifecycle(tmp_path):
    from ferrodac.core.projectgit import ProjectRepo
    proj = tmp_path / "proj"
    proj.mkdir()
    repo = ProjectRepo(str(proj))
    assert not repo.is_repo()
    assert repo.log() == []                              # no repo → empty history

    # first commit inits the repo and records the current files
    (proj / "README.md").write_text("# Project\n")
    os.makedirs(proj / "reports" / "run1")
    (proj / "reports" / "run1" / "data.csv").write_text("t,v\n1,2\n")
    sha = repo.commit("Recorded run1")
    assert repo.is_repo() and sha and len(sha) == 40

    # nothing changed → no empty commit
    assert repo.commit("noop") is None

    # a change → a new commit, newest first in the log
    (proj / "README.md").write_text("# Project\n\nedited\n")
    assert repo.is_dirty()
    sha2 = repo.commit("Edited documents")
    assert sha2 and sha2 != sha
    hist = repo.log()
    assert [h["message"] for h in hist] == ["Edited documents", "Recorded run1"]
    assert all(len(h["sha"]) == 40 and h["time"] > 0 for h in hist)


def test_projectrepo_push_pull(tmp_path):
    """Set a remote, push, and pull — round-tripped through a local bare repo (offline)."""
    import subprocess
    from ferrodac.core.projectgit import ProjectRepo
    bare = tmp_path / "remote.git"                       # a bare repo = the "remote"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)

    a = tmp_path / "a"
    a.mkdir()
    ra = ProjectRepo(str(a))
    (a / "f.txt").write_text("one\n")
    ra.commit("first")
    assert ra.remote_url() == ""
    ra.set_remote(str(bare))
    assert ra.remote_url() == str(bare)
    ok, msg = ra.push()
    assert ok, msg

    b = str(tmp_path / "b")                               # a second checkout of the remote
    ProjectRepo.clone(str(bare), b)
    rb = ProjectRepo(b)
    (tmp_path / "b" / "g.txt").write_text("two\n")
    assert rb.commit("second")
    assert rb.push()[0]

    assert ra.pull()[0]                                   # A pulls B's commit
    msgs = [h["message"] for h in ra.log()]
    assert "first" in msgs and "second" in msgs


def test_projectrepo_remote_op_without_remote(tmp_path):
    from ferrodac.core.projectgit import ProjectRepo
    r = ProjectRepo(str(tmp_path / "p"))
    ok, msg = r.push()
    assert not ok and "remote" in msg.lower()            # graceful, no crash


def test_projectrepo_is_defensive(tmp_path):
    """A commit never raises — a missing dir / odd state just returns None."""
    from ferrodac.core.projectgit import ProjectRepo
    repo = ProjectRepo(str(tmp_path / "does-not-exist-yet"))
    sha = repo.commit("create")                          # inits + commits the empty dir
    # an empty new dir has nothing to commit → None, and no exception
    assert sha is None or len(sha) == 40


def test_commit_with_author(tmp_path):
    """A commit can be attributed to the real user (name + email)."""
    import subprocess
    from ferrodac.core.projectgit import ProjectRepo
    p = tmp_path / "p"
    p.mkdir()
    (p / "f.txt").write_text("x\n")
    repo = ProjectRepo(str(p))
    assert repo.commit("with author", author=("Ada Lovelace", "ada@example.com"))
    out = subprocess.run(["git", "-C", str(p), "log", "-1", "--format=%an|%ae"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "Ada Lovelace|ada@example.com"
