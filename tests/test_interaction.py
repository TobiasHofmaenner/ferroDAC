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
    p = Prompt("dev-1", "Retract the arm?", kind=CONFIRM, severity="critical")
    assert p.id and p.created > 0                       # auto-minted id + created
    assert p.is_critical
    d = prompt_to_dict(p)
    import json
    json.dumps(d)                                       # strictly JSON-able (crosses the wire)
    back = prompt_from_dict(d)
    assert back.id == p.id and back.device_id == "dev-1"
    assert back.kind == CONFIRM and back.severity == "critical"

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
    # choice → one button per option, each answering with that option
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

    res = s.dispatch("device.respond", {"id": p.id, "answer": True}, scope="control")
    assert res == {"ok": True, "id": p.id, "answer": True}
    assert answered == [True]                            # the device callback fired
    assert s.dispatch("device.prompts", scope="read") == []   # closed

    # answering a gone/unknown request is an explicit error, not a silent no-op
    with pytest.raises(ControlError):
        s.dispatch("device.respond", {"id": p.id, "answer": True}, scope="control")

    # a provenance tag recorded the outcome (origin=device, immutable)
    tags = [m for m in w.dashboard.markers.all() if m.kind == "interaction"]
    assert tags and tags[-1].origin_kind == "device"
    assert tags[-1].payload.get("answer") is True


# -- a REAL localapi HTTP round-trip -----------------------------------------
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
