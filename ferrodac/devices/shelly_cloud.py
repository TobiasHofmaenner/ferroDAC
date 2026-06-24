"""Shelly Cloud account driver — one configurable "Shelly Cloud" device whose
channels are every sensor's temperature + humidity, read via the Shelly Cloud
Control API (no local network needed).

**Flow** — "Shelly Cloud" always shows up in the Devices panel. Add it, then open
⚙ Configure and enter:

  Server    — your account's server hostname (Shelly App → User Settings →
              Authorization Cloud Key, e.g. ``shelly-189-eu.shelly.cloud``).
              A full ``https://…`` URL is accepted too — the scheme is stripped.
  Auth key  — the Authorization Cloud Key (masked).

Once both are set the driver pulls ``/device/all_status`` — the WHOLE account in one
request — and exposes whatever **temperature / humidity** components each device
reports as channels (``Shelly <id> · Temperature`` / ``· Humidity``). Detecting
components (rather than allow-listing model codes, which go stale across Gen1/2/3)
means any temp/humidity sensor just works, and one bulk call per poll cycle keeps
us well under the cloud's ~1 req/s global rate limit regardless of sensor count.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request

from ..core.base import BaseDevice
from ..core.device import Interface, Option, RateControl, RateMode, Source

log = logging.getLogger("ferrodac.shelly")


def _get(server: str, path: str, params: dict, timeout: int = 20) -> dict:
    url = f"https://{server}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        result = json.loads(resp.read())
    if not result.get("isok"):
        raise RuntimeError(f"Shelly Cloud error: {result.get('errors', result)}")
    return result.get("data", {})


def _components(status: dict) -> list:
    """The temperature/humidity components a device status reports, as
    (status_key, value_field, unit, label) — Gen2/3 (temperature:N → tC,
    humidity:N → rh) and Gen1 (tmp/hum → value)."""
    out = []
    for key, v in status.items():
        if not isinstance(v, dict):
            continue
        base, _, idx = key.partition(":")
        suffix = f" {idx}" if idx and idx != "0" else ""
        if base in ("temperature", "tmp"):
            out.append((key, "tC" if "tC" in v else "value", "°C", "Temperature" + suffix))
        elif base in ("humidity", "hum"):
            out.append((key, "rh" if "rh" in v else "value", "% RH", "Humidity" + suffix))
    out.sort(key=lambda c: (0 if c[3].startswith("Temp") else 1, c[0]))   # temp before humidity
    return out


class ShellyCloud(BaseDevice):
    """A Shelly Cloud account — configure server + key, then its sensors stream as
    channels (one device, many sources)."""

    driver = "shelly_cloud"
    discoverable = True

    def __init__(self) -> None:
        self._cache: dict = {}          # {device_id: status} — whole account, one cycle
        self._cache_t: float = -1e9
        self._chan: dict = {}           # source_id -> (device_id, status_key, value_field)
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
            model="cloud account",
            manufacturer="Allterco Robotics (Shelly)",
        )

    # -- discovery: always offer the account (config happens in the GUI) -----
    @classmethod
    def discover(cls) -> list["ShellyCloud"]:
        return [cls()]

    # -- config: (re)enumerate channels when server/key change ---------------
    def _on_option(self, key: str, value) -> None:
        if key in ("server", "auth_key"):
            self._refresh_sensors()

    def _connect(self) -> None:
        self._refresh_sensors()          # populate on add / session-restore

    def _creds(self):
        server = (self._option_values.get("server") or "").strip()
        server = re.sub(r"^https?://", "", server).rstrip("/")    # accept a full URL too
        return server, (self._option_values.get("auth_key") or "").strip()

    def _all_status(self) -> dict:
        """Bulk status of every device — {device_id: status} in ONE request, cached
        for the current poll cycle so every channel shares a single cloud call."""
        now = time.monotonic()
        interval = 1.0 / (self._rate_hz or (1 / 60))
        if self._cache and (now - self._cache_t) < interval * 0.5:
            return self._cache
        server, auth = self._creds()
        data = _get(server, "/device/all_status", {"auth_key": auth})
        self._cache = data.get("devices_status", {}) or {}
        self._cache_t = now
        return self._cache

    @staticmethod
    def _room_names(server: str, auth: str) -> dict:
        """{room_id: room_name} from /interface/room/list — the human-readable room names
        set in the Shelly app. Best-effort: on failure, channels just omit the room."""
        try:
            data = _get(server, "/interface/room/list", {"auth_key": auth})
        except Exception as exc:         # noqa: BLE001
            log.warning("Shelly Cloud: room list failed (omitting rooms): %s", exc)
            return {}
        out = {}
        for room in (data.get("rooms") or {}).values():
            rid, name = room.get("id"), (room.get("name") or "").strip()
            if rid is not None and name:
                out[rid] = name
        return out

    def _refresh_sensors(self) -> None:
        server, auth = self._creds()
        if not server or not auth:
            return                       # not configured yet — leave channels empty
        rooms = self._room_names(server, auth)       # room_id -> name (best-effort)
        time.sleep(1.1)                  # respect the cloud's ~1 req/s limit between calls
        try:                             # the list carries name + room_id + a status snapshot
            data = _get(server, "/interface/device/list", {"auth_key": auth})
        except Exception as exc:         # noqa: BLE001 — surface WHY (not a silent [])
            log.warning("Shelly Cloud: device list failed: %s", exc)
            return
        sources, chan = [], {}
        for sid, dev in sorted((data.get("devices") or {}).items()):
            if not isinstance(dev, dict) or dev.get("category") != "sensor":
                continue                 # skip relays / non-sensors (no temp/humidity)
            name = (dev.get("name") or "").strip() or f"Shelly {sid[-6:]}"
            room = rooms.get(dev.get("room_id"))
            tail = f" ({room})" if room else ""
            status = (dev.get("ss") or {}).get("status") or {}
            for skey, field, unit, label in _components(status):
                src_id = f"{sid}_{skey.replace(':', '_')}"
                sources.append(Source(id=src_id, name=f"{name} · {label}{tail}", unit=unit))
                chan[src_id] = (sid, skey, field)
        self._chan, self._sources = chan, sources    # poll loop picks up next cycle
        log.info("Shelly Cloud: %d channel(s) across %d sensor(s)",
                 len(sources), len({c[0] for c in chan.values()}))

    def _read(self, source: Source):
        info = self._chan.get(source.id)
        if info is None:
            return float("nan"), 1
        sid, skey, field = info
        try:
            status = self._all_status().get(sid, {})
        except Exception:                # noqa: BLE001 — a flaky call → NaN sample
            return float("nan"), 1
        val = (status.get(skey) or {}).get(field)
        return (float(val), 0) if val is not None else (float("nan"), 1)
