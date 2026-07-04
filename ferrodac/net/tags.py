"""HubTagSync — mirror the local TagStore to a hub's tag channel (DESIGN §7.3).

Role-independent: runs whenever connected to a hub, in either agent or viewer
mode (a pure viewer must be able to create tags too). Watches the hub's
``WatchTags`` stream and hands incoming tags to a callback (the Qt glue upserts
them into the local TagStore); publishes local creates/edits/deletes back up.

Reliability: every local tag is held in a pending set and **re-published on every
(re)connect**, so a tag created while the hub was down still converges — the same
self-healing trick the device agent uses for announcements. The hub fans our own
writes back to us, but the TagStore merges idempotently by version, so there is
no echo loop. Runs grpc.aio in its own thread; callbacks fire on that thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import convert
from .session import ReconnectingClient

log = logging.getLogger("hub.tags")


class HubTagSync(ReconnectingClient):
    _thread_name = "hub-tags"
    _disconnect_label = "tag sync"

    def __init__(self, addr: str, agent_id: str = "ferrodac",
                 on_tag=None, on_state=None):
        super().__init__(addr, on_state)       # thread / loop / stop / reconnect FSM
        self._agent_id = agent_id
        self._on_tag = on_tag                  # (Marker) — an incoming tag/tombstone
        self._stub = None                      # set on the loop thread when connected
        self._lock = threading.Lock()
        self._pending: dict = {}               # id -> pb.Tag (replayed on reconnect)

    # -- public API (any thread) --------------------------------------------
    def publish(self, marker) -> None:
        """Publish a local tag (or, if ``marker.deleted``, a tombstone). Held
        for replay on reconnect; sent now if connected."""
        pb_tag = convert.tag_to_proto(marker)
        with self._lock:
            self._pending[pb_tag.id] = pb_tag
        self._schedule(self._send_one(pb_tag))

    # -- internals -----------------------------------------------------------
    def _schedule(self, coro) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(lambda: loop.create_task(coro))
        else:
            coro.close()                       # not running yet — replayed later

    async def _send_one(self, pb_tag) -> None:
        stub = self._stub
        if stub is None:
            return                             # offline — reconnect replays it
        try:
            if pb_tag.deleted:
                await stub.DeleteTag(pb.DeleteTagRequest(
                    id=pb_tag.id, version=pb_tag.version,
                    origin_id=pb_tag.origin_id))
            else:
                await stub.PublishTag(pb.PublishTagRequest(tag=pb_tag))
        except Exception:                      # transient — replayed on reconnect
            pass

    async def _run_session(self, ch) -> None:
        self._stub = rpc.TagsStub(ch)
        self._notify(True, f"tag sync connected to {self._addr}")
        try:
            await self._replay()               # re-assert local tags on (re)connect
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
        for pb_tag in pending:                 # uniform replay (tombstones too)
            try:
                await self._stub.PublishTag(pb.PublishTagRequest(tag=pb_tag))
            except Exception:
                return

    async def _watch(self) -> None:
        async for ev in self._stub.WatchTags(pb.WatchTagsRequest()):
            if self._on_tag is not None:
                self._on_tag(convert.tag_from_proto(ev.tag))
