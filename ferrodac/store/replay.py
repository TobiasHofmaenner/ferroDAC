"""Replay engine — re-experience history through the live pipeline (DESIGN §7.4).

One head-driven model: **live is just the head at now.** A `TimeContext` holds
the head (following-now | parked) + transport state and notifies observers (the
UI bridges it to Qt). A `PlaybackSource` reads **full-resolution** raw from the
store (never downsampled — analysis input) and re-streams it, **in time order
and chunked**, through a `Bus` into the same processors/sinks the live engine
feeds — so the whole analysis pipeline re-experiences the old data, in its own
context (not the live inbox). Qt-free.
"""

from __future__ import annotations

import time as _time

from ..core.bus import Bus
from ..core.reading import Reading
from ..core.trace import Trace


class TimeContext:
    """The app's single time control: a head that either follows now (live) or is
    parked in the past, plus a window width. Qt-free observer (UI bridges to Qt)."""

    def __init__(self, width: float = 600.0, now_fn=None):
        self._now = now_fn or _time.time
        self.head: float = self._now()
        self.width: float = width
        self.following: bool = True
        self.playing: bool = False
        self.speed: float = 1.0
        self.rate: float = 1.0           # achieved playback rate (set by the driver)
        self.grow: bool = False          # play/follow: grow from an anchor vs slide
        self.anchor: float | None = None # pinned back edge while growing
        self._subs: list = []

    @property
    def window(self):
        if self.grow and self.anchor is not None:    # anchored back, growing front
            return (min(self.anchor, self.head), self.head)
        return (self.head - self.width, self.head)   # fixed-width sliding

    def subscribe(self, cb):
        self._subs.append(cb)
        return lambda: self._subs.remove(cb) if cb in self._subs else None

    def _notify(self):
        for cb in list(self._subs):
            try:
                cb()
            except Exception:
                pass

    # -- transport -----------------------------------------------------------
    def follow_now(self):
        # catching up to now settles into live at realtime (no overshoot past now)
        self.following, self.playing, self.head = True, False, self._now()
        self.speed = 1.0
        self._notify()

    def park(self, head: float):
        # a head jump (scrub/step/calendar) is discontinuous: stop live-follow AND
        # playback so the controller does a clean reload at the new spot (no stale
        # incremental). The head can never be in the future — clamp to now.
        self.following = self.playing = False
        self.head = min(float(head), self._now())
        self._notify()

    @property
    def moving(self) -> bool:
        """The head is advancing — live (following at 1x) or replaying (playing).
        The transport's play/pause reflects this; ● Now implies it."""
        return self.following or self.playing

    def pause(self):
        """Freeze the head where it is (stop both live-follow and replay)."""
        self.following = self.playing = False
        self._notify()

    def play(self):
        """Resume motion: live if we're at the live edge, else replay forward."""
        if self.head >= self._now() - 1.0:
            self.follow_now()
        else:
            self.playing = True
            self._notify()

    def set_width(self, width: float):
        self.width = max(1e-3, float(width))
        self._notify()

    def set_grow(self, grow: bool):
        """Toggle play/follow mode: grow from a pinned anchor vs slide a fixed
        width. Entering grow pins the current back edge; leaving grow keeps the
        current window size as the new fixed width (so it doesn't jump)."""
        grow = bool(grow)
        if grow and not self.grow:
            self.anchor = self.head - self.width
        elif self.grow and not grow and self.anchor is not None:
            self.width = max(1e-3, self.head - self.anchor)
        self.grow = grow
        self._notify()

    def resize_back(self, t0: float):
        """Drag the back edge: move the anchor (grow) or set the width (slide)."""
        if self.grow:
            self.anchor = min(float(t0), self.head)
        else:
            self.width = max(1e-3, self.head - float(t0))
        self._notify()

    def tick_live(self):
        """Advance the head to now while following (the live case)."""
        if self.following:
            self.head = self._now()
            self._notify()

    def tick_play(self, dt_wall: float):
        """Advance the parked head by speed×dt; lock to live when it catches now."""
        if not self.playing:
            return
        self.head += self.speed * dt_wall
        if self.head >= self._now():
            self.follow_now()
        else:
            self._notify()


class PlaybackSource:
    """Streams the full-resolution raw of a window through a Bus, in time order
    and in chunks, so subscribed processors/sinks re-experience it."""

    def __init__(self, store, bus, chunk: int = 20000):
        self.store = store
        self.bus = bus
        self.chunk = chunk

    def stream(self, sources, t0, t1) -> int:
        """Read full-res raw for `sources` over [t0,t1], merge by time, and emit
        through the bus in time-ordered chunks. Returns the number of readings
        emitted. (Window-bounded; vectorised batches are a later optimisation.)"""
        rows: list = []
        for sid in sources:
            dev, _, src = sid.rpartition("/")        # key 'device/source' → Reading
            if self._is_trace(sid):                  # 2-D scans → Trace readings
                for times, Y, x in self.store.read_raw_trace(sid, t0, t1):
                    rows.extend(
                        (float(times[i]),
                         Reading(dev, src, float(times[i]), Trace(x=x, y=Y[i])))
                        for i in range(len(times)))
                continue
            t, v = self.store.read_raw(sid, t0, t1)  # full-res scalars
            if not len(t):
                continue
            rows.extend((float(t[i]), Reading(dev, src, float(t[i]), float(v[i])))
                        for i in range(len(t)))
        if not rows:
            return 0
        rows.sort(key=lambda r: r[0])                # global time order
        n, batch = 0, []
        for _, rd in rows:
            batch.append(rd)
            if len(batch) >= self.chunk:
                n += self._emit(batch)
                batch = []
        if batch:
            n += self._emit(batch)
        return n

    def _is_trace(self, sid) -> bool:
        sd = getattr(self.store, "source_dtype", None)
        return sd(sid) == "trace" if sd else False

    def _emit(self, batch) -> int:
        for r in batch:
            self.bus.publish(r)
        self.bus.drain()                             # fan the chunk to subscribers
        return len(batch)


class ReplayController:
    """The L3 spine: one **playback Bus** the whole app subscribes to, fed either
    by the live engine (following now) or by re-streaming the historic slice
    (parked). "Live is just the head at now." Driven by a shared `TimeContext`;
    calls `on_reset` when the view jumps (so consumers clear stale data).

    Source selection is a callable (the routed sources, from the dataflow graph).
    Qt-free; the engine it subscribes to may be the Qt Engine — only `subscribe`
    is used. Replay runs synchronously on park for now (off-thread is a later
    optimisation, signalled by the realtime-rate readout)."""

    def __init__(self, engine, store, time_context, sources=None, on_reset=None):
        self.store = store
        self.tc = time_context
        self.bus = Bus()                             # what the dashboard subscribes to
        self.playback = PlaybackSource(store, self.bus)
        self._sources = sources or store.sources     # callable → [source keys]
        self.on_reset = on_reset
        self._was_following = time_context.following
        self._played_to = None
        self._live_unsub = engine.subscribe(self._on_live)
        self._ctx_unsub = time_context.subscribe(self._on_context)

    def _on_live(self, batch) -> None:
        if self.tc.following:                        # live → straight to the playback bus
            for r in batch:
                self.bus.publish(r)
            self.bus.drain()

    def _on_context(self) -> None:
        if self.tc.following:
            if not self._was_following:
                if self.on_reset:
                    self.on_reset()                  # returned to live → clear historic
                t0, t1 = self.tc.window              # re-seed the recent window so the
                self.playback.stream(list(self._sources()), t0, t1)  # chart isn't empty
            self._was_following = True
            self._played_to = None
            return
        t0, t1 = self.tc.window
        cont = (self.tc.playing and self._played_to is not None
                and t0 <= self._played_to <= t1)     # smooth play advance only
        if cont:
            # advancing playhead → stream only the newly-revealed range
            self.playback.stream(list(self._sources()), self._played_to, t1)
        else:
            if self.on_reset:                        # fresh park / scrub → clear + full replay
                self.on_reset()
            self.playback.stream(list(self._sources()), t0, t1)
        self._played_to = t1
        self._was_following = False

    def stop(self) -> None:
        self._live_unsub()
        self._ctx_unsub()
