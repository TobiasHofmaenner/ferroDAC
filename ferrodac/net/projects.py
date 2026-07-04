"""HubProjectSync — mirror local hub-project edits to a hub's Projects channel.

The exact same shape as HubTagSync (DESIGN §8.1): role-independent, watches the
hub's ``WatchProjects`` stream and hands incoming records to a callback (the Qt
glue materialises them into the client's ProjectManager cache); publishes local
creates/edits/deletes back up. Every published record is held in a pending set
and **re-published on every (re)connect**, so an edit made while the hub was down
still converges. The hub fans our own writes back to us, but the client merges
LWW by version, so there's no echo loop. grpc.aio in its own thread.

Unlike tags, projects are OPT-IN: only records the app explicitly publishes (an
"on hub" create, an edit to a hub project, a share-to-hub) ever flow here — the
app never auto-publishes its local projects.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import convert
from .session import ReconnectingClient

log = logging.getLogger("hub.projects")


class HubProjectSync(ReconnectingClient):
    _thread_name = "hub-projects"
    _disconnect_label = "project sync"

    def __init__(self, addr: str, agent_id: str = "ferrodac",
                 on_project=None, on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._agent_id = agent_id
        self._on_project = on_project          # (record dict) — an incoming project
        self._stub = None
        self._lock = threading.Lock()
        self._pending: dict = {}               # id -> pb.Project (replayed on reconnect)

    # -- public API (any thread) --------------------------------------------
    def publish(self, rec: dict) -> None:
        """Publish a project record (or, if ``rec['deleted']``, a tombstone). Held
        for replay on reconnect; sent now if connected."""
        pb_proj = convert.project_to_proto(rec)
        with self._lock:
            self._pending[pb_proj.id] = pb_proj
        self._schedule(self._send_one(pb_proj))

    # -- internals -----------------------------------------------------------
    def _schedule(self, coro) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: loop.create_task(coro))
        else:
            coro.close()                       # not running yet — replayed later

    async def _send_one(self, pb_proj) -> None:
        stub = self._stub
        if stub is None:
            return                             # offline — reconnect replays it
        try:
            if pb_proj.deleted:
                await stub.DeleteProject(pb.DeleteProjectRequest(
                    id=pb_proj.id, version=pb_proj.version,
                    origin_id=pb_proj.origin_id))
            else:
                await stub.PublishProject(pb.PublishProjectRequest(project=pb_proj))
        except Exception:                      # transient — replayed on reconnect
            pass

    async def _run_session(self, ch) -> None:
        self._stub = rpc.ProjectsStub(ch)
        self._notify(True, f"project sync connected to {self._addr}")
        try:
            await self._replay()               # re-assert local edits on (re)connect
            watch = asyncio.create_task(self._watch())
            stopper = asyncio.create_task(self._stop.wait())
            await asyncio.wait({watch, stopper}, return_when=asyncio.FIRST_COMPLETED)
            for t in (watch, stopper):
                t.cancel()
            await asyncio.gather(watch, stopper, return_exceptions=True)
        finally:
            self._stub = None

    async def _replay(self) -> None:
        with self._lock:
            pending = list(self._pending.values())
        for pb_proj in pending:
            try:
                await self._stub.PublishProject(
                    pb.PublishProjectRequest(project=pb_proj))
            except Exception:
                return

    async def _watch(self) -> None:
        async for ev in self._stub.WatchProjects(pb.WatchProjectsRequest()):
            if self._on_project is not None:
                self._on_project(convert.project_from_proto(ev.project))
