"""A tiny, dependency-free client for the ferroDAC local control API.

This is the reference an LLM assistant (or any tool) uses to drive a running ferroDAC:
discover the loopback port, pair (a code pops in the app for the user to approve),
then discover the verb catalog and invoke commands/queries. Stdlib only — copy it into
your harness. The self-describing `describe()` output IS the tool list you hand the LLM.

    python examples/control_client.py           # pair + a small demo

Enable the API first: ferroDAC ▸ Cloud ▸ External Control… ▸ tick "Enable".
"""

from __future__ import annotations

import json
import os
import time
import urllib.request


class ControlClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    # -- discovery -----------------------------------------------------------
    @classmethod
    def discover(cls) -> "ControlClient":
        """Find the running app's loopback port via the discovery file."""
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        with open(os.path.join(base, "ferrodac", "connector.json")) as fh:
            d = json.load(fh)
        return cls(f"http://{d.get('host', '127.0.0.1')}:{d['port']}")

    # -- pairing -------------------------------------------------------------
    def pair(self, name: str, scope: str = "control", *, timeout: float = 180.0) -> str:
        """Request pairing, print the verification code, and block until the user
        approves in the app. Returns the bearer token (also stored on self.token)."""
        r = self._req("POST", "/pair", {"name": name, "scope": scope})
        pid, code = r["pairing_id"], r["verification_code"]
        print(f"Pairing '{name}' — approve in ferroDAC. Verification code: {code}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self._req("GET", f"/pair/{pid}")
            if st.get("status") == "approved":
                self.token = st["token"]
                return self.token
            if st.get("status") in ("denied", "expired", "unknown"):
                raise RuntimeError(f"pairing {st.get('status')}")
            time.sleep(0.5)
        raise TimeoutError("pairing not approved in time")

    # -- the API -------------------------------------------------------------
    def describe(self) -> dict:
        """The verb catalog (scope-filtered) — the LLM's tool list."""
        return self._req("GET", "/describe")

    def command(self, verb: str, payload: dict = None, *, confirm: bool = False) -> dict:
        return self._req("POST", f"/command/{verb}",
                         {"payload": payload or {}, "confirm": confirm})["result"]

    def query(self, verb: str, params: dict = None) -> dict:
        q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        return self._req("GET", f"/query/{verb}" + (f"?{q}" if q else ""))["result"]

    # -- transport -----------------------------------------------------------
    def _req(self, method: str, path: str, body: dict = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{exc.code} {path}: {detail}") from None


if __name__ == "__main__":
    c = ControlClient.discover()
    c.pair("demo-assistant", scope="control")
    print("\nVerbs:", ", ".join(v["name"] for v in c.describe()["verbs"]))
    print("\nHub status:", c.query("hub.status"))
    print("Devices:", [d["instance_id"] for d in c.query("device.list")["active"]])
    # e.g. drive it:  c.command("hub.connect", {"addr": "192.168.236.140:50051"})
    #                 c.command("device.set_sink",
    #                           {"instance_id": "sim:psu:1", "sink_id": "voltage", "value": 5})
    #                 c.command("tag.add", {"label": "assistant was here"})
