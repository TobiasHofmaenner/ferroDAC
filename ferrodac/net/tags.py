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

import grpc
from ferrodac_contract.v1 import data_plane_pb2 as pb
from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

from . import _drain, convert

log = logging.getLogger("hub.tags")


class HubTagSync:
    def __init__(self, addr: str, agent_id: str = "ferrodac",
                 on_tag=None, on_state=None):
        self._addr = addr
        self._agent_id = agent_id
        self._on_tag = on_tag                  # (Marker) — an incoming tag/tombstone
        self._on_state = on_state
        self._thread: "threading.Thread | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop: "asyncio.Event | None" = None
        self._stub = None                      # set on the loop thread when connected
        self._lock = threading.Lock()
        self._pending: dict = {}               # id -> pb.Tag (replayed on reconnect)

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="hub-tags", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and self._stop is not None:
            loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

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

    def _notify(self, connected: bool, detail: str) -> None:
        log.info("%s", detail)
        if self._on_state is not None:
            try:
                self._on_state(connected, detail)
            except Exception:
                pass

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._stop = asyncio.Event()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        finally:
            _drain(loop)
            loop.close()

    async def _main(self) -> None:
        while not self._stop.is_set():
            try:
                async with grpc.aio.insecure_channel(self._addr) as ch:
                    self._stub = rpc.TagsStub(ch)
                    self._notify(True, f"tag sync connected to {self._addr}")
                    await self._replay()       # re-assert local tags on (re)connect
                    watch = asyncio.create_task(self._watch())
                    stopper = asyncio.create_task(self._stop.wait())
                    await asyncio.wait({watch, stopper},
                                       return_when=asyncio.FIRST_COMPLETED)
                    for t in (watch, stopper):
                        t.cancel()
                    await asyncio.gather(watch, stopper, return_exceptions=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._notify(False, f"tag sync disconnected: {e}")
            finally:
                self._stub = None
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass                           # backoff, then reconnect

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
