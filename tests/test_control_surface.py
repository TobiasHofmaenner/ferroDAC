"""The control surface (ControlSurface registry) + connector auth (ConnectorRegistry).

The Qt-free foundation of the external-connector control plane: a self-describing
command/query/event registry with scope enforcement, and a pairing-with-approval auth
model issuing per-connector revocable bearer tokens (stored hashed, scoped, persisted).
"""

import os
import tempfile

import pytest

from ferrodac.core.control import ControlError, ControlSurface, ScopeError
from ferrodac.core.connectors import ConnectorRegistry, _hash_token


# -- ControlSurface ----------------------------------------------------------
def _surface():
    s = ControlSurface()
    log = []
    s.register("device.set", lambda p: log.append(("set", p)) or {"ok": True},
               scope="control", description="set a value",
               params={"value": {"type": "number", "required": True}})
    s.query("device.list", lambda p: ["a", "b"], description="list devices")
    s.register("project.delete", lambda p: {"deleted": p["id"]},
               scope="admin", destructive=True,
               params={"id": {"type": "string", "required": True}})
    return s, log


def test_dispatch_runs_handler_and_returns_result():
    s, log = _surface()
    assert s.dispatch("device.set", {"value": 5}, scope="control") == {"ok": True}
    assert log == [("set", {"value": 5})]
    assert s.dispatch("device.list", scope="read") == ["a", "b"]


def test_scope_is_enforced():
    s, _ = _surface()
    # a read-only caller can't invoke a control verb…
    with pytest.raises(ScopeError):
        s.dispatch("device.set", {"value": 1}, scope="read")
    # …but can run a query
    assert s.dispatch("device.list", scope="read") == ["a", "b"]


def test_destructive_needs_admin_and_confirm():
    s, _ = _surface()
    with pytest.raises(ScopeError):                     # control < admin
        s.dispatch("project.delete", {"id": "p1"}, scope="control", confirm=True)
    with pytest.raises(ScopeError):                     # admin but no confirm
        s.dispatch("project.delete", {"id": "p1"}, scope="admin")
    assert s.dispatch("project.delete", {"id": "p1"}, scope="admin",
                      confirm=True) == {"deleted": "p1"}


def test_missing_param_and_unknown_verb_and_handler_error():
    s, _ = _surface()
    with pytest.raises(ControlError):
        s.dispatch("device.set", {}, scope="control")   # missing required 'value'
    with pytest.raises(ControlError):
        s.dispatch("nope", scope="admin")
    s.register("boom", lambda p: 1 / 0, scope="control")
    with pytest.raises(ControlError):                   # handler exception → ControlError
        s.dispatch("boom", scope="control")


def test_describe_is_scope_filtered():
    s, _ = _surface()
    names = lambda scope: {v["name"] for v in s.describe(scope)["verbs"]}
    assert names("read") == {"device.list"}             # only what read may invoke
    assert names("control") == {"device.list", "device.set"}
    assert names("admin") == {"device.list", "device.set", "project.delete"}
    d = next(v for v in s.describe("admin")["verbs"] if v["name"] == "project.delete")
    assert d["destructive"] and d["scope"] == "admin"   # self-describing metadata


def test_events_fan_out_and_unsubscribe():
    s, _ = _surface()
    got = []
    unsub = s.subscribe(got.append)
    s.emit("device.added", id="dev/1")
    assert got == [{"event": "device.added", "id": "dev/1"}]
    unsub()
    s.emit("device.added", id="dev/2")
    assert len(got) == 1                                # no delivery after unsubscribe


def test_build_control_surface_registers_the_cheap_tier():
    """The app adapter exposes the cheap-tier verbs with the right scopes/kinds
    (built without a full MainWindow — it only needs _gui_bridge at build time)."""
    import types

    from ferrodac.ui.appcontrol import build_control_surface

    app = types.SimpleNamespace(
        _gui_bridge=types.SimpleNamespace(post_and_wait=lambda fn, **kw: fn()))
    s = build_control_surface(app)
    by = {v["name"]: v for v in s.describe("admin")["verbs"]}
    assert {"device.list", "device.add", "device.set_sink", "hub.connect",
            "hub.disconnect", "layout.add_panel", "time.park_window", "tag.add",
            "project.switch", "project.list", "time.window", "hub.status"} <= set(by)
    assert by["device.list"]["kind"] == "query" and by["device.list"]["scope"] == "read"
    assert by["device.set_sink"]["scope"] == "control"        # a control command
    assert by["hub.connect"]["params"]["addr"]["required"]    # self-describing params


# -- ConnectorRegistry (pairing + auth) --------------------------------------
def _registry(tmp):
    return ConnectorRegistry(path=os.path.join(tmp, "connectors.json"))


def test_pairing_approve_issues_a_token_that_authenticates():
    with tempfile.TemporaryDirectory() as tmp:
        reg = _registry(tmp)
        notified = []
        reg.set_pairing_notifier(notified.append)
        p = reg.request_pairing("my-assistant", scope="control")
        assert notified and notified[0].id == p.id and len(p.code) == 6
        assert reg.poll_pairing(p.id).status == "pending"

        tok = reg.approve(p.id)                          # user approves
        assert tok and tok.startswith("fdc_")
        # the client polls once and gets the token (one-shot); then it's gone
        snap = reg.poll_pairing(p.id)
        assert snap.status == "approved" and snap.token == tok
        assert reg.poll_pairing(p.id) is None

        c = reg.authenticate(tok)                        # the token now authenticates
        assert c is not None and c.name == "my-assistant" and c.scope == "control"
        assert c.last_seen > 0
        assert reg.authenticate("fdc_wrong") is None


def test_approve_can_downgrade_scope_and_token_is_stored_hashed():
    with tempfile.TemporaryDirectory() as tmp:
        reg = _registry(tmp)
        p = reg.request_pairing("a", scope="admin")
        tok = reg.approve(p.id, scope="read")            # user downgrades admin→read
        assert reg.authenticate(tok).scope == "read"
        # the raw token is NEVER on disk — only its hash
        with open(os.path.join(tmp, "connectors.json")) as fh:
            blob = fh.read()
        assert tok not in blob and _hash_token(tok) in blob


def test_deny_and_revoke():
    with tempfile.TemporaryDirectory() as tmp:
        reg = _registry(tmp)
        p = reg.request_pairing("a")
        reg.deny(p.id)
        assert reg.poll_pairing(p.id).status == "denied"
        assert reg.approve(p.id) is None                 # can't approve an answered pairing

        tok = reg.approve(reg.request_pairing("b").id)
        cid = reg.list()[0]["id"]
        assert reg.authenticate(tok) is not None
        assert reg.revoke(cid) is True
        assert reg.authenticate(tok) is None             # revoked → no auth
        assert reg.list() == []                          # dropped from the visible list


def test_connectors_persist_across_restart():
    with tempfile.TemporaryDirectory() as tmp:
        reg = _registry(tmp)
        tok = reg.approve(reg.request_pairing("keeps").id)
        reg2 = _registry(tmp)                            # "restart"
        assert reg2.authenticate(tok).name == "keeps"
