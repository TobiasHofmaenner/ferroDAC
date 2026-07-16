"""ControlSurface — the app's self-describing command/query/event registry.

This is the ONE programmatic surface over the application's functions (connect a hub,
add/control a device, change layout, switch project, tag, scrub the timeline, …). It
is transport-agnostic: the local FastAPI server, a hub relay, a Python SDK, or an MCP
adapter are all thin clients over this registry. Qt-free — a GUI-mutating handler is
wrapped BY THE CALLER (the app) to run on the GUI thread, so this module never imports
Qt and can be reasoned about / tested headlessly.

Self-describing: every verb declares a param spec + description + minimum scope + a
`destructive` flag, so a consumer (e.g. an LLM assistant) DISCOVERS the API at runtime
via `describe()`. `dispatch()` enforces the caller's scope before running the handler.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Scope tiers form a TOTAL ORDER read < control < admin, and map onto the hub's §13
# RBAC actions (read → read/subscribe; control → command/configure/publish; admin →
# +delete/admin). A connector is granted one scope; a verb declares the minimum it needs.
SCOPES = ("read", "control", "admin")


def scope_rank(scope: str) -> int:
    try:
        return SCOPES.index(scope)
    except ValueError:
        return -1


class ControlError(Exception):
    """A verb was unknown, its params were invalid, or its handler failed."""


class ScopeError(Exception):
    """The caller's scope is insufficient (or a destructive verb lacked confirm)."""


@dataclass
class Verb:
    name: str
    handler: Callable[[dict], Any]     # handler(payload: dict) -> JSON-able result
    kind: str                          # "command" (mutates) | "query" (reads)
    scope: str                         # minimum scope to invoke
    description: str
    params: dict                       # {pname: {type, required, description, enum?}}
    returns: str
    destructive: bool                  # needs scope=admin AND confirm=true


class ControlSurface:
    def __init__(self) -> None:
        self._verbs: dict[str, Verb] = {}
        self._subs: list = []               # event subscribers: cb(event: dict)
        self._lock = threading.RLock()
        self._context_provider = None       # () -> ambient scope dict (active project, time…)

    # -- ambient context (§ the scope every response is implicitly against) ---
    def set_context_provider(self, fn) -> None:
        """Register a callable returning the AMBIENT scope a response is implicitly against
        — the active project, the time mode. The API layer stamps it onto every response so a
        stateless client can tell 'state changed' from 'different project active' (issue #7)."""
        self._context_provider = fn

    def context(self) -> dict:
        fn = self._context_provider
        if fn is None:
            return {}
        try:
            return dict(fn() or {})
        except Exception:                   # noqa: BLE001 — context is best-effort, never fatal
            return {}

    # -- registration (the app / core modules call this) --------------------
    def register(self, name: str, handler: Callable[[dict], Any], *,
                 kind: str = "command", scope: str = "control", description: str = "",
                 params: Optional[dict] = None, returns: str = "",
                 destructive: bool = False) -> None:
        if scope not in SCOPES:
            raise ValueError(f"bad scope {scope!r}")
        with self._lock:
            self._verbs[name] = Verb(name, handler, kind, scope, description,
                                     dict(params or {}), returns, bool(destructive))

    def query(self, name: str, handler, *, scope: str = "read", **kw) -> None:
        """Convenience: register a read-only query (default scope 'read')."""
        self.register(name, handler, kind="query", scope=scope, **kw)

    # -- introspection (the consumer learns the API) ------------------------
    def describe(self, scope: str = "admin") -> dict:
        """The catalog of verbs the given scope may invoke — the tool list an LLM /
        SDK / MCP adapter builds from. Filtered so a consumer never sees verbs above
        its grant."""
        with self._lock:
            verbs = [
                {"name": v.name, "kind": v.kind, "scope": v.scope,
                 "description": v.description, "params": v.params,
                 "returns": v.returns, "destructive": v.destructive}
                for v in sorted(self._verbs.values(), key=lambda x: x.name)
                if scope_rank(scope) >= scope_rank(v.scope)
            ]
        return {"scopes": list(SCOPES), "verbs": verbs}

    # -- invocation ----------------------------------------------------------
    def dispatch(self, name: str, payload: Optional[dict] = None, *,
                 scope: str = "admin", confirm: bool = False, caller: str = "") -> Any:
        """Run a verb on behalf of a caller with `scope`. Raises ScopeError if the
        scope is too low (or a destructive verb wasn't confirmed) and ControlError on
        an unknown verb / bad params / handler failure. `caller` (the connector name, when
        known) is injected as the reserved, un-spoofable payload key ``_caller`` so a verb
        can attribute an action to WHO invoked it (e.g. device.respond's provenance tag)."""
        with self._lock:
            v = self._verbs.get(name)
        if v is None:
            raise ControlError(f"unknown verb: {name!r}")
        if scope_rank(scope) < scope_rank(v.scope):
            raise ScopeError(f"{name} needs scope '{v.scope}', caller has '{scope}'")
        if v.destructive and not confirm:
            raise ScopeError(f"{name} is destructive — pass confirm=true")
        payload = dict(payload or {})
        payload.pop("_caller", None)            # reserved — never accept it from the wire
        if caller:
            payload["_caller"] = caller         # trusted, set by the transport (localapi/hub)
        for pname, spec in v.params.items():
            if spec.get("required") and pname not in payload:
                raise ControlError(f"{name}: missing required param {pname!r}")
        try:
            return v.handler(payload)
        except (ControlError, ScopeError):
            raise
        except Exception as exc:                # noqa: BLE001 — surface, don't leak a trace
            raise ControlError(f"{name} failed: {exc}") from exc

    # -- events (state-change stream a consumer subscribes to) --------------
    def subscribe(self, cb: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._subs.append(cb)
        return lambda: self._unsubscribe(cb)

    def _unsubscribe(self, cb) -> None:
        with self._lock:
            if cb in self._subs:
                self._subs.remove(cb)

    def emit(self, event: str, **data) -> None:
        """Broadcast a state-change event to subscribers (fan-out isolated per sink)."""
        payload = {"event": event, **data}
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(payload)
            except Exception:                   # noqa: BLE001 — a bad subscriber ≠ break
                pass
