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


def test_strip_credentials():
    from ferrodac.core.projectgit import strip_credentials
    assert strip_credentials("https://user:tok@git.example.com/o/r.git") \
        == "https://git.example.com/o/r.git"
    assert strip_credentials("https://git.example.com/o/r.git") \
        == "https://git.example.com/o/r.git"          # already clean → unchanged
    assert strip_credentials("git@github.com:o/r.git") == "git@github.com:o/r.git"  # SSH
    assert strip_credentials("") == ""


def test_set_remote_never_stores_a_token(tmp_path):
    """A `user:token@` credential in a remote URL is stripped before it touches
    .git/config — the leak fix (the token used to sit in config → backups → zips)."""
    from ferrodac.core.projectgit import ProjectRepo
    r = ProjectRepo(str(tmp_path / "p"))
    r.set_remote("https://ferrodac:SECRET@git.example.com/o/proj.git")
    assert r.remote_url() == "https://git.example.com/o/proj.git"
    cfg = (tmp_path / "p" / ".git" / "config").read_text()
    assert "SECRET" not in cfg


def test_sanitize_origin_scrubs_a_pre_fix_token(tmp_path):
    """Self-heal: an origin written with an embedded token before the fix gets
    scrubbed in place (idempotently)."""
    import subprocess
    from ferrodac.core.projectgit import ProjectRepo
    p = tmp_path / "p"
    p.mkdir()
    r = ProjectRepo(str(p))
    r.init()
    subprocess.run(["git", "-C", str(p), "remote", "add", "origin",
                    "https://u:TOKEN@git/o/proj.git"], check=True)
    assert "TOKEN" in (p / ".git" / "config").read_text()
    assert r.sanitize_origin() is True
    assert r.remote_url() == "https://git/o/proj.git"
    assert "TOKEN" not in (p / ".git" / "config").read_text()
    assert r.sanitize_origin() is False               # already clean → no-op


def test_credential_helper_keeps_the_secret_out_of_argv():
    """The ephemeral credential is injected via a helper that reads the token from
    the ENV — so it never appears in argv (visible to `ps`) or on disk."""
    from ferrodac.core.projectgit import _credential_helper
    args, env = _credential_helper(("user", "SECRET"))
    assert "SECRET" not in " ".join(args)             # secret not in the command line
    assert env == {"FD_GIT_USER": "user", "FD_GIT_PASS": "SECRET"}
    assert _credential_helper(None) == ([], {})       # no cred → no injection
    assert _credential_helper(("u", "")) == ([], {})  # no password → no injection


def test_push_with_cred_does_not_persist_the_token(tmp_path):
    """Passing an ephemeral credential to push() must not write it to .git/config."""
    import subprocess
    from ferrodac.core.projectgit import ProjectRepo
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    a = tmp_path / "a"
    a.mkdir()
    ra = ProjectRepo(str(a))
    (a / "f.txt").write_text("one\n")
    ra.commit("first")
    ra.set_remote(str(bare))
    ok, msg = ra.push(cred=("user", "SECRET"))        # local remote ignores it — harmless
    assert ok, msg
    assert "SECRET" not in (a / ".git" / "config").read_text()


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
