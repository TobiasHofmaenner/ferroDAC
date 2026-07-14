"""The local control API over a REAL HTTP round-trip — pairing → bearer → dispatch.

Stands up LocalApiServer (Starlette+uvicorn, loopback) over a ControlSurface +
ConnectorRegistry and drives it with an httpx client exactly as an external connector
(an LLM assistant) would: pair, get approved, discover the API, invoke verbs, and be
gated by scope. Qt-free (the surface's handlers here are plain functions)."""

import os
import tempfile
import time

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")
pytest.importorskip("uvicorn")

from ferrodac.core.control import ControlSurface
from ferrodac.core.connectors import ConnectorRegistry
from ferrodac.net.localapi import LocalApiServer


@pytest.fixture
def server():
    surface = ControlSurface()
    state = {"voltage": 0.0}
    surface.register("device.set_voltage",
                     lambda p: state.__setitem__("voltage", float(p["value"])) or
                     {"voltage": state["voltage"]},
                     scope="control", description="set the PSU voltage",
                     params={"value": {"type": "number", "required": True}})
    surface.query("device.voltage", lambda p: state["voltage"])
    surface.register("project.delete", lambda p: {"deleted": p["id"]},
                     scope="admin", destructive=True,
                     params={"id": {"type": "string", "required": True}})

    tmp = tempfile.mkdtemp()
    reg = ConnectorRegistry(path=os.path.join(tmp, "connectors.json"))
    audit = []
    srv = LocalApiServer(surface, reg, port=0, version="test", config_dir=tmp,
                         on_audit=lambda *a: audit.append(a))
    srv.start()
    srv._audit_log = audit                      # expose for assertions
    yield srv, reg
    srv.stop()


def _base(srv):
    return f"http://127.0.0.1:{srv.port}"


def _pair(srv, reg, scope="control", grant=None):
    """Run the pairing handshake, simulating the user clicking Approve."""
    with httpx.Client(base_url=_base(srv), timeout=5.0) as c:
        r = c.post("/pair", json={"name": "assistant", "scope": scope})
        pid = r.json()["pairing_id"]
        assert len(r.json()["verification_code"]) == 6
        reg.approve(pid, scope=grant)           # the popup's Approve
        tok = c.get(f"/pair/{pid}").json()["token"]
    return tok


def test_health_needs_no_auth(server):
    srv, _ = server
    r = httpx.get(_base(srv) + "/health", timeout=5.0)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_pair_then_describe_and_command(server):
    srv, reg = server
    tok = _pair(srv, reg, scope="control")
    h = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(base_url=_base(srv), timeout=5.0, headers=h) as c:
        # discover the API — scope-filtered (no admin verbs for a control connector)
        d = c.get("/describe").json()
        verbs = {v["name"] for v in d["verbs"]}
        assert "device.set_voltage" in verbs and "device.voltage" in verbs
        assert "project.delete" not in verbs
        assert d["connector"]["scope"] == "control"
        # invoke a command + read it back via a query
        assert c.post("/command/device.set_voltage",
                      json={"payload": {"value": 12.0}}).json()["result"]["voltage"] == 12.0
        assert c.get("/query/device.voltage").json()["result"] == 12.0


def test_auth_is_required_and_scope_is_enforced(server):
    srv, reg = server
    # no token → 401
    assert httpx.post(_base(srv) + "/command/device.set_voltage",
                      json={"payload": {"value": 1}}, timeout=5.0).status_code == 401
    # a READ-only connector can't invoke a control command → 403
    tok = _pair(srv, reg, scope="control", grant="read")
    h = {"Authorization": f"Bearer {tok}"}
    with httpx.Client(base_url=_base(srv), timeout=5.0, headers=h) as c:
        assert c.post("/command/device.set_voltage",
                      json={"payload": {"value": 1}}).status_code == 403
        assert c.get("/query/device.voltage").status_code == 200        # reads are fine
        # a bad param → 400
        tok2 = _pair(srv, reg, scope="control")
        c.headers["Authorization"] = f"Bearer {tok2}"
        assert c.post("/command/device.set_voltage", json={"payload": {}}).status_code == 400


def test_revoked_token_stops_working(server):
    srv, reg = server
    tok = _pair(srv, reg, scope="control")
    h = {"Authorization": f"Bearer {tok}"}
    assert httpx.get(_base(srv) + "/describe", headers=h, timeout=5.0).status_code == 200
    reg.revoke(reg.list()[0]["id"])
    assert httpx.get(_base(srv) + "/describe", headers=h, timeout=5.0).status_code == 401


def test_nan_result_is_a_clean_400_not_500(server):
    # a verb returning a NaN (or otherwise non-JSON-serializable) result must not crash
    # the response with a 500 — the server turns it into a clean 400.
    srv, reg = server
    srv._surface.query("debug.nan", lambda p: float("nan"))
    tok = _pair(srv, reg, scope="control")
    r = httpx.get(_base(srv) + "/query/debug.nan",
                  headers={"Authorization": f"Bearer {tok}"}, timeout=5.0)
    assert r.status_code == 400 and "non-JSON" in r.json()["error"]


def test_events_stream_delivers_emitted_events(server):
    srv, reg = server
    surface = srv._surface
    tok = _pair(srv, reg, scope="read")
    h = {"Authorization": f"Bearer {tok}"}
    got = []
    with httpx.Client(base_url=_base(srv), timeout=10.0, headers=h) as c:
        with c.stream("GET", "/events") as r:
            assert r.status_code == 200
            # emit from the test thread; the server hops it onto its loop → SSE
            import threading
            threading.Timer(0.2, lambda: surface.emit("device.added",
                                                      id="dev/1")).start()
            deadline = time.time() + 5
            for line in r.iter_lines():
                if line.startswith("data:") and "device.added" in line:
                    got.append(line)
                    break
                if time.time() > deadline:
                    break
    assert got and "dev/1" in got[0]
