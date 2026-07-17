"""The device→app→device request/response channel (core.interaction).

Covers the whole channel end-to-end, mirroring the emit_tag suite:
  * the Qt-free Prompt entity round-trips through JSON (it crosses the wire);
  * the PendingInteractions store's add/resolve invokes the driver callback + records
    WHO answered, is first-responder-wins, and applies the timeout policy (a CRITICAL
    prompt never silently proceeds);
  * the Requests inbox renders offscreen and an inline answer resolves a prompt;
  * the device.prompts / device.respond control-surface verbs dispatch against a real
    MainWindow, and answer a prompt over a REAL localapi HTTP round-trip.
"""

import os
import tempfile

import pytest

from ferrodac.core.interaction import (
    ABORT, ACKNOWLEDGE, CHOICE, CONFIRM, STAY, TEXT, Prompt,
    prompt_from_dict, prompt_to_dict)


# -- the Qt-free entity ------------------------------------------------------
def test_prompt_defaults_and_json_roundtrip():
    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM, severity="critical",
               timeout=30, on_timeout=ABORT)
    assert p.id and p.created > 0                       # auto-minted id + created
    assert p.is_critical
    d = prompt_to_dict(p)
    import json
    json.dumps(d)                                       # strictly JSON-able (crosses the wire)
    back = prompt_from_dict(d)
    assert back.id == p.id and back.device_id == "dev-1"
    assert back.kind == CONFIRM and back.severity == "critical"
    # the timeout policy fields must survive the wire too (the device declares them)
    assert back.timeout == 30 and back.on_timeout == ABORT and back.created == p.created

    choice = Prompt("d", "Which detector?", kind=CHOICE, options=["FC", "SEM"])
    assert prompt_from_dict(prompt_to_dict(choice)).options == ["FC", "SEM"]
    assert prompt_from_dict({}) is None                 # junk in → None out


# -- the PendingInteractions store -------------------------------------------
def test_store_resolve_invokes_callback_and_records_responder(qapp):
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    seen = []
    resolved = []
    store.resolved.connect(resolved.append)

    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM)
    store.add(p, seen.append)
    assert [pr.id for pr in store.pending()] == [p.id]
    assert store.count() == 1
    assert store.has_pending("dev-1") and not store.has_pending("dev-2")

    ok = store.resolve(p.id, True, by="operator")
    assert ok is True
    assert seen == [True]                               # the driver callback fired with the answer
    assert store.count() == 0 and store.get(p.id) is None
    rec = resolved[0]                                   # the resolved record carries the bookkeeping
    assert rec.answer is True and rec.answered_by == "operator"
    assert rec.answered_at is not None


def test_store_is_first_responder_wins(qapp):
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    calls = []
    p = Prompt("dev-1", "OK?", kind=CONFIRM)
    store.add(p, calls.append)
    assert store.resolve(p.id, True, by="operator") is True
    assert store.resolve(p.id, False, by="connector") is False   # already answered → no-op
    assert calls == [True]                              # the callback ran exactly once


def test_store_withdraw_drops_without_invoking_callback(qapp):
    """withdraw() retires a device's open prompts WITHOUT answering them — no callback into
    a dead driver, no provenance tag (unlike resolve). Used when a device is removed."""
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    called, resolved = [], []
    store.resolved.connect(resolved.append)
    a = Prompt("dev-1", "?", kind=CONFIRM)
    b = Prompt("dev-2", "?", kind=CONFIRM)
    store.add(a, called.append)
    store.add(b, called.append)

    n = store.withdraw("dev-1")                          # only dev-1's prompt goes
    assert n == 1
    assert store.get(a.id) is None and store.get(b.id) is not None
    assert called == []                                 # NOT answered — no callback fired
    assert resolved == []                               # …and no provenance-tag signal
    assert store.withdraw("dev-1") == 0                 # idempotent — nothing left to withdraw


def test_store_withdraw_ids_drops_a_specific_prompt(qapp):
    """withdraw_ids() retires prompts BY id without answering — the device resolved that exact
    request on its own panel (?DONE), so it leaves the inbox without a callback."""
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    called, resolved = [], []
    store.resolved.connect(resolved.append)
    a = Prompt("dev-1", "?", kind=CONFIRM)
    b = Prompt("dev-1", "?", kind=CONFIRM)
    store.add(a, called.append)
    store.add(b, called.append)

    assert store.withdraw_ids(a.id) == 1                 # only that prompt id goes
    assert store.get(a.id) is None and store.get(b.id) is not None
    assert called == [] and resolved == []              # no callback, no provenance tag
    assert store.withdraw_ids(a.id) == 0                # idempotent


def test_store_resolve_records_callback_failure(qapp):
    """A driver on_response that throws must not break the store, and the resolved record
    must report the failure (ok=False) so the audit tag stays honest."""
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    resolved = []
    store.resolved.connect(resolved.append)

    def _boom(_answer):
        raise RuntimeError("ack write failed")
    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM)
    store.add(p, _boom)
    assert store.resolve(p.id, True) is True            # resolve still succeeds…
    assert store.count() == 0                            # …the prompt still closes…
    assert resolved[-1].ok is False                      # …but the record marks the failed callback
    assert resolved[-1].answer is True


def test_store_flood_guard_caps_open_prompts_per_device(qapp):
    from ferrodac.ui.interactions import PendingInteractions, _MAX_OPEN_PER_DEVICE

    store = PendingInteractions()
    for _ in range(_MAX_OPEN_PER_DEVICE + 5):
        store.add(Prompt("spammer", "?", kind=CONFIRM), lambda a: None)
    assert store.count() == _MAX_OPEN_PER_DEVICE        # excess prompts are dropped, not filed
    store.add(Prompt("other", "?", kind=CONFIRM), lambda a: None)
    assert store.count() == _MAX_OPEN_PER_DEVICE + 1    # a DIFFERENT device is unaffected


def test_store_timeout_policy(qapp):
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()

    # STAY: stays pending, never resolves
    p_stay = Prompt("d", "?", timeout=10, on_timeout=STAY)
    store.add(p_stay, lambda a: None)
    store._timed_out(p_stay.id)
    assert store.get(p_stay.id) is not None

    # ABORT: resolves with a None answer, tagged by="timeout:abort"
    aborted = []
    store.resolved.connect(aborted.append)
    p_abort = Prompt("d", "?", timeout=10, on_timeout=ABORT)
    store.add(p_abort, lambda a: None)
    store._timed_out(p_abort.id)
    assert store.get(p_abort.id) is None
    assert aborted[-1].answer is None and aborted[-1].answered_by == "timeout:abort"

    # a literal DEFAULT answer resolves with that value
    got = []
    p_def = Prompt("d", "?", kind=CONFIRM, timeout=10, on_timeout=False)
    store.add(p_def, got.append)
    store._timed_out(p_def.id)
    assert got == [False]

    # CRITICAL: a default answer is REFUSED — it never silently proceeds (stays pending)
    p_crit = Prompt("d", "?", kind=CONFIRM, severity="critical", timeout=10, on_timeout=True)
    store.add(p_crit, lambda a: None)
    store._timed_out(p_crit.id)
    assert store.get(p_crit.id) is not None             # still open — critical never auto-answers

    # CRITICAL + ABORT: the ONE auto-resolve a critical prompt DOES honour (it aborts,
    # it just never silently proceeds with a default answer)
    crit_aborts = []
    store.resolved.connect(crit_aborts.append)
    p_ca = Prompt("d", "?", kind=CONFIRM, severity="critical", timeout=10, on_timeout=ABORT)
    store.add(p_ca, lambda a: None)
    store._timed_out(p_ca.id)
    assert store.get(p_ca.id) is None                   # aborted…
    assert crit_aborts[-1].answered_by == "timeout:abort"   # …explicitly, not silently


# -- the Requests inbox (offscreen) ------------------------------------------
def test_requests_inbox_renders_and_inline_answer_resolves(qapp):
    from qtpy.QtWidgets import QPushButton
    from ferrodac.ui.interactions import PendingInteractions, RequestsPanel

    store = PendingInteractions()
    panel = RequestsPanel(store, device_name=lambda did: "Sim Arm")
    try:
        answered = []
        p = Prompt("dev-1", "Have you retracted the arm?", kind=CONFIRM)
        store.add(p, answered.append)                   # the panel rebuilds on store.changed

        assert "(1)" in panel._label.text()             # the badge count
        yes = [b for b in panel.findChildren(QPushButton) if b.text() == "Yes"]
        assert yes, "the confirm prompt did not auto-render a [Yes] button"
        yes[0].click()                                  # answer inline

        assert answered == [True]                       # the driver callback fired…
        assert store.count() == 0                        # …and the prompt left the inbox
        assert "Requests" == panel._label.text().split("  ")[0]
    finally:
        panel.deleteLater()


def test_answer_controls_auto_generate_from_kind(qapp):
    from qtpy.QtWidgets import QLineEdit, QPushButton
    from ferrodac.ui.interactions import build_answer_controls

    got = []
    # confirm → [Yes]/[No] answering True / False
    ctrls = build_answer_controls(Prompt("d", "?", kind=CONFIRM), got.append)
    no = next(w for w in ctrls if isinstance(w, QPushButton) and w.text() == "No")
    no.click()
    assert got == [False]                          # the False branch, not just Yes

    # choice → one button per option, each answering with that option
    got.clear()
    ctrls = build_answer_controls(Prompt("d", "?", kind=CHOICE, options=["A", "B"]),
                                  got.append)
    labels = [w.text() for w in ctrls if isinstance(w, QPushButton)]
    assert labels == ["A", "B"]
    ctrls[1].click()
    assert got == ["B"]

    # text → a field + submit answering with the typed string
    got.clear()
    ctrls = build_answer_controls(Prompt("d", "?", kind=TEXT), got.append)
    edit = next(w for w in ctrls if isinstance(w, QLineEdit))
    submit = next(w for w in ctrls if isinstance(w, QPushButton))
    submit.click()
    assert got == []                               # an EMPTY submit does not answer with ""
    edit.setText("42")
    submit.click()
    assert got == ["42"]

    # acknowledge → a single OK answering True
    got.clear()
    ctrls = build_answer_controls(Prompt("d", "?", kind=ACKNOWLEDGE), got.append)
    ctrls[0].click()
    assert got == [True]


# -- the control-surface verbs (dispatched against a real MainWindow) --------
@pytest.mark.ui
def test_device_prompt_verbs_dispatch_against_the_app(control_surface):
    import json
    from ferrodac.core.control import ControlError

    w, s = control_surface
    answered = []
    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM, severity="critical")
    w.interactions.add(p, answered.append)

    listing = s.dispatch("device.prompts", scope="read")
    json.dumps(listing)                                 # nothing non-JSON-able leaks
    assert [pr["id"] for pr in listing] == [p.id]
    assert listing[0]["kind"] == "confirm" and listing[0]["severity"] == "critical"

    # a wrong-TYPED answer to a confirm must be rejected, not passed to the driver (it would
    # read truthy and could drive hardware) — the request stays open
    with pytest.raises(ControlError):
        s.dispatch("device.respond", {"id": p.id, "answer": "no"}, scope="control")
    assert answered == [] and s.dispatch("device.prompts", scope="read") != []

    # caller is recorded on the provenance tag (answered by connector:<name>)
    res = s.dispatch("device.respond", {"id": p.id, "answer": True},
                     scope="control", caller="assistant")
    assert res == {"ok": True, "id": p.id, "answer": True}
    assert answered == [True]                            # the device callback fired
    assert s.dispatch("device.prompts", scope="read") == []   # closed

    # answering a gone/unknown request is an explicit error, not a silent no-op
    with pytest.raises(ControlError):
        s.dispatch("device.respond", {"id": p.id, "answer": True}, scope="control")

    # a provenance tag recorded the outcome (origin=device, immutable, WHO answered)
    tags = [m for m in w.dashboard.markers.all() if m.kind == "interaction"]
    assert tags and tags[-1].origin_kind == "device"
    assert tags[-1].payload.get("answer") is True
    assert tags[-1].payload.get("answered_by") == "connector:assistant"

    # an out-of-options choice answer is likewise rejected
    c = Prompt("dev-1", "Which detector?", kind=CHOICE, options=["FC", "SEM"])
    w.interactions.add(c, lambda a: None)
    with pytest.raises(ControlError):
        s.dispatch("device.respond", {"id": c.id, "answer": "TOF"}, scope="control")
    assert s.dispatch("device.respond", {"id": c.id, "answer": "FC"},
                      scope="control")["answer"] == "FC"


@pytest.mark.ui
def test_device_removal_withdraws_open_prompts(control_surface):
    """Removing a device retires its open requests through the app wiring
    (manager.device_removed → interactions.withdraw): the prompt leaves the inbox and its
    callback is NOT fired into the now-dead driver."""
    w, _s = control_surface
    called = []
    p = Prompt("gauge-uuid", "Retract the arm?", kind=CONFIRM, severity="critical")
    w.interactions.add(p, called.append)
    assert w.interactions.count() == 1

    w.manager.device_removed.emit(("gauge-uuid", "gauge-1"))   # what manager.remove() emits
    assert w.interactions.get(p.id) is None             # withdrawn from the inbox…
    assert called == []                                 # …without answering the dead driver


# -- a REAL localapi HTTP round-trip -----------------------------------------
# This test targets the TRANSPORT (auth + wire + the /command|/query envelope) with a
# minimal surface. The SHIPPING device.respond verb's own logic (per-kind coercion,
# first-responder-wins, and the connector-name provenance tag) is exercised on a real
# MainWindow in test_device_prompt_verbs_dispatch_against_the_app above.
def test_device_respond_over_localapi_roundtrip(qapp):
    httpx = pytest.importorskip("httpx")
    pytest.importorskip("starlette")
    pytest.importorskip("uvicorn")

    from ferrodac.core.control import ControlError, ControlSurface
    from ferrodac.core.connectors import ConnectorRegistry
    from ferrodac.net.localapi import LocalApiServer
    from ferrodac.ui.interactions import PendingInteractions

    store = PendingInteractions()
    answered = []
    surface = ControlSurface()
    surface.query("device.prompts", lambda _p: store.to_list())

    def _respond(p):
        pid = str(p.get("id") or "")
        if store.get(pid) is None:
            raise ControlError("no open request")
        store.resolve(pid, p.get("answer"), by="connector")
        return {"ok": True, "id": pid, "answer": p.get("answer")}
    surface.register("device.respond", _respond, scope="control",
                     params={"id": {"type": "string", "required": True},
                             "answer": {"type": "any"}})

    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM)
    store.add(p, answered.append)

    tmp = tempfile.mkdtemp()
    reg = ConnectorRegistry(path=os.path.join(tmp, "connectors.json"))
    srv = LocalApiServer(surface, reg, port=0, version="test", config_dir=tmp)
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        with httpx.Client(base_url=base, timeout=5.0) as c:
            pid = c.post("/pair", json={"name": "assistant", "scope": "control"}) \
                .json()["pairing_id"]
            reg.approve(pid, scope="control")
            tok = c.get(f"/pair/{pid}").json()["token"]
            h = {"Authorization": f"Bearer {tok}"}
            # the agent SEES the open request…
            listed = c.get("/query/device.prompts", headers=h).json()["result"]
            assert [pr["id"] for pr in listed] == [p.id]
            # …and answers it over the wire
            r = c.post("/command/device.respond",
                       json={"id": p.id, "answer": True}, headers=h)
            assert r.status_code == 200 and r.json()["result"]["answer"] is True
            assert answered == [True]                    # the device callback fired
            assert c.get("/query/device.prompts", headers=h).json()["result"] == []
    finally:
        srv.stop()


@pytest.mark.ui
def test_hub_prompt_relay_app_wiring(control_surface):
    """The app's hub-relay handlers (§7.3): a VIEWER injects a remote prompt + relays the
    answer with NO local provenance tag (the owner records it); an AGENT resolves a prompt a
    viewer answered (driver callback + provenance tag + close broadcast); raising a device
    prompt publishes it to the hub."""
    import types

    w, _s = control_surface

    class _FakeHub:
        actor = "viewer-a"

        def __init__(self):
            self.published, self.closed, self.responded = [], [], []

        def publish_prompt(self, p):
            self.published.append(p)

        def close_prompt(self, pid, answer_text="", by=""):
            self.closed.append((pid, answer_text, by))

        def respond_remote_prompt(self, pid, answer, by=""):
            self.responded.append((pid, answer, by))

        def disconnect(self):
            pass                                    # MainWindow.closeEvent calls this

    w.hub = _FakeHub()

    def _interaction_tags():
        return [m for m in w.dashboard.markers.all() if m.kind == "interaction"]

    # AGENT: raising a device prompt mirrors it to the hub
    p = Prompt("dev-x", "Retract the arm?", kind=CONFIRM)
    w._on_device_prompt(p, lambda a: None)
    assert w.hub.published == [p]

    # VIEWER: a remote prompt injects into the inbox; answering relays it and does NOT
    # drop a local provenance tag (the owning agent owns that record).
    wire = types.SimpleNamespace(
        id="rp-1", device_uuid="lsc-uuid", question="Vent?", kind="confirm", title="",
        options=[], severity="warn", timeout=0.0, on_timeout="stay", created=0.0)
    n_before = len(_interaction_tags())
    w._on_remote_prompt_opened(wire)
    assert w.interactions.get("rp-1") is not None
    assert w.interactions.resolve("rp-1", True, by="operator") is True
    assert w.hub.responded == [("rp-1", True, "viewer-a")]        # answer relayed to the owner
    assert len(_interaction_tags()) == n_before                   # NO local provenance tag

    # a remote CLOSE (owner resolved it) withdraws it from the viewer inbox
    w._on_remote_prompt_opened(types.SimpleNamespace(
        id="rp-2", device_uuid="lsc", question="?", kind="confirm", title="",
        options=[], severity="info", timeout=0.0, on_timeout="stay", created=0.0))
    assert w.interactions.get("rp-2") is not None
    w._on_remote_prompt_closed("rp-2")
    assert w.interactions.get("rp-2") is None

    # AGENT: a viewer answered OUR prompt → resolve locally (driver cb + provenance tag +
    # a close broadcast so every surface withdraws).
    answered = []
    q = Prompt("dev-x", "Proceed?", kind=CONFIRM)
    w.interactions.add(q, answered.append)
    w._on_agent_prompt_answered(q.id, True, "viewer-b")
    assert answered == [True]                                     # the driver callback fired
    assert (q.id, "Yes", "hub:viewer-b") in w.hub.closed          # resolution broadcast
    assert any(t.payload.get("prompt_id") == q.id for t in _interaction_tags())
