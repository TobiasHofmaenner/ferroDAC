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


def test_projectrepo_is_defensive(tmp_path):
    """A commit never raises — a missing dir / odd state just returns None."""
    from ferrodac.core.projectgit import ProjectRepo
    repo = ProjectRepo(str(tmp_path / "does-not-exist-yet"))
    sha = repo.commit("create")                          # inits + commits the empty dir
    # an empty new dir has nothing to commit → None, and no exception
    assert sha is None or len(sha) == 40
