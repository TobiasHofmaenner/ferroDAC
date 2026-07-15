"""The /guidance playbook library + its two read-verbs.

GuidanceLibrary loads markdown-with-frontmatter playbooks from the built-in package
dir (+ a user dir that shadows by id); guidance.list/guidance.get expose them read-
scoped so any connector discovers them via /describe. Qt-free."""

import os
import tempfile
import types

import pytest

from ferrodac.core.control import ControlError
from ferrodac.guidance import GuidanceLibrary, parse_frontmatter

_BUILTINS = {"set-up-a-live-readout", "document-the-bench",
             "annotate-an-experiment", "plot-live-external-data", "add-a-device"}


def test_builtin_playbooks_load():
    lib = GuidanceLibrary(user_dir=tempfile.mkdtemp())        # isolate from a real user dir
    idx = lib.list()
    assert _BUILTINS <= {p["id"] for p in idx}
    for p in idx:
        assert p["source"] == "builtin" and p["title"] and "when_to_use" in p and "tags" in p


def test_get_returns_full_body():
    lib = GuidanceLibrary(user_dir=tempfile.mkdtemp())
    pb = lib.get("set-up-a-live-readout")
    assert pb is not None
    assert "layout.route" in pb["verbs_used"] and "## Steps" in pb["body"]
    assert lib.get("no-such-playbook") is None


def test_user_playbook_shadows_builtin():
    ud = tempfile.mkdtemp()
    with open(os.path.join(ud, "x.md"), "w", encoding="utf-8") as f:
        f.write("---\nid: set-up-a-live-readout\ntitle: Custom\ntags: [a]\n---\nmine\n")
    lib = GuidanceLibrary(user_dir=ud)
    pb = lib.get("set-up-a-live-readout")
    assert pb["title"] == "Custom" and pb["source"] == "user" and pb["body"].strip() == "mine"


def test_frontmatter_parses_scalars_and_inline_lists():
    meta, body = parse_frontmatter("---\nid: a\ntitle: Hi\ntags: [x, y, z]\n---\nhello\n")
    assert meta["id"] == "a" and meta["title"] == "Hi"
    assert meta["tags"] == ["x", "y", "z"] and body.strip() == "hello"
    # no frontmatter -> empty meta, text unchanged
    m2, b2 = parse_frontmatter("plain body")
    assert m2 == {} and b2 == "plain body"


def test_guidance_verbs_registered_and_dispatch():
    from ferrodac.ui.appcontrol import build_control_surface
    app = types.SimpleNamespace(
        _gui_bridge=types.SimpleNamespace(post_and_wait=lambda fn, **kw: fn()))
    s = build_control_surface(app)
    by = {v["name"]: v for v in s.describe("read")["verbs"]}
    assert by["guidance.list"]["kind"] == "query" and by["guidance.list"]["scope"] == "read"
    assert by["guidance.get"]["scope"] == "read"

    lst = s.dispatch("guidance.list", scope="read")
    assert any(p["id"] == "document-the-bench" for p in lst)
    pb = s.dispatch("guidance.get", {"id": "annotate-an-experiment"}, scope="read")
    assert "tag.add" in pb["verbs_used"] and pb["title"] == "Annotate an experiment"
    with pytest.raises(ControlError):
        s.dispatch("guidance.get", {"id": "nope"}, scope="read")
    with pytest.raises(ControlError):
        s.dispatch("guidance.get", {}, scope="read")           # missing required id
