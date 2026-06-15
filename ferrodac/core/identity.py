"""Device identity & the onboarding registry.

A device's stable identity is a **UUID**, minted the first time it is onboarded
(added). Hardware can't carry our UUID, so it lives on a registry record keyed by
the device's **fingerprint** ``(driver, hardware_id)``. The registry is the
UUID ↔ hardware bridge: a local JSON file now, the networked hub later.

``instance_id`` stays a *physical address* (how a driver reaches the hardware);
the **UUID is the data-plane identity** — Readings, routes and saved layouts all
key on it, so a layout is portable across machines and resolvable to a local or
(later) remote device.
"""

from __future__ import annotations

import json
import os
import uuid as _uuid
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Fingerprint:
    """What identifies *the same instrument* across sessions and machines."""
    driver: str
    hardware_id: str

    @property
    def key(self) -> str:
        return f"{self.driver}::{self.hardware_id}"


class DeviceRegistry:
    """Persistent ``uuid ↔ fingerprint`` map. ``path=None`` keeps it in-memory."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._by_uuid: dict[str, dict] = {}      # uuid -> record
        self._by_fp: dict[str, str] = {}         # fingerprint.key -> uuid
        self._load()

    # -- lookups -------------------------------------------------------------
    def uuid_for(self, fp: Fingerprint) -> Optional[str]:
        return self._by_fp.get(fp.key)

    def fingerprint_for(self, uuid: str) -> Optional[Fingerprint]:
        rec = self._by_uuid.get(uuid)
        if rec is None:
            return None
        return Fingerprint(rec["driver"], rec["hardware_id"])

    def friendly_for(self, uuid: str) -> Optional[str]:
        rec = self._by_uuid.get(uuid)
        return rec.get("friendly") if rec else None

    def known(self, uuid: str) -> bool:
        return uuid in self._by_uuid

    # -- mutation ------------------------------------------------------------
    def register(self, fp: Fingerprint, friendly: str = "") -> str:
        """Get-or-create the UUID for a fingerprint (minted at onboarding)."""
        existing = self._by_fp.get(fp.key)
        if existing is not None:
            if friendly:
                self._by_uuid[existing]["friendly"] = friendly
                self._save()
            return existing
        uid = str(_uuid.uuid4())
        self._by_uuid[uid] = {
            "driver": fp.driver, "hardware_id": fp.hardware_id, "friendly": friendly,
        }
        self._by_fp[fp.key] = uid
        self._save()
        return uid

    def adopt(self, uuid: str, fp: Fingerprint, friendly: str = "") -> None:
        """Insert a known (uuid, fingerprint) — used when loading a session whose
        devices this machine has never onboarded."""
        if uuid in self._by_uuid:
            return
        self._by_uuid[uuid] = {
            "driver": fp.driver, "hardware_id": fp.hardware_id, "friendly": friendly,
        }
        self._by_fp[fp.key] = uuid
        self._save()

    # -- persistence ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._by_uuid = dict(data.get("devices", {}))
            self._by_fp = {
                Fingerprint(r["driver"], r["hardware_id"]).key: uid
                for uid, r in self._by_uuid.items()
            }
        except Exception:
            self._by_uuid, self._by_fp = {}, {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "devices": self._by_uuid}, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass
