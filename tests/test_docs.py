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
