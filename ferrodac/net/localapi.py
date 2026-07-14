"""LocalApiServer — the loopback HTTP door to the ControlSurface (external connectors).

Binds 127.0.0.1 ONLY (a connector on another machine goes through the hub, not this
server — no local TLS to manage). Starlette ASGI app on uvicorn in a background thread
with its own asyncio loop; the blocking command dispatch (which may marshal to the GUI
thread) runs in a threadpool so it never stalls the loop. Auth is a per-connector bearer
token (ConnectorRegistry). Qt-free — the ControlSurface's GUI handlers do the marshalling.

  POST /pair {name, scope}                 -> {pairing_id, verification_code}   (no auth)
  GET  /pair/{id}                          -> {status, token?}          (no auth; one-shot)
  GET  /describe                           -> scope-filtered verb catalog        (bearer)
  POST /command/{verb} {payload?, confirm?}-> {result} | error                  (bearer)
  GET  /query/{verb}                       -> {result}   (params via ?a=b)       (bearer)
  GET  /events                             -> SSE state-change stream            (bearer)
  GET  /health                             -> {ok, name, version}               (no auth)

A discovery file (~/.config/ferrodac/connector.json, {port, pid}) lets a same-user
client find the port.
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
            time.sleep(0.01)
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
                             **self._surface.describe(conn.scope)})

    async def _command(self, request: Request):
        conn = self._auth(request)
        if conn is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        verb = request.path_params["verb"]
        body = await _json_body(request)
        payload = body.get("payload", body.get("params", {})) or {}
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
                scope=conn.scope, confirm=confirm)
            self._audit(conn, verb, True, "")
            return JSONResponse({"result": result})
        except ScopeError as exc:
            self._audit(conn, verb, False, str(exc))
            return JSONResponse({"error": str(exc)}, status_code=403)
        except ControlError as exc:
            self._audit(conn, verb, False, str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)

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


def _offer(queue: asyncio.Queue, item) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass                              # a slow SSE client drops events, never blocks
