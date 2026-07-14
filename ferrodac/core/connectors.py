"""ConnectorRegistry — paired external connectors (local API clients) + the pairing
flow. Qt-free.

A connector is an RBAC principal (DESIGN §13) with a scope (read < control < admin).
Auth is a per-connector BEARER TOKEN issued by a pairing-with-approval flow: an external
client asks to pair, the app pops up an approval dialog with a short VERIFICATION CODE
(so the human confirms it's the right client), and on approval a token is minted. Tokens
are stored HASHED (a stolen registry file yields no usable tokens), scoped, and revocable.
Persisted to the app config dir, mode 0600 (owner-only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass

from .control import SCOPES


def default_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "ferrodac")


def _now() -> float:
    return time.time()


def _hash_token(tok: str) -> str:
    return hashlib.sha256(("fdc-v1:" + (tok or "")).encode("utf-8")).hexdigest()


@dataclass
class Connector:
    id: str
    name: str
    scope: str
    token_hash: str
    created: float
    last_seen: float = 0.0
    revoked: bool = False

    def public(self) -> dict:
        """Safe to show in the UI / API — never includes the token."""
        return {"id": self.id, "name": self.name, "scope": self.scope,
                "created": self.created, "last_seen": self.last_seen,
                "revoked": self.revoked}


@dataclass
class Pairing:
    id: str
    name: str
    code: str                          # verification code shown in client AND popup
    scope: str                         # requested scope (user may downgrade on approve)
    created: float
    status: str = "pending"            # pending | approved | denied | expired
    token: str = ""                    # set once on approve (handed to the client, then cleared)


class ConnectorRegistry:
    PAIR_TTL = 180.0                   # a pairing request expires after 3 min unanswered

    def __init__(self, path: "str | None" = None):
        self._path = path or os.path.join(default_config_dir(), "connectors.json")
        self._lock = threading.RLock()
        self._conns: dict[str, Connector] = {}
        self._pending: dict[str, Pairing] = {}
        self._on_pairing = None         # cb(Pairing) — notify the UI to pop the dialog
        self._load()

    def set_pairing_notifier(self, cb) -> None:
        """The UI installs this so a POST /pair pops an approval dialog (marshalled to
        the GUI thread by the caller)."""
        self._on_pairing = cb

    # -- pairing -------------------------------------------------------------
    def request_pairing(self, name: str, scope: str = "read") -> Pairing:
        """An external client asks to pair. Creates a PENDING request + notifies the UI;
        the client then polls poll_pairing() until approved/denied."""
        self._expire()
        p = Pairing(id=secrets.token_hex(8), name=(str(name) or "connector")[:64],
                    code=f"{secrets.randbelow(1_000_000):06d}",
                    scope=scope if scope in SCOPES else "read", created=_now())
        with self._lock:
            self._pending[p.id] = p
        if self._on_pairing is not None:
            try:
                self._on_pairing(p)
            except Exception:               # noqa: BLE001 — a bad notifier ≠ break pairing
                pass
        return p

    def approve(self, pairing_id: str, scope: "str | None" = None) -> "str | None":
        """User approved (optionally downgrading the scope) → mint + store a token,
        return it ONCE. None if the pairing is gone/answered."""
        with self._lock:
            p = self._pending.get(pairing_id)
            if p is None or p.status != "pending":
                return None
            granted = scope if scope in SCOPES else p.scope
            tok = "fdc_" + secrets.token_urlsafe(32)
            c = Connector(id=secrets.token_hex(8), name=p.name, scope=granted,
                          token_hash=_hash_token(tok), created=_now())
            self._conns[c.id] = c
            p.status, p.token = "approved", tok
            self._save()
        return tok

    def deny(self, pairing_id: str) -> None:
        with self._lock:
            p = self._pending.get(pairing_id)
            if p is not None and p.status == "pending":
                p.status = "denied"

    def poll_pairing(self, pairing_id: str) -> "Pairing | None":
        """The client polls this; on 'approved' it reads (and we clear) the token."""
        self._expire()
        with self._lock:
            p = self._pending.get(pairing_id)
            if p is None:
                return None
            snap = Pairing(**asdict(p))
            if p.status in ("approved", "denied"):
                self._pending.pop(pairing_id, None)   # one-shot: gone after it's read
            return snap

    # -- auth ----------------------------------------------------------------
    def authenticate(self, token: str) -> "Connector | None":
        """A bearer token → its (live, non-revoked) connector, stamping last_seen."""
        h = _hash_token(token)
        with self._lock:
            for c in self._conns.values():
                if not c.revoked and hmac.compare_digest(c.token_hash, h):
                    c.last_seen = _now()
                    return c
        return None

    # -- management ----------------------------------------------------------
    def list(self) -> list:
        with self._lock:
            return [c.public() for c in self._conns.values() if not c.revoked]

    def revoke(self, conn_id: str) -> bool:
        with self._lock:
            c = self._conns.get(conn_id)
            if c is None or c.revoked:
                return False
            c.revoked = True
            self._save()
        return True

    # -- pre-shared connectors (phone companion; no pairing handshake) -------
    def create_preshared(self, name: str, scope: str = "control") -> "tuple[Connector, str]":
        """Mint a PRE-APPROVED connector for a pre-shared-key client (the phone
        companion): no pairing/approval popup — the app already trusts the device it
        hands the link to. Stores only the token HASH; returns the plaintext psk ONCE
        (it lives in the /enter link + the phone's cookie, never on disk)."""
        tok = "fdc_" + secrets.token_urlsafe(32)
        with self._lock:
            c = Connector(id=secrets.token_hex(8), name=(str(name) or "phone")[:64],
                          scope=scope if scope in SCOPES else "control",
                          token_hash=_hash_token(tok), created=_now())
            self._conns[c.id] = c
            self._save()
        return c, tok

    def find_preshared(self, name: str) -> "Connector | None":
        """The live (non-revoked) pre-shared connector of this name, or None."""
        with self._lock:
            for c in self._conns.values():
                if not c.revoked and c.name == name:
                    return c
        return None

    def rotate_preshared(self, name: str, scope: str = "control") -> "tuple[Connector, str]":
        """'Get a new phone link': revoke every existing connector of this name and mint
        a fresh pre-shared one — the old QR/URL (and its cookie) stops working at once."""
        with self._lock:
            for c in self._conns.values():
                if not c.revoked and c.name == name:
                    c.revoked = True
            tok = "fdc_" + secrets.token_urlsafe(32)
            c = Connector(id=secrets.token_hex(8), name=(str(name) or "phone")[:64],
                          scope=scope if scope in SCOPES else "control",
                          token_hash=_hash_token(tok), created=_now())
            self._conns[c.id] = c
            self._save()
        return c, tok

    # -- persistence (owner-only file) --------------------------------------
    def _expire(self) -> None:
        now = _now()
        with self._lock:
            for pid in [k for k, p in self._pending.items()
                        if p.status == "pending" and now - p.created > self.PAIR_TTL]:
                self._pending[pid].status = "expired"
                self._pending.pop(pid, None)

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return
        with self._lock:
            for rec in data.get("connectors", []):
                try:
                    self._conns[rec["id"]] = Connector(**rec)
                except (TypeError, KeyError):
                    continue

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"connectors": [asdict(c) for c in self._conns.values()]}, fh,
                      indent=2)
        try:
            os.chmod(tmp, 0o600)            # owner-only (tokens are hashed, but still)
        except OSError:
            pass
        os.replace(tmp, self._path)
