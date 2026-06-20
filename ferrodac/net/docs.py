"""HubDocSync — relay one client's collaborative edits to/from a hub Docs.Session.

Bidirectional like HubAgent's egress Session, but TWO-WAY: opaque Yjs update bytes
+ awareness + presence flow both ways over a single stream that multiplexes every
doc room this client joined. The CRDT lives in the editor (JS); this layer is a
DUMB pipe and never parses the bytes — it only base64-codes them at the Qt/JS seam
(gRPC carries raw bytes; QWebChannel carries strings). Role-independent like tags/
projects. Re-joins every open doc on (re)connect so the hub replays state and the
session self-heals. grpc.aio in its own daemon thread; Qt-free.

Callbacks fire on the WORKER thread (like HubAgent's on_state) — the Qt glue
(hubclient.HubController) marshals them to the GUI thread via forced
QueuedConnection signals.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import CONTRACT_VERSION, _drain

log = logging.getLogger("hub.docs")


class HubDocSync:
    def __init__(self, addr: str, agent_id: str = "ferrodac",
                 on_seed=None, on_update=None, on_awareness=None,
                 on_presence=None, on_state=None):
        self._addr = addr
        self._agent_id = agent_id
        self._on_seed = on_seed              # (doc_id, should_seed: bool, text: str)
        self._on_update = on_update          # (doc_id, update_b64: str)
        self._on_awareness = on_awareness    # (doc_id, state_b64: str)
        self._on_presence = on_presence      # (doc_id, actors: list[str])
        self._on_state = on_state            # (connected: bool, detail: str)
        self._thread: "threading.Thread | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._outq: "asyncio.Queue | None" = None
        self._stop: "asyncio.Event | None" = None
        self._lock = threading.Lock()
        self._joined: dict = {}              # doc_id -> (actor, color), replayed on reconnect

    # -- lifecycle (mirror HubAgent) ----------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hub-docs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._do_stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _do_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._outq is not None:
            self._outq.put_nowait(None)      # unblock the out generator

    # -- public API (any thread) — base64 in/out at the Qt/JS seam ----------
    def join(self, doc_id: str, actor: str = "", color: str = "") -> None:
        with self._lock:
            self._joined[doc_id] = (actor, color)
        self._send(pb.DocClientMsg(join=pb.DocJoin(
            doc_id=doc_id, actor=actor, color=color)))

    def leave(self, doc_id: str) -> None:
        with self._lock:
            self._joined.pop(doc_id, None)
        self._send(pb.DocClientMsg(leave=pb.DocLeave(doc_id=doc_id)))

    def send_update(self, doc_id: str, update_b64: str, compaction: bool = False) -> None:
        self._send(pb.DocClientMsg(update=pb.DocUpdate(
            doc_id=doc_id, update=base64.b64decode(update_b64), compaction=compaction)))

    def send_awareness(self, doc_id: str, state_b64: str) -> None:
        self._send(pb.DocClientMsg(awareness=pb.DocAwareness(
            doc_id=doc_id, state=base64.b64decode(state_b64))))

    def send_snapshot(self, doc_id: str, text: str) -> None:
        self._send(pb.DocClientMsg(snapshot=pb.DocSnapshot(doc_id=doc_id, text=text)))

    # -- internals -----------------------------------------------------------
    def _send(self, msg) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(self._safe_put, msg)

    def _safe_put(self, msg) -> None:
        try:
            self._outq.put_nowait(msg)
        except Exception:
            pass

    def _notify(self, connected: bool, detail: str) -> None:
        log.info("%s", detail)
        if self._on_state is not None:
            try:
                self._on_state(connected, detail)
            except Exception:
                pass

    def _dispatch(self, msg) -> None:
        """Hand an incoming DocServerMsg to the right callback (worker thread)."""
        which = msg.WhichOneof("msg")
        try:
            if which == "seed" and self._on_seed is not None:
                self._on_seed(msg.seed.doc_id, msg.seed.should_seed, msg.seed.text)
            elif which == "update" and self._on_update is not None:
                self._on_update(msg.update.doc_id,
                                base64.b64encode(msg.update.update).decode("ascii"))
            elif which == "awareness" and self._on_awareness is not None:
                self._on_awareness(
                    msg.awareness.doc_id,
                    base64.b64encode(msg.awareness.state).decode("ascii"))
            elif which == "presence" and self._on_presence is not None:
                self._on_presence(msg.presence.doc_id, list(msg.presence.actors))
        except Exception:                    # a callback must never kill the stream
            log.exception("doc callback failed")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._outq = asyncio.Queue()
        self._stop = asyncio.Event()
        self._loop = loop                    # set last → _send sees a ready queue
        try:
            loop.run_until_complete(self._session_loop())
        finally:
            _drain(loop)
            loop.close()

    async def _session_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with grpc.aio.insecure_channel(self._addr) as ch:
                    stub = rpc.DocsStub(ch)
                    call = stub.Session(self._outgen())
                    async for msg in call:
                        self._dispatch(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:           # hub down / connection lost
                self._notify(False, f"doc sync disconnected: {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass                         # backoff, then reconnect

    async def _outgen(self):
        yield pb.DocClientMsg(hello=pb.Hello(
            agent_id=self._agent_id, contract_version=CONTRACT_VERSION))
        with self._lock:
            joined = list(self._joined.items())
        for doc_id, (actor, color) in joined:    # re-join every open doc on (re)connect
            yield pb.DocClientMsg(join=pb.DocJoin(
                doc_id=doc_id, actor=actor, color=color))
        self._notify(True, f"doc sync connected to {self._addr}")
        while True:
            msg = await self._outq.get()
            if msg is None or self._stop.is_set():
                break
            yield msg
