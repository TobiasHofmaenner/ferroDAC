"""Shelly Cloud account driver — one configurable "Shelly Cloud" device whose
channels are every H&T sensor's temperature + humidity, read via the Shelly Cloud
Control API (no local network needed).

**Flow** — "Shelly Cloud" always shows up in the Devices panel. Add it, then open
⚙ Configure and enter:

  Server    — your account's server hostname (Shelly App → User Settings →
              Authorization Cloud Key, e.g. ``shelly-49-eu.shelly.cloud``)
  Auth key  — the Authorization Cloud Key (masked)

Once both are set the driver calls ``/interface/device/list``, filters for known
H&T device types, and exposes each sensor as two channels — ``<name> · Temperature``
and ``<name> · Humidity``.

**Polling** — the cloud caches each sensor's last reported reading; the device
itself sleeps ~5 min between wake-ups, so the default poll (1/min) is conservative.
The cloud API rate-limits to ~1 req/s globally; one status call per sensor per cycle
keeps you well under that for a handful of sensors.

**Supported type codes** — SHHT-1 (Gen1), SNSN-0013A / SNSN-0043X (Gen3). Add more
to ``_HT_TYPES`` as Shelly ships new hardware.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request

from ..core.base import BaseDevice
from ..core.device import Interface, Option, RateControl, RateMode, Source

log = logging.getLogger("ferrodac.shelly")

# Known H&T device type codes — extend as Shelly ships new hardware.
_HT_TYPES: frozenset[str] = frozenset({
    "SHHT-1",      # Gen1 Shelly H&T
    "SNSN-0013A",  # Gen3 Shelly H&T
    "SNSN-0043X",  # Gen3 Shelly H&T (EU variant)
})


# --- HTTP (stdlib only — no extra deps) ------------------------------------
def _request(url: str, data: bytes = None, method: str = "GET", timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url, data=data, method=method,
        headers=({"Content-Type": "application/x-www-form-urlencoded"} if data else {}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read())
    if not result.get("isok"):
        raise RuntimeError(f"Shelly Cloud error: {result.get('errors', result)}")
    return result.get("data", {})


def _post(server: str, path: str, payload: dict) -> dict:
    return _request(f"https://{server}{path}",
                    data=urllib.parse.urlencode(payload).encode(), method="POST")


def _get(server: str, path: str, params: dict) -> dict:
    return _request(f"https://{server}{path}?{urllib.parse.urlencode(params)}")


class ShellyCloud(BaseDevice):
    """A Shelly Cloud account — configure server + key, then its H&T sensors stream
    as channels (one device, many sources)."""

    driver = "shelly_cloud"
    discoverable = True

    def __init__(self) -> None:
        self._cache: dict = {}          # sensor_id -> (monotonic, status dict)
        self._chan: dict = {}           # source_id -> (sensor_id, metric)
        super().__init__(
            instance_id="shelly:cloud",
            name="Shelly Cloud",
            interface=Interface(kind="cloud", params={}),
            sources=[],                 # populated once configured
            options=[
                Option("server", "Server", kind="text"),
                Option("auth_key", "Auth key", kind="secret"),
            ],
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=1 / 300,
                             default_hz=1 / 60, min_hz=1 / 3600, max_hz=1 / 30),
            hardware_id="shelly-cloud-account",
            model="H&T account",
            manufacturer="Allterco Robotics (Shelly)",
        )

    # -- discovery: always offer the account (config happens in the GUI) -----
    @classmethod
    def discover(cls) -> list["ShellyCloud"]:
        return [cls()]

    # -- config: (re)enumerate the sensors when server/key change ------------
    def _on_option(self, key: str, value) -> None:
        if key in ("server", "auth_key"):
            self._refresh_sensors()

    def _connect(self) -> None:
        self._refresh_sensors()          # populate on add / session-restore

    def _creds(self):
        return ((self._option_values.get("server") or "").strip(),
                (self._option_values.get("auth_key") or "").strip())

    def _refresh_sensors(self) -> None:
        server, auth = self._creds()
        if not server or not auth:
            return                       # not configured yet — leave channels empty
        try:
            sensors = self._list_sensors(server, auth)
        except Exception as exc:         # noqa: BLE001 — surface WHY (not silent)
            log.warning("Shelly Cloud: device list failed: %s", exc)
            return
        sources, chan = [], {}
        for s in sensors:
            sid, name = s["id"], s["name"]
            for metric, unit, label in (("temperature", "°C", "Temperature"),
                                        ("humidity", "% RH", "Humidity")):
                src_id = f"{sid}_{metric}"
                sources.append(Source(id=src_id, name=f"{name} · {label}", unit=unit))
                chan[src_id] = (sid, metric)
        self._chan, self._sources = chan, sources    # poll loop picks up next cycle
        log.info("Shelly Cloud: %d sensor(s), %d channel(s)", len(sensors), len(sources))

    @staticmethod
    def _list_sensors(server: str, auth: str) -> list:
        data = _get(server, "/interface/device/list", {"auth_key": auth})
        # older cloud → dict keyed by id; newer → list
        raw = data.get("devices_status", data)
        if isinstance(raw, dict):
            items = [{"id": k, **v} for k, v in raw.items()]
        elif isinstance(raw, list):
            items = raw
        else:
            return []
        out = []
        for dev in items:
            did = dev.get("id") or dev.get("_id")
            dtype = dev.get("type") or dev.get("_type") or ""
            if did and dtype in _HT_TYPES:
                out.append({"id": did, "name": dev.get("name") or did})
        return out

    # -- data plane ----------------------------------------------------------
    def _fetch_status(self, sid: str) -> dict:
        """Per-sensor status, cached for the current poll cycle (so temperature and
        humidity of the same sensor share one cloud call)."""
        now = time.monotonic()
        interval = 1.0 / (self._rate_hz or (1 / 60))
        cached = self._cache.get(sid)
        if cached is not None and (now - cached[0]) < interval * 0.5:
            return cached[1]
        server, auth = self._creds()
        data = _post(server, "/device/status", {"auth_key": auth, "id": sid})
        status = data.get("device_status", data)
        self._cache[sid] = (now, status)
        return status

    @staticmethod
    def _extract(status: dict, metric: str):
        if metric == "temperature":      # Gen2/3: temperature:0→{tC} | Gen1: tmp→{value}
            obj = status.get("temperature:0") or status.get("tmp") or {}
            return obj.get("tC") if "tC" in obj else obj.get("value")
        obj = status.get("humidity:0") or status.get("hum") or {}   # Gen2/3 rh | Gen1 value
        return obj.get("rh") if "rh" in obj else obj.get("value")

    def _read(self, source: Source):
        sid, metric = self._chan.get(source.id, (None, None))
        if sid is None:
            return float("nan"), 1
        try:
            val = self._extract(self._fetch_status(sid), metric)
        except Exception:                # noqa: BLE001 — a flaky call → NaN sample
            return float("nan"), 1
        return (float(val), 0) if val is not None else (float("nan"), 1)
