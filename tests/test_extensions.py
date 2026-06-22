"""The extension loader (Phase 3): discovery, loading, persistence.

Loading is Qt-free for processors/drivers; the widget-loading case needs Qt (marked
ui). The in-tree examples/ferrodac-ext-example/ is the single-extension fixture.
"""
import os

import pytest

EX = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                  "examples", "ferrodac-ext-example"))


def _write_proc_ext(root, name, kind, pkg):
    """Scaffold a fresh single-extension repo with a unique processor + package."""
    p = root / pkg
    p.mkdir()
    (p / "__init__.py").write_text("")
    (p / "proc.py").write_text(
        "from ferrodac.plugin import Port, Processor, register_processor\n"
        "@register_processor\n"
        "class P(Processor):\n"
        f"    kind = {kind!r}\n"
        f"    label = {kind!r}\n"
        "    def outputs(self): return [Port('x/y', 'y')]\n"
        "    def process(self, v): return {}\n")
    (root / "ferrodac-extension.toml").write_text(
        f'[extension]\nname = "{name}"\napi = 1\n'
        f'[[processors]]\nentry = "{pkg}.proc:P"\n')


def test_discover_single_and_monorepo(tmp_path):
    from ferrodac.extensions import discover_extensions
    # single-extension repo (the in-tree fixture has a root manifest)
    single = discover_extensions(EX)
    assert [m.name for m in single] == ["ferrodac-ext-example"]
    # monorepo: each immediate subdir with a manifest is one extension
    for name in ("a", "b"):
        d = tmp_path / name
        d.mkdir()
        (d / "ferrodac-extension.toml").write_text(f'[extension]\nname="{name}"\napi=1\n')
    (tmp_path / "c").mkdir()                              # a subdir with no manifest → ignored
    assert sorted(m.name for m in discover_extensions(str(tmp_path))) == ["a", "b"]


def test_load_repo_registers_processor(tmp_path):
    from ferrodac.analysis.processor import PROCESSOR_TYPES
    from ferrodac.extensions import ExtensionManager
    _write_proc_ext(tmp_path, "fresh-ext", "fresh_proc_kind", "fresh_ext_pkg")
    assert "fresh_proc_kind" not in PROCESSOR_TYPES
    mgr = ExtensionManager(str(tmp_path / "_root"))
    loaded = mgr.load_repo(str(tmp_path))
    assert len(loaded) == 1 and loaded[0].ok and loaded[0].name == "fresh-ext"
    assert "fresh_proc_kind" in PROCESSOR_TYPES           # the loader imported it → registered
    assert mgr.loaded[0].name == "fresh-ext"


def test_load_incompatible_api(tmp_path):
    from ferrodac.extensions import ExtensionError, ExtensionManager
    (tmp_path / "ferrodac-extension.toml").write_text('[extension]\nname="x"\napi=999\n')
    with pytest.raises(ExtensionError):
        ExtensionManager(str(tmp_path / "_r")).load_extension(str(tmp_path))


def test_install_records_and_load_enabled(tmp_path):
    from ferrodac.extensions import ExtensionManager
    root = str(tmp_path / "extroot")
    src = str(tmp_path / "src")
    os.makedirs(src)
    _write_proc_ext(tmp_path / "src", "persist-ext", "persist_kind", "persist_pkg")
    mgr = ExtensionManager(root)
    mgr.install(src)                                      # records + loads
    recs = mgr.records()
    assert recs and recs[0]["source"] == src and recs[0]["enabled"]
    # a disabled record is skipped; a fresh manager replays enabled ones on startup
    from ferrodac.analysis.processor import PROCESSOR_TYPES
    assert "persist_kind" in PROCESSOR_TYPES
    mgr.set_enabled(src, False)
    assert mgr.records()[0]["enabled"] is False
    ExtensionManager(root).load_enabled()                # no error even when disabled
    mgr.remove(src)
    assert mgr.records() == []


def test_load_enabled_survives_a_broken_source(tmp_path):
    """A missing/broken source is logged and skipped, never blocking launch."""
    from ferrodac.extensions import ExtensionManager
    root = str(tmp_path / "r")
    mgr = ExtensionManager(root)
    mgr.install(str(tmp_path / "does-not-exist"), enabled=True)   # records a bad source
    ExtensionManager(root).load_enabled()                # must not raise


def test_install_url_from_local_git(tmp_path):
    """install_url clones + pins a git repo (offline, from a local repo) and records
    the resolved commit, then loads it."""
    import subprocess
    from ferrodac.analysis.processor import PROCESSOR_TYPES
    from ferrodac.extensions import ExtensionManager
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_proc_ext(repo, "git-ext", "git_proc_kind", "git_ext_pkg")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    mgr = ExtensionManager(str(tmp_path / "root"))
    loaded, dest, sha = mgr.install_url(str(repo))
    assert len(sha) == 40 and loaded and loaded[0].ok
    assert "git_proc_kind" in PROCESSOR_TYPES
    rec = mgr.records()[0]
    assert rec["source"] == str(repo) and rec["commit"] == sha and rec["clone"] == dest


def test_source_and_whitepaper_lookup(tmp_path):
    """After loading, the manager can show a provider's source (read from file, no
    execution) and resolve its white paper — by the provider's kind."""
    from ferrodac.extensions import ExtensionManager
    mgr = ExtensionManager(str(tmp_path / "root"))
    mgr.load_repo(EX)                                    # in-tree example: window_integral
    assert "class WindowIntegral" in mgr.source_for("window_integral")
    wp = mgr.whitepaper_for("window_integral")
    assert wp and wp.endswith("integrate.md") and os.path.exists(wp)
    assert mgr.source_for("nope") == "" and mgr.whitepaper_for("nope") is None


@pytest.mark.ui
def test_extensions_dialog_providers(qapp, tmp_path):
    from qtpy.QtCore import Qt
    from ferrodac.extensions import ExtensionManager
    from ferrodac.ui.extensions_view import ExtensionsDialog
    mgr = ExtensionManager(str(tmp_path / "root"))
    mgr.install(EX, enabled=True)                        # records the in-tree example
    dlg = ExtensionsDialog(mgr, None)
    try:
        dlg._list.setCurrentRow(0)                       # selecting → providers populate
        assert dlg._providers.count() == 1
        assert "WindowIntegral" in dlg._providers.item(0).text()
        root_dir, entry, paper = dlg._providers.item(0).data(Qt.UserRole)
        assert entry.endswith(":WindowIntegral") and paper and os.path.exists(paper)
    finally:
        dlg.deleteLater()


@pytest.mark.ui
def test_extensions_dialog_lists_and_toggles(qapp, tmp_path):
    from ferrodac.extensions import ExtensionManager
    from ferrodac.ui.extensions_view import ExtensionsDialog
    mgr = ExtensionManager(str(tmp_path / "root"))
    mgr._record("https://example.test/repo", "/clones/repo", "abc123def456", None, True)
    dlg = ExtensionsDialog(mgr, None)
    try:
        assert dlg._list.count() == 1
        assert "example.test/repo" in dlg._list.item(0).text()
        assert "abc123de" in dlg._list.item(0).text()
        dlg._list.setCurrentRow(0)
        dlg._toggle()                                  # disable via the button handler
        assert mgr.records()[0]["enabled"] is False
    finally:
        dlg.deleteLater()


@pytest.mark.ui
def test_load_widget_extension(qapp, tmp_path):
    pkg = tmp_path / "wext_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "w.py").write_text(
        "from ferrodac.plugin import Widget, register_widget\n"
        "@register_widget('Ext Widget')\n"
        "class ExtW(Widget):\n    kind = 'ext_widget_kind'\n")
    (tmp_path / "ferrodac-extension.toml").write_text(
        '[extension]\nname = "wext"\napi = 1\n[[widgets]]\nentry = "wext_pkg.w:ExtW"\n')
    from ferrodac.extensions import ExtensionManager
    from ferrodac.ui.widget import WIDGET_TYPES
    try:
        loaded = ExtensionManager(str(tmp_path / "_r")).load_repo(str(tmp_path))
        assert loaded[0].ok and "ext_widget_kind" in WIDGET_TYPES   # in the Add-menu registry
        assert WIDGET_TYPES["ext_widget_kind"][0] == "Ext Widget"
    finally:
        WIDGET_TYPES.pop("ext_widget_kind", None)
