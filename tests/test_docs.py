"""In-app document view (ferrodac.ui.docs) — the QtWebEngine markdown/LaTeX renderer.

Spins a real QWebEngine, so it's importorskip-guarded (skips where WebEngine isn't
installed) and marked `ui` (the lightweight CI gate skips Qt).
"""

import os
import tempfile
import time

import pytest

pytest.importorskip("qtpy")
pytest.importorskip("qtpy.QtWebEngineWidgets")


def _wait_html(qapp, webview, needle, timeout=30.0):
    """Pump the event loop until the rendered #doc HTML contains `needle`."""
    out = {"html": ""}
    end = time.time() + timeout
    while time.time() < end:
        webview.page().runJavaScript(
            "var d=document.getElementById('doc'); d?d.innerHTML:''",
            lambda h: out.__setitem__("html", h or ""))
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.02)
        if needle in out["html"]:
            return out["html"]
    return out["html"]


@pytest.mark.ui
def test_docview_centers_display_math(qapp):
    """A standalone `$$ … $$` line renders as CENTERED display math (katex-display),
    matching Obsidian/GitHub/MathJax — not left-aligned inline."""
    from ferrodac.ui.docs import DocView
    d = tempfile.mkdtemp()
    p = os.path.join(d, "README.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("intro\n\n$$\\frac{\\sigma}{\\pi}$$\n")
    dv = DocView()
    dv.resize(560, 360)
    try:
        dv.open(p)
        html = _wait_html(qapp, dv.view, "katex-display")
        assert "katex-display" in html, "$$…$$ did not render as centered display math"
    finally:
        dv.deleteLater()


@pytest.mark.ui
def test_docview_edit_mode_mounts_and_saves(qapp):
    """Switching to Edit mounts the CodeMirror editor; the editor's autosave path
    writes the .md (file stays truth)."""
    from ferrodac.ui.docs import DocView
    d = tempfile.mkdtemp()
    p = os.path.join(d, "README.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# original\n")
    dv = DocView()
    dv.resize(720, 420)
    try:
        dv.open(p)
        _wait_html(qapp, dv.view, "original")          # rendered in Read mode
        dv.view.page().runJavaScript(
            "document.querySelector('#toolbar [data-mode=edit]').click()")
        got = {"v": ""}
        end = time.time() + 15
        while time.time() < end:
            dv.view.page().runJavaScript(
                "document.querySelector('.cm-editor') ? 'yes' : 'no'",
                lambda r: got.__setitem__("v", r))
            for _ in range(10):
                qapp.processEvents()
                time.sleep(0.02)
            if got["v"] == "yes":
                break
        assert got["v"] == "yes", "CodeMirror did not mount in Edit mode"
        # the editor autosaves via bridge.save → the file is written
        dv.bridge.save("# edited in app\n\n$$x^2$$\n")
        for _ in range(10):
            qapp.processEvents()
            time.sleep(0.02)
        with open(p, encoding="utf-8") as fh:
            assert fh.read() == "# edited in app\n\n$$x^2$$\n"
    finally:
        dv.deleteLater()


@pytest.mark.ui
def test_doc_panel_registered_opens_and_persists(qapp):
    """The Document panel is in the Add-menu registry, carries no data route, opens
    a file, and round-trips its path through save/restore state."""
    from ferrodac.ui.panels import PANEL_TYPES, DocPanel
    assert PANEL_TYPES.get("doc", (None, None))[1] is DocPanel
    assert DocPanel.routable is False            # no patch-bay port
    d = tempfile.mkdtemp()
    p = os.path.join(d, "notes.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# notes\n")
    panel = DocPanel()
    panel.resize(600, 400)
    other = DocPanel()
    try:
        panel.open(p)
        assert panel.state() == {"path": p}
        assert panel.state() != other.state()    # the other is still empty
        other.set_state({"path": p})             # restore from a saved layout
        assert other.state() == {"path": p}
    finally:
        panel.deleteLater()
        other.deleteLater()


def _pump(qapp, pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        qapp.processEvents()
        time.sleep(0.02)
    return False


@pytest.mark.ui
def test_collab_two_views_converge_via_yjs(qapp):
    """Two DocViews exchanging one opaque Yjs update converge to identical text
    with NO duplication — the real bundle's Yjs, end-to-end. The seeder builds the
    doc from its text and emits the seed update; the other starts empty and applies
    it (the single-seeding rule that avoids CRDT duplication)."""
    from ferrodac.ui.docs import DocView
    d = tempfile.mkdtemp()
    pa = os.path.join(d, "A.md")
    pb_ = os.path.join(d, "B.md")
    with open(pa, "w", encoding="utf-8") as fh:
        fh.write("# seed\n")
    with open(pb_, "w", encoding="utf-8") as fh:
        fh.write("# other\n")
    a = DocView()
    a.resize(640, 420)
    b = DocView()
    b.resize(640, 420)
    try:
        a.open(pa)
        b.open(pb_)
        _wait_html(qapp, a.view, "seed")          # A's bundle loaded + bridge wired
        _wait_html(qapp, b.view, "other")         # B's too

        updates = []
        a.bridge.updateRequested.connect(lambda u, c: updates.append(u))
        # A seeds the shared doc from this (collaborative) text and emits the update
        a.bridge.collabSeed.emit(True, "# hello collab\n", "alice")
        assert _pump(qapp, lambda: bool(updates)), "seeder emitted no Yjs update"
        seed_update = updates[0]

        # B joins empty, then applies A's update → converges
        b.bridge.collabSeed.emit(False, "", "bob")
        assert _pump(qapp, lambda: True, timeout=0.4)   # let B enter collab
        b.bridge.collabUpdate.emit(seed_update)
        html = _wait_html(qapp, b.view, "hello collab")
        assert "hello collab" in html, "B did not converge to A's text"
        assert html.count("hello collab") == 1, "content duplicated (seeding bug)"
        assert "other" not in html, "B's original text survived the merge"
    finally:
        a.deleteLater()
        b.deleteLater()


@pytest.mark.ui
def test_docview_renders_and_reloads(qapp):
    from ferrodac.ui.docs import DocView
    d = tempfile.mkdtemp()
    p = os.path.join(d, "README.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# Title\n\nInline $x^2$ and:\n\n```python\nx = 1\n```\n")
    dv = DocView()
    dv.resize(640, 420)
    try:
        dv.open(p)
        html = _wait_html(qapp, dv.view, "katex")
        assert "Title" in html, "markdown heading not rendered"
        assert "katex" in html, "LaTeX not rendered (KaTeX)"
        assert "hljs" in html, "code block not highlighted"
        # the file is truth + live-watched → an external edit re-renders
        assert p in dv._watcher.files()         # live-watch is wired
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("# Changed\n\n$y^3$\n")
        dv._reload()                            # what QFileSystemWatcher.fileChanged calls
        html2 = _wait_html(qapp, dv.view, "Changed")
        assert "Changed" in html2 and "Title" not in html2
    finally:
        dv.deleteLater()
