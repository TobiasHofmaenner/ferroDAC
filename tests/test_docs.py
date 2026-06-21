"""In-app document view (ferrodac.ui.docs) — the QtWebEngine markdown/LaTeX renderer.

Spins a real QWebEngine, so it's importorskip-guarded (skips where WebEngine isn't
installed) and marked `ui` (the lightweight CI gate skips Qt).
"""

import os
import tempfile
import threading
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
        fh.write("# hello collab\n")              # A's local content seeds the shared doc
    with open(pb_, "w", encoding="utf-8") as fh:
        fh.write("# other\n")
    a = DocView()
    a.resize(640, 420)
    b = DocView()
    b.resize(640, 420)
    try:
        a.open(pa)
        b.open(pb_)
        _wait_html(qapp, a.view, "hello collab")  # A's bundle loaded + bridge wired
        _wait_html(qapp, b.view, "other")         # B's too

        updates = []
        a.bridge.updateRequested.connect(lambda u, c: updates.append(u))
        # A seeds from its LOCAL content (server text empty — first collaboration)
        a.bridge.collabSeed.emit(True, "", "alice")
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
def test_collab_edits_rerender_preview(qapp):
    """In a collab session BOTH the typist's and the receiver's rendered preview
    update on EVERY edit — not just the first. Render is driven by the editor's
    updateListener (fires after y-codemirror applies local AND remote changes), so a
    stale double-render can't leave the preview behind."""
    from ferrodac.ui.docs import DocView
    d = tempfile.mkdtemp()
    pa = os.path.join(d, "A.md")
    pb_ = os.path.join(d, "B.md")
    with open(pa, "w", encoding="utf-8") as fh:
        fh.write("# doc\n")
    with open(pb_, "w", encoding="utf-8") as fh:
        fh.write("# old\n")
    a = DocView()
    a.resize(640, 420)
    b = DocView()
    b.resize(640, 420)
    try:
        a.open(pa)
        b.open(pb_)
        _wait_html(qapp, a.view, "doc")
        _wait_html(qapp, b.view, "old")
        ups = []
        a.bridge.updateRequested.connect(lambda u, c: ups.append(u))
        a.bridge.collabSeed.emit(True, "", "alice")
        assert _pump(qapp, lambda: bool(ups))
        b.bridge.collabSeed.emit(False, "", "bob")
        _pump(qapp, lambda: True, 0.3)
        for u in list(ups):
            b.bridge.collabUpdate.emit(u)
        _wait_html(qapp, b.view, "doc")

        # a SECOND live edit — the case the convergence test never exercised
        n = len(ups)
        a.view.page().runJavaScript("window.__doc.insert(' MORE')")
        assert _pump(qapp, lambda: len(ups) > n), "2nd edit emitted no update"
        assert "MORE" in _wait_html(qapp, a.view, "MORE"), "typist's preview stale"
        for u in ups[n:]:
            b.bridge.collabUpdate.emit(u)
        assert "MORE" in _wait_html(qapp, b.view, "MORE"), "receiver's preview stale"
    finally:
        a.deleteLater()
        b.deleteLater()


@pytest.mark.ui
def test_collab_local_fanout_and_seed(qapp):
    """Two local views sharing one controller (docked + popped-out window of the same
    doc): the hub treats the app as ONE member, so the controller must (a) seed a 2nd
    local view from an existing peer and (b) fan a local edit to the other view itself
    (the hub never echoes our own edits back). This is the pop-out-while-collaborating
    fix — without it the second view stays stale."""
    from ferrodac.ui.hubclient import HubController
    from ferrodac.ui.docs import DocBridge
    hc = HubController(None, None, None)
    hc._aid = hc._collab_actor = "me"
    b1, b2 = DocBridge(), DocBridge()
    seeds2, req1, up1, up2 = [], [], [], []
    b2.collabSeed.connect(lambda s, t, a: seeds2.append((s, t, a)))
    b1.collabRequestState.connect(lambda: req1.append(1))
    b1.collabUpdate.connect(lambda u: up1.append(u))
    b2.collabUpdate.connect(lambda u: up2.append(u))
    try:
        hc.doc_open("D", b1)             # first local view
        hc.doc_open("D", b2)             # 2nd local view → seeded from b1
        qapp.processEvents()
        assert seeds2 and seeds2[0][0] is False, "2nd view not told to enter empty"
        assert req1, "existing peer not asked to dump its state to seed the newcomer"

        hc.doc_send_update("D", "UPD", sender=b2)   # b2 edits
        qapp.processEvents()
        assert up1 == ["UPD"], "local edit did not fan out to the peer view"
        assert up2 == [], "local edit echoed back to its own sender"

        hc.doc_close("D", b2)            # closing the window leaves b1 in the room
        assert "D" in hc._doc_bridges and b1 in hc._doc_bridges["D"]
        hc.doc_close("D", b1)            # last view → room gone
        assert "D" not in hc._doc_bridges
    finally:
        b1.deleteLater()
        b2.deleteLater()


def _run_hub(projects_dir, out, ready):
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    from hub.core import Hub
    from hub.main import build_server
    server, _ = build_server(hub=Hub(projects_dir=projects_dir))
    port = server.add_insecure_port("127.0.0.1:0")
    loop.run_until_complete(server.start())
    out["addr"] = f"127.0.0.1:{port}"
    out["loop"] = loop
    ready.set()
    loop.run_forever()
    loop.run_until_complete(server.stop(0))
    loop.close()


def _mk_doc_controller(addr, actor):
    """A real HubController exercising ONLY its doc path (no dashboard/engine), with
    its HubDocSync wired exactly as connect() does — so this is the real relay +
    QueuedConnection routing, not a stub."""
    from ferrodac.ui.hubclient import HubController
    from ferrodac.net.docs import HubDocSync
    hc = HubController(None, None, None)
    hc._aid = hc._collab_actor = actor
    hc._docsync = HubDocSync(
        addr, agent_id=actor,
        on_seed=lambda d, s, t: hc._doc_seed.emit(d, s, t),
        on_update=lambda d, u: hc._doc_update.emit(d, u),
        on_awareness=lambda d, a: hc._doc_awareness.emit(d, a),
        on_presence=lambda d, a: hc._doc_presence.emit(d, a))
    hc._docsync.start()
    return hc


@pytest.mark.ui
@pytest.mark.skipif(
    bool(os.environ.get("CI")),
    reason="heavy full-stack co-edit (2 HubControllers + a hub thread + 2 QtWebEngine "
           "views) has fragile process-exit teardown on shared CI runners (segfaults). "
           "The pieces are covered by lighter tests + the hub-room/relay e2e; this runs "
           "locally for full-stack confidence.")
def test_collab_end_to_end_through_hub(qapp):
    """The WHOLE stack: two DocViews, two real HubControllers, one hub. A toggles
    Collaborate on a cold doc → seeds from its local text → the hub fans it to B,
    whose editor converges. Exercises DocView toggle → controller → HubDocSync → hub
    → HubDocSync → controller (QueuedConnection) → bridge → JS, both directions."""
    pytest.importorskip("grpc")
    pytest.importorskip("ferrodac_contract.v1.data_plane_pb2")
    from ferrodac.ui.docs import DocView

    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "proj1", "docs"), exist_ok=True)
    pa = os.path.join(tmp, "A.md")
    pb_ = os.path.join(tmp, "B.md")
    with open(pa, "w", encoding="utf-8") as fh:
        fh.write("# live doc\n")
    with open(pb_, "w", encoding="utf-8") as fh:
        fh.write("# stale\n")
    doc_id = "proj1::README.md"

    out, ready = {}, threading.Event()
    ht = threading.Thread(target=_run_hub, args=(tmp, out, ready), daemon=True)
    ht.start()
    assert ready.wait(5), "hub did not start"
    addr = out["addr"]

    hca = _mk_doc_controller(addr, "alice")
    hcb = _mk_doc_controller(addr, "bob")
    a = DocView()
    a.resize(640, 420)
    b = DocView()
    b.resize(640, 420)
    try:
        a.open(pa)
        b.open(pb_)
        _wait_html(qapp, a.view, "live doc")
        _wait_html(qapp, b.view, "stale")

        a_seeds = []
        a.bridge.collabSeed.connect(lambda s, t, act: a_seeds.append(s))
        a_updates = []
        a.bridge.updateRequested.connect(lambda u, c: a_updates.append(u))

        # A goes live first → cold room → it seeds from its local "# live doc"
        a.set_collab_target(hca, doc_id)
        a._start_collab()
        assert _pump(qapp, lambda: a_seeds and a_seeds[0] is True), "A wasn't asked to seed"
        assert _pump(qapp, lambda: bool(a_updates)), "A emitted no baseline to the hub"

        # B joins → the hub replays the baseline → B converges through the full stack
        b.set_collab_target(hcb, doc_id)
        b._start_collab()
        html = _wait_html(qapp, b.view, "live doc", timeout=20)
        assert "live doc" in html, "B did not converge through the hub"
        assert html.count("live doc") == 1, "content duplicated"
        assert "stale" not in html, "B's original text survived the merge"
    finally:
        a._stop_collab()
        b._stop_collab()
        for _ in range(10):
            qapp.processEvents()
            time.sleep(0.02)
        hca._docsync.stop()
        hcb._docsync.stop()
        a.deleteLater()
        b.deleteLater()
        if out.get("loop") is not None:
            out["loop"].call_soon_threadsafe(out["loop"].stop)
        ht.join(timeout=5)


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
