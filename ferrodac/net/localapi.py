"""LocalApiServer — the loopback HTTP door to the ControlSurface (external connectors).

Binds 127.0.0.1 ONLY (a connector on another machine goes through the hub, not this
server — no local TLS to manage). Starlette ASGI app on uvicorn in a background thread
with its own asyncio loop; the blocking command dispatch (which may marshal to the GUI
thread) runs in a threadpool so it never stalls the loop. Auth is a per-connector bearer
token (ConnectorRegistry). Qt-free — the ControlSurface's GUI handlers do the marshalling.

  GET  /                                   -> what the app is + how to pilot it   (no auth)
  POST /pair {name, scope}                 -> {pairing_id, verification_code}   (no auth)
  GET  /pair/{id}                          -> {status, token?}          (no auth; one-shot)
  GET  /describe                           -> scope-filtered verb catalog        (bearer)
  POST /command/{verb} {payload?, confirm?}-> {result} | error                  (bearer)
  GET  /query/{verb}                       -> {result}   (params via ?a=b)       (bearer)
  GET  /events                             -> SSE state-change stream            (bearer)
  GET  /health                             -> {ok, name, version}               (no auth)

`GET /` is the cold-start door: a client given only the base URL learns from it what the
app is, how to pair/authenticate, the endpoint map, and how to drive it well — the protocol
self-describes, not just the verbs (`/describe`). A discovery file
(~/.config/ferrodac/connector.json, {host, port, pid}) additionally lets a same-user client
find an OS-assigned port; a pinned port makes even that unnecessary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from ..core.control import ControlError, ScopeError
from ..core.connectors import default_config_dir

log = logging.getLogger("ferrodac.localapi")


class LocalApiServer:
    def __init__(self, surface, connectors, *, host: str = "127.0.0.1", port: int = 0,
                 app_name: str = "ferroDAC", version: str = "", on_audit=None,
                 config_dir: "str | None" = None):
        self._surface = surface           # ControlSurface
        self._conns = connectors          # ConnectorRegistry
        self._host = host                 # ALWAYS loopback in production
        self._req_port = port
        self._name = app_name
        self._version = version
        self._config_dir = config_dir or default_config_dir()
        self._on_audit = on_audit         # (connector_name, verb, ok, detail) — trust log
        self._server: "uvicorn.Server | None" = None
        self._thread: "threading.Thread | None" = None
        self._port = 0
        self._app = self._build_app()

    @property
    def port(self) -> int:
        return self._port

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> int:
        # log_config=None: do NOT let uvicorn run logging.config.dictConfig. Its default
        # config builds a 'default' formatter (uvicorn.logging.DefaultFormatter) that can't
        # be configured in the frozen, console-less Windows build (no sys.stdout/stderr, and
        # the frozen importer can't resolve the formatter's dotted path) → the enable step
        # failed with "Unable to configure formatter 'default'". We use the app's own logging;
        # log_level/access_log below are still honoured (they don't depend on log_config).
        config = uvicorn.Config(self._app, host=self._host, port=self._req_port,
                                log_level="warning", access_log=False, log_config=None)
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None   # not on the main thread
        self._thread = threading.Thread(target=self._serve, name="fd-localapi",
                                        daemon=True)
        self._thread.start()
        for _ in range(500):              # wait for the socket to bind (≤5 s)
            if self._server.started:
                break
            if not self._thread.is_alive():   # _serve exited (e.g. port in use) — don't wait 5 s
                break
            time.sleep(0.01)
        if not self._server.started:      # bind failed → let the caller fall back / report
            raise RuntimeError(
                f"control API could not bind {self._host}:{self._req_port} (in use?)")
        self._port = self._bound_port()
        self._write_discovery()
        log.info("local control API on http://%s:%d", self._host, self._port)
        return self._port

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._remove_discovery()

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._server.serve())
        except Exception:                 # noqa: BLE001 — never crash the app thread
            log.debug("local API server ended", exc_info=True)

    def _bound_port(self) -> int:
        try:
            return self._server.servers[0].sockets[0].getsockname()[1]
        except (AttributeError, IndexError):
            return self._req_port

    # -- discovery file (same-user readable) --------------------------------
    def _discovery_path(self) -> str:
        return os.path.join(self._config_dir, "connector.json")

    def _write_discovery(self) -> None:
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            path = self._discovery_path()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"host": self._host, "port": self._port, "pid": os.getpid(),
                           "name": self._name, "version": self._version}, fh)
            os.chmod(path, 0o600)
        except OSError:
            log.debug("could not write the connector discovery file", exc_info=True)

    def _remove_discovery(self) -> None:
        try:
            os.remove(self._discovery_path())
        except OSError:
            pass

    # -- ASGI app ------------------------------------------------------------
    def _build_app(self) -> Starlette:
        return Starlette(routes=[
            Route("/", self._root, methods=["GET"]),
            Route("/health", self._health, methods=["GET"]),
            Route("/pair", self._pair, methods=["POST"]),
            Route("/pair/{pairing_id}", self._pair_poll, methods=["GET"]),
            Route("/describe", self._describe, methods=["GET"]),
            Route("/command/{verb}", self._command, methods=["POST"]),
            Route("/query/{verb}", self._query, methods=["GET"]),
            Route("/events", self._events, methods=["GET"]),
        ])

    def _auth(self, request: Request):
        """Bearer token → connector, or None (caller returns 401)."""
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h[:7].lower() == "bearer " else ""
        return self._conns.authenticate(token) if token else None

    async def _health(self, request: Request):
        return JSONResponse({"ok": True, "name": self._name, "version": self._version})

    async def _root(self, request: Request):
        """Cold-start discovery (no auth): what the app is, what it can do, how to
        authenticate, the endpoint map, and how to pilot it well. A client handed only
        the base URL can bootstrap from here; the full verb catalog is behind /describe
        once paired. Describes the app and protocol — not the caller."""
        try:
            verbs = self._surface.describe("admin").get("verbs", [])
        except Exception:                          # never let discovery fail
            verbs = []
        areas = sorted({v["name"].split(".", 1)[0] for v in verbs if v.get("name")})
        return JSONResponse({
            "app": self._name,
            "version": self._version,
            "about": (
                "ferroDAC is a local-first laboratory data-acquisition, control and "
                "documentation platform. It streams live readings from lab instruments, "
                "charts and records them to plain files (Zarr + CSV + Markdown), and lets "
                "you annotate the timeline, run experiments, and export results. This "
                "loopback HTTP API exposes the running app's own functions as a set of "
                "self-describing 'verbs', so an external program can pilot the app."),
            "capabilities": [
                "Read live and historical device data — sources are scalars, spectra/traces, or video",
                "Control instruments — sinks are setpoints, toggles, actions and enums",
                "Build the dashboard — add chart/readout panels and route sources onto them",
                "Annotate — drop tags on the shared timeline; devices raise their own alarm tags",
                "Answer device requests — a device can ask the operator a question mid-workflow (device.prompts / device.respond)",
                "Document — write project notes and docs alongside the data",
                "Record and export — record a labelled span (auto-exports a bundle), or pull a CSV for a window",
                "Manage projects and the shared hub; author Python devices that compute or fetch external data",
            ],
            "auth": {
                "scheme": "per-connector bearer token, scoped read < control < admin",
                "handshake": [
                    "POST /pair {\"name\": \"<who you are>\", \"scope\": \"read|control|admin\"} -> {pairing_id, verification_code}",
                    "A person approves the pairing inside the app, confirming the verification_code and granting a scope",
                    "GET /pair/{pairing_id} until status == 'approved' -> {token}  (returned once — store it)",
                    "Send 'Authorization: Bearer <token>' on every /describe, /command, /query and /events request",
                ],
            },
            "endpoints": {
                "GET /": "this document (no auth)",
                "GET /health": "liveness + name/version (no auth)",
                "POST /pair": "begin pairing (no auth)",
                "GET /pair/{id}": "poll a pairing; yields the token once approved (no auth)",
                "GET /describe": "the scope-filtered catalog of verbs, each with params, scope and a 'destructive' flag — the source of truth for what you can do (bearer)",
                "POST /command/{verb}": "run a command (mutating) verb; params in the JSON body either flat ({\"source_key\": ...}) or wrapped ({\"params\": {...}}), plus optional top-level {\"confirm\": true} and {\"expect_project\": \"<id-or-name>\"} (409 if the active project differs) (bearer)",
                "GET /query/{verb}": "run a query (read) verb; params as ?key=value (bearer)",
                "GET /events": "server-sent-events stream of state changes (bearer)",
            },
            "verbs": {
                "count": len(verbs),
                "areas": areas,
                "catalog": "GET /describe after pairing — read verbs from there, never hardcode them",
                "how_to": "the guidance.list / guidance.get verbs return step-by-step playbooks (the HOW) that complement /describe (the WHAT)",
            },
            "piloting": [
                "Discover first: after pairing, GET /describe and drive off it — don't assume a verb or its params exist.",
                "Least privilege: reads need 'read', mutations need 'control', a few verbs need 'admin'. Ask for the smallest scope that does the job.",
                "Destructive verbs (flagged in /describe) require {\"confirm\": true} in the command body — pass it deliberately.",
                "Errors: 401 = missing/invalid token, 403 = scope too low or confirm missing, 400 = bad params or a verb error.",
                "Command params go in the POST body — flat ({\"source_key\": ...}) or wrapped ({\"params\": {...}}), both accepted; confirm is always top-level. Query verbs take params in the URL (?key=value).",
                "Every /command, /query and /describe response carries an ambient 'context' {project:{id,name}, time_mode} — the scope it is implicitly against. Watch it: if the project changes under you, unexpected data means 'different project', not 'state reset'. Pin it with {\"expect_project\": \"<id-or-name>\"} on a command to get a 409 instead of acting in the wrong project.",
                "Consult a guidance playbook before a multi-step task for the recommended sequence.",
            ],
            "workflows": [
                "Live readout: query source.list -> command layout.add_panel {kind:'chart'} -> layout.route to show a source",
                "Annotate an event: tag.add {label, t?} marks the shared timeline",
                "Answer a device: poll device.prompts for open requests -> device.respond {id, answer} when a device needs an operator answer to proceed",
                "Run an experiment: record.start {label} -> drive the instruments -> record.stop (auto-exports a labelled bundle)",
                "Use another client's instrument: hub.connect -> device.remote_list -> device.add_remote {agent_id, instance_id}; its channels then appear in source.list",
                "Bring in external data: device.create a Python device that fetches/computes, then chart it like any source",
                "Get results out: export.csv for a time window (returns the CSV), or export.window for a full reimportable bundle",
            ],
            "notes": [
                "This server binds 127.0.0.1 only — a connector on another machine goes through the hub, not here.",
                "Everything is plain files on the operator's disk; changes are real and immediate — prefer 'read' scope while exploring.",
            ],
            "for_agents": (
                "This interface was shaped specifically for LLM / agent clients — to make "
                "driving ferroDAC straightforward and its state legible. ferroDAC is open "
                "source: if you need to understand something this API can't answer, clone the "
                "repo at https://github.com/TobiasHofmaenner/ferroDAC and read the source "
                "directly. And if something trips you up — a confusing or misleading response, "
                "an endpoint that isn't intuitive, a bug, a rough edge — you are warmly invited "
                "to open an issue at https://github.com/TobiasHofmaenner/ferroDAC/issues . "
                "Issues opened by LLMs are genuinely welcome (several already shaped this very "
                "API). If you don't have permission to file one, ask your user to open it."),
        })

    async def _pair(self, request: Request):
        body = await _json_body(request)
        name = str(body.get("name") or "connector")
        scope = str(body.get("scope") or "read")
        p = self._conns.request_pairing(name, scope)
        return JSONResponse({"pairing_id": p.id, "verification_code": p.code,
                             "status": p.status})

    async def _pair_poll(self, request: Request):
        p = self._conns.poll_pairing(request.path_params["pairing_id"])
        if p is None:
            return JSONResponse({"status": "unknown"}, status_code=404)
        out = {"status": p.status}
        if p.status == "approved":
            out["token"] = p.token         # one-shot — the client stores it now
        return JSONResponse(out)

    async def _describe(self, request: Request):
        conn = self._auth(request)
        if conn is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"app": self._name, "version": self._version,
                             "connector": conn.public(),
                             "context": self._surface.context(),
                             **self._surface.describe(conn.scope)})

    async def _command(self, request: Request):
        conn = self._auth(request)
        if conn is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        verb = request.path_params["verb"]
        body = await _json_body(request)
        expect = body.get("expect_project")
        if expect is not None:               # optional guard: fail if the frame isn't what the
            ctx = self._surface.context()    # client expects, instead of acting in the wrong place
            active = ctx.get("project") or {}
            if str(expect) not in (str(active.get("id")), str(active.get("name"))):
                return JSONResponse(
                    {"error": f"expect_project mismatch: active project is "
                              f"{active.get('name')!r}, expected {str(expect)!r}",
                     "context": ctx}, status_code=409)
        payload = _payload_of(body)
        confirm = bool(body.get("confirm", False))
        return await self._run_verb(conn, verb, payload, confirm)

    async def _query(self, request: Request):
        conn = self._auth(request)
        if conn is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        verb = request.path_params["verb"]
        payload = dict(request.query_params)
        return await self._run_verb(conn, verb, payload, False)

    async def _run_verb(self, conn, verb, payload, confirm):
        # the dispatch runs the handler (which may block on the GUI thread) → threadpool,
        # so the event loop stays free for other requests / the SSE stream.
        try:
            result = await run_in_threadpool(
                self._surface.dispatch, verb, payload,
                scope=conn.scope, confirm=confirm, caller=conn.name)
        except ScopeError as exc:
            self._audit(conn, verb, False, str(exc))
            return JSONResponse({"error": str(exc), "context": self._surface.context()},
                                status_code=403)
        except ControlError as exc:
            self._audit(conn, verb, False, str(exc))
            return JSONResponse({"error": str(exc), "context": self._surface.context()},
                                status_code=400)
        # Building the JSONResponse serializes the result (json.dumps, allow_nan=False).
        # A non-JSON-able / NaN result would otherwise raise here and 500 the whole
        # response — turn it into a clean 400 so a bad verb can never crash the server.
        # The ambient context (active project / time mode) rides on EVERY response so a
        # stateless client can detect a frame shift (issue #7); read AFTER dispatch so a
        # project-switching command reflects the new project.
        try:
            resp = JSONResponse({"context": self._surface.context(), "result": result})
        except (ValueError, TypeError) as exc:
            self._audit(conn, verb, False, f"unserializable result: {exc}")
            return JSONResponse(
                {"error": f"{verb} returned a non-JSON-serializable result: {exc}"},
                status_code=400)
        self._audit(conn, verb, True, "")
        return resp

    async def _events(self, request: Request):
        conn = self._auth(request)
        if conn is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        # surface.emit fires on arbitrary threads → hop onto THIS loop, thread-safely
        unsub = self._surface.subscribe(
            lambda ev: loop.call_soon_threadsafe(_offer, queue, ev))

        async def stream():
            try:
                yield b": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"       # keep the connection warm
                        continue
                    yield b"data: " + json.dumps(ev).encode("utf-8") + b"\n\n"
            finally:
                unsub()

        return StreamingResponse(stream(), media_type="text/event-stream")

    def _audit(self, conn, verb, ok, detail) -> None:
        if self._on_audit is not None:
            try:
                self._on_audit(conn.name, verb, ok, detail)
            except Exception:             # noqa: BLE001
                pass


async def _json_body(request: Request) -> dict:
    try:
        body = await request.body()
        return json.loads(body) if body else {}
    except (ValueError, TypeError):
        return {}


def _payload_of(body: dict) -> dict:
    """A command verb's params may be sent WRAPPED — {"params": {...}} or {"payload":
    {...}} — OR FLAT at the top level, which is what /describe's per-verb `params` naturally
    suggests. Accept both (an explicit envelope wins); otherwise the whole body IS the
    params, minus the reserved envelope keys. So `{"source_key": ...}` and
    `{"params": {"source_key": ...}}` are equivalent, and `confirm` stays top-level either
    way (issue #5)."""
    if not isinstance(body, dict):
        return {}
    if isinstance(body.get("payload"), dict):
        return body["payload"]
    if isinstance(body.get("params"), dict):
        return body["params"]
    return {k: v for k, v in body.items()
            if k not in ("payload", "params", "confirm", "expect_project")}


def _offer(queue: asyncio.Queue, item) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass                              # a slow SSE client drops events, never blocks
