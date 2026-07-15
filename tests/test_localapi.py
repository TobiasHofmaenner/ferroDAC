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


def test_root_is_unauthenticated_bootstrap(server):
    """GET / (no auth) is the cold-start door: a client given only the base URL learns
    what the app is, how to pair, the endpoint map, and how to pilot it — the protocol
    self-describes. The full verb catalog is NOT leaked here (that's behind /describe)."""
    import json as _json
    srv, _ = server
    r = httpx.get(_base(srv) + "/", timeout=5.0)          # no Authorization header
    assert r.status_code == 200
    d = r.json()
    assert d["app"] and "self-describing" in d["about"]
    assert any("/pair" in step for step in d["auth"]["handshake"])   # the handshake is spelled out
    assert "POST /pair" in d["endpoints"] and "GET /describe" in d["endpoints"]
    assert d["workflows"] and d["piloting"]               # how-to + best practices present
    # areas are derived from the live surface, but the catalog itself stays behind auth
    assert d["verbs"]["count"] == 3
    assert set(d["verbs"]["areas"]) == {"device", "project"}
    assert "device.set_voltage" not in _json.dumps(d)     # no full verb list leaked at the root


def test_start_raises_on_a_busy_port(server):
    """start() RAISES if the port can't bind (a busy port), so _enable_control_api can fall
    back to an ephemeral one instead of hanging / reporting a phantom port."""
    srv, _ = server
    tmp = tempfile.mkdtemp()
    dup = LocalApiServer(ControlSurface(),
                         ConnectorRegistry(path=os.path.join(tmp, "c.json")),
                         port=srv.port, version="t", config_dir=tmp)   # same, already-bound port
    with pytest.raises(Exception):
        dup.start()
    dup.stop()


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


def test_command_body_accepts_flat_or_wrapped_params(server):
    """Params may be sent FLAT ({"value": ...}) — what /describe's params suggest — or
    WRAPPED ({"params": {...}}); both work, and confirm stays top-level (issue #5)."""
    srv, reg = server
    tok = _pair(srv, reg, scope="control")
    with httpx.Client(base_url=_base(srv), timeout=5.0,
                      headers={"Authorization": f"Bearer {tok}"}) as c:
        assert c.post("/command/device.set_voltage",
                      json={"value": 7.0}).json()["result"]["voltage"] == 7.0      # flat
        assert c.post("/command/device.set_voltage",
                      json={"params": {"value": 9.0}}).json()["result"]["voltage"] == 9.0  # wrapped
        assert c.post("/command/device.set_voltage",
                      json={"payload": {"value": 3.0}}).json()["result"]["voltage"] == 3.0  # payload
    # a flat body + top-level confirm on a destructive (admin) verb
    tok_a = _pair(srv, reg, scope="admin")
    r = httpx.post(_base(srv) + "/command/project.delete",
                   json={"id": "p1", "confirm": True},
                   headers={"Authorization": f"Bearer {tok_a}"}, timeout=5.0)
    assert r.status_code == 200 and r.json()["result"]["deleted"] == "p1"


def test_responses_carry_context_and_expect_project_guard(server):
    """Every command/query/describe response carries an ambient 'context' (project +
    time_mode) so a stateless client can tell 'state changed' from 'different project'; a
    command may pin the project with expect_project (409 on mismatch) — issue #7."""
    srv, reg = server
    srv._surface.set_context_provider(
        lambda: {"project": {"id": "p1", "name": "LSC_Demo"}, "time_mode": "live"})
    tok = _pair(srv, reg, scope="control")
    with httpx.Client(base_url=_base(srv), timeout=5.0,
                      headers={"Authorization": f"Bearer {tok}"}) as c:
        # a QUERY response carries the context
        rq = c.get("/query/device.voltage").json()
        assert rq["context"]["project"]["name"] == "LSC_Demo"
        assert rq["context"]["time_mode"] == "live"
        # a COMMAND response echoes it too (the tight feedback loop for a builder client)
        rc = c.post("/command/device.set_voltage", json={"value": 5.0}).json()
        assert rc["result"]["voltage"] == 5.0 and rc["context"]["project"]["id"] == "p1"
        # expect_project MATCHES (by name) → runs
        assert c.post("/command/device.set_voltage",
                      json={"value": 6.0, "expect_project": "LSC_Demo"}).status_code == 200
        # expect_project MISMATCHES → 409, and the command did NOT run
        bad = c.post("/command/device.set_voltage",
                     json={"value": 999.0, "expect_project": "OTHER"})
        assert bad.status_code == 409 and "expect_project" in bad.json()["error"]
        assert c.get("/query/device.voltage").json()["result"] == 6.0   # 999 not applied
    # DESCRIBE carries the context as well
    d = httpx.get(_base(srv) + "/describe",
                  headers={"Authorization": f"Bearer {tok}"}, timeout=5.0).json()
    assert d["context"]["project"]["name"] == "LSC_Demo"


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
