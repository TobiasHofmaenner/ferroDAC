"""The phone companion over a REAL HTTP round-trip (Starlette+uvicorn, LAN bind on
loopback for the test). Drives it exactly as a phone browser would: the app mints a
pre-shared connector, the phone hits /enter?k=psk (gets an HttpOnly cookie), loads the
mobile page, then POSTs a base64 photo to /upload which dispatches media.add_photo.
Qt-free — the surface's media.add_photo here is a plain recorder (no MainWindow)."""

import base64
import os
import tempfile

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")
pytest.importorskip("uvicorn")

from ferrodac.core.control import ControlSurface
from ferrodac.core.connectors import ConnectorRegistry
from ferrodac.net.companion import CompanionServer


@pytest.fixture
def companion():
    surface = ControlSurface()
    calls = []
    # a fake media.add_photo that just records the dispatched payload
    surface.register(
        "media.add_photo",
        lambda p: (calls.append(p) or {"tag_id": "t1", "relpath": "media/x.png"}),
        scope="control", description="add an uploaded photo (test stub)")

    tmp = tempfile.mkdtemp()
    reg = ConnectorRegistry(path=os.path.join(tmp, "connectors.json"))
    conn, psk = reg.create_preshared("Phone companion", scope="control")

    srv = CompanionServer(surface, reg, host="127.0.0.1", port=0, version="test",
                          get_project=lambda: "Anneal Run 7")
    srv.start()
    yield srv, reg, psk, calls
    srv.stop()


def _base(srv):
    return f"http://127.0.0.1:{srv.port}"


def test_create_preshared_mints_authenticable_token():
    reg = ConnectorRegistry(path=os.path.join(tempfile.mkdtemp(), "c.json"))
    conn, psk = reg.create_preshared("phone", scope="control")
    assert psk.startswith("fdc_")
    assert reg.authenticate(psk) is not None            # the plaintext works
    assert conn.token_hash != psk                       # only the HASH is stored
    assert reg.find_preshared("phone").id == conn.id


def test_health_needs_no_auth(companion):
    srv, _, _, _ = companion
    r = httpx.get(_base(srv) + "/health", timeout=5.0)
    assert r.status_code == 200 and r.json()["ok"] is True


def test_enter_sets_cookie_then_page_then_upload(companion):
    srv, reg, psk, calls = companion
    with httpx.Client(base_url=_base(srv), timeout=5.0) as c:
        # no cookie yet -> the 'pair from the app' page
        assert "Not connected" in c.get("/").text

        # /enter validates the psk, redirects, and drops an HttpOnly session cookie
        r = c.get("/enter", params={"k": psk}, follow_redirects=False)
        assert r.status_code == 302
        sc = r.headers.get("set-cookie", "")
        assert "fdc_sess=" in sc and "HttpOnly" in sc and "samesite=lax" in sc.lower()
        assert c.cookies.get("fdc_sess") == psk

        # with the cookie, the mobile upload page renders (project header + capture input)
        page = c.get("/").text
        for needle in ("Upload photo", "capture=environment", "Anneal Run 7",
                       "Unencrypted connection", "<select id=category>"):
            assert needle in page, f"missing {needle!r}"

        # a tiny image blob -> /upload -> media.add_photo dispatched with decoded bytes
        raw = b"\xff\xd8\xff\xe0tiny-jpeg\xff\xd9"
        b64 = base64.b64encode(raw).decode()
        r = c.post("/upload", json={"category": "sample",
                                    "label": "after 2h anneal", "data_b64": b64})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert len(calls) == 1
        assert calls[0]["category"] == "sample"
        assert calls[0]["label"] == "after 2h anneal"
        assert calls[0]["data"] == raw                  # server base64-decoded it


def test_enter_with_bad_key_is_rejected(companion):
    srv, _, _, _ = companion
    r = httpx.get(_base(srv) + "/enter", params={"k": "nope"},
                  follow_redirects=False, timeout=5.0)
    assert r.status_code == 401 and "Invalid" in r.text


def test_upload_without_cookie_is_401(companion):
    srv, _, _, calls = companion
    b64 = base64.b64encode(b"x").decode()
    r = httpx.post(_base(srv) + "/upload",
                   json={"category": "setup", "data_b64": b64}, timeout=5.0)
    assert r.status_code == 401
    assert calls == []


def test_upload_needs_control_scope(companion):
    srv, reg, _, calls = companion
    _, read_psk = reg.create_preshared("read phone", scope="read")
    with httpx.Client(base_url=_base(srv), timeout=5.0) as c:
        c.get("/enter", params={"k": read_psk}, follow_redirects=False)
        r = c.post("/upload", json={"category": "setup",
                                    "data_b64": base64.b64encode(b"x").decode()})
        assert r.status_code == 403
        assert calls == []


def test_unknown_category_falls_back_to_generic(companion):
    srv, _, psk, calls = companion
    with httpx.Client(base_url=_base(srv), timeout=5.0) as c:
        c.get("/enter", params={"k": psk}, follow_redirects=False)
        r = c.post("/upload", json={"category": "bogus",
                                    "data_b64": base64.b64encode(b"y").decode()})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert calls[-1]["category"] == "generic"