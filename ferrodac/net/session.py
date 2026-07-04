"""ReconnectingClient — the reconnect/back-off/teardown FSM every hub channel
controller shares (DESIGN §12.1).

The audit found this loop copy-pasted FIVE times (agent, viewer, tags, projects,
docs), its semantics already diverging. It is now here, once: the base owns the
worker thread, the event loop, the channel lifecycle, the 2 s back-off, the
"a CancelledError is a disconnect (reconnect), not a shutdown (only stop() is)"
rule, and clean loop teardown. A subclass implements ``_run_session(channel)``
with just its per-concern stream logic (the part that actually differs), sets
``_thread_name`` + ``_disconnect_label``, and optionally overrides
``_on_loop_created`` (to build an out-queue) / ``_do_stop`` (to unblock it).

Qt-free (plain threading + grpc.aio), like the rest of net/.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import grpc

from . import GRPC_CHANNEL_OPTIONS, _drain, call_soon_safe

log = logging.getLogger("hub.session")


class ReconnectingClient:
    _thread_name = "hub-client"        # the worker thread's name
    _disconnect_label = "hub"          # prefix for the "<label> disconnected" notice

    def __init__(self, addr: str, on_state=None):
        self._addr = addr
        self._on_state = on_state      # callback(connected: bool, detail: str)
        self._thread: "threading.Thread | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._stop: "asyncio.Event | None" = None

    # -- lifecycle (identical for every channel) ----------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=self._thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        call_soon_safe(self._loop, self._do_stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _do_stop(self) -> None:
        """Signal the loop to exit. Override to ALSO unblock a blocked out-queue
        generator (agent/docs), then call super()._do_stop()."""
        if self._stop is not None:
            self._stop.set()

    def _notify(self, connected: bool, detail: str) -> None:
        log.info("%s", detail)
        if self._on_state is not None:
            try:
                self._on_state(connected, detail)
            except Exception:          # a bad observer must never break the loop
                pass

    # -- worker thread ------------------------------------------------------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._on_loop_created(loop)    # hook: build per-loop state (e.g. an outq)
        self._stop = asyncio.Event()
        self._loop = loop              # set LAST → senders see a ready loop/queue
        try:
            loop.run_until_complete(self._loop_forever())
        finally:
            _drain(loop)
            loop.close()

    def _on_loop_created(self, loop) -> None:
        """Hook: create per-loop state (e.g. the out-queue) before the loop runs."""

    async def _loop_forever(self) -> None:
        while not self._stop.is_set():
            try:
                async with grpc.aio.insecure_channel(
                        self._addr, options=GRPC_CHANNEL_OPTIONS) as ch:
                    await self._run_session(ch)
            except asyncio.CancelledError:
                # grpc.aio also surfaces a locally-cancelled CALL this way (the hub
                # vanishing mid-write). Only OUR stop() is a shutdown; anything else
                # is a disconnect → reconnect. (Treating every CancelledError as a
                # shutdown once left the agent silently dead after a hub restart.)
                if self._stop.is_set():
                    break
                self._notify(
                    False, f"{self._disconnect_label} disconnected: stream cancelled")
            except Exception as e:             # hub down / connection lost
                self._notify(False, f"{self._disconnect_label} disconnected: {e}")
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass                           # back off, then reconnect

    async def _run_session(self, channel) -> None:
        """One connected session: the per-concern stream logic. Returns when the
        session ends (naturally or on stop). Subclass MUST implement."""
        raise NotImplementedError
