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

_EPS = 1e-6          # time tolerance for "extends back" / "advanced" comparisons


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
        self.nav: int = 0                # bumps on navigation (scrub/tail-drag) only —
        #                                  NOT on pause/play/go-live, so transport never
        #                                  triggers a reload
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
        # a head jump (scrub/step/calendar) is discontinuous navigation: stop
        # live-follow AND playback so the controller reloads cleanly at the new
        # spot. The head can never be in the future — clamp to now.
        self.following = self.playing = False
        self.head = min(float(head), self._now())
        self.nav += 1
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
        """Drag the back edge: move the anchor (grow) or set the width (slide).
        This is navigation (may extend back into unloaded data)."""
        if self.grow:
            self.anchor = min(float(t0), self.head)
        else:
            self.width = max(1e-3, self.head - float(t0))
        self.nav += 1
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

    def stream(self, sources, t0, t1, on_progress=None) -> int:
        """Read full-res raw for `sources` over [t0,t1], merge by time, and emit
        through the bus in time-ordered chunks. Returns the number of readings
        emitted. `on_progress(frac)` (0..1) reports the load: the read phase is
        the first half, the emit phase the second. (Window-bounded; the read is
        synchronous for now — a worker thread is the later optimisation for big
        slices.)"""
        srcs = list(sources)
        rows: list = []
        for i, sid in enumerate(srcs):
            dev, _, src = sid.rpartition("/")        # key 'device/source' → Reading
            if self._is_trace(sid):                  # 2-D scans → Trace readings
                for times, Y, x in self.store.read_raw_trace(sid, t0, t1):
                    rows.extend(
                        (float(times[i]),
                         Reading(dev, src, float(times[i]), Trace(x=x, y=Y[i])))
                        for i in range(len(times)))
            else:
                t, v = self.store.read_raw(sid, t0, t1)  # full-res scalars
                if len(t):
                    rows.extend((float(t[i]), Reading(dev, src, float(t[i]), float(v[i])))
                                for i in range(len(t)))
            if on_progress:
                on_progress(0.5 * (i + 1) / max(1, len(srcs)))   # read = first half
        if not rows:
            if on_progress:
                on_progress(1.0)
            return 0
        rows.sort(key=lambda r: r[0])                # global time order
        total, n, batch = len(rows), 0, []
        for _, rd in rows:
            batch.append(rd)
            if len(batch) >= self.chunk:
                n += self._emit(batch)
                batch = []
                if on_progress:
                    on_progress(0.5 + 0.5 * n / total)           # emit = second half
        if batch:
            n += self._emit(batch)
        if on_progress:
            on_progress(1.0)
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

    def __init__(self, engine, store, time_context, sources=None, on_reset=None,
                 on_progress=None):
        self.store = store
        self.tc = time_context
        self.bus = Bus()                             # what the dashboard subscribes to
        self.playback = PlaybackSource(store, self.bus)
        self._sources = sources or store.sources     # callable → [source keys]
        self.on_reset = on_reset
        self.on_progress = on_progress               # frac 0..1 during a load; None=done
        self._was_following = time_context.following
        self._loaded = None                          # (lo,hi) time span now in the panels
        self._last_nav = time_context.nav            # to detect navigation vs transport
        self._busy = False                           # re-entrancy guard (processEvents)
        self._live_unsub = engine.subscribe(self._on_live)
        self._ctx_unsub = time_context.subscribe(self._on_context)

    def _on_live(self, batch) -> None:
        if self.tc.following:                        # live → straight to the playback bus
            for r in batch:
                self.bus.publish(r)
            self.bus.drain()
            if batch:                                # track what the live panels now hold
                lo = min(r.t for r in batch)
                hi = max(r.t for r in batch)
                self._loaded = ((lo, hi) if self._loaded is None
                                else (min(self._loaded[0], lo), max(self._loaded[1], hi)))

    def _on_context(self) -> None:
        if self._busy:                               # a load is in flight (processEvents
            return                                   # may re-enter) — ignore until done
        t0, t1 = self.tc.window
        nav = self.tc.nav                            # navigation (scrub/tail-drag) vs
        navigated = nav != self._last_nav            # transport (pause/play/go-live)
        self._last_nav = nav
        if self.tc.following:
            # going/being live never LOADS history — at most catch the front up to now;
            # the live pass-through then keeps appending. (Pausing then playing here is
            # free: the panels already hold the data.)
            if not self._was_following:
                if self._loaded is None:
                    self._reset_and_load(t0, t1)
                elif t1 > self._loaded[1] + _EPS:
                    self.playback.stream(list(self._sources()), self._loaded[1], t1)
                    self._loaded = (self._loaded[0], t1)
            self._was_following = True
            return
        self._was_following = False
        if self._loaded is None:                     # first historic view
            self._reset_and_load(t0, t1)
            return
        lo, hi = self._loaded
        if navigated:                                # scrub head / drag tail
            if t0 < lo - _EPS or t0 > hi + _EPS:     # needs earlier data, or disjoint jump
                self._reset_and_load(t0, t1)         # → clear + full in-order re-stream
            elif t1 > hi + _EPS:                     # head moved forward into unloaded
                self.playback.stream(list(self._sources()), hi, t1)
                self._loaded = (lo, t1)
            # else: window ⊆ loaded (shorten / nudge) → no load
        elif self.tc.playing and t1 > hi + _EPS:     # play/slide forward (transport)
            self.playback.stream(list(self._sources()), hi, t1)   # cheap front sliver
            self._loaded = (lo, t1)
        # else: pause / transport with nothing new → no load, no reload

    def _reset_and_load(self, t0, t1) -> None:
        if self.on_reset:
            self.on_reset()                          # clear stale data (panels re-fit)
        self._load(t0, t1)
        self._loaded = (t0, t1)

    def _load(self, t0, t1) -> None:
        """Full-res re-stream of [t0,t1] with a progress callback + a re-entrancy
        guard (the UI's progress handler may pump the event loop)."""
        self._busy = True
        try:
            self.playback.stream(list(self._sources()), t0, t1,
                                 on_progress=self.on_progress)
        finally:
            self._busy = False
            if self.on_progress:
                self.on_progress(None)               # done → hide the indicator

    def stop(self) -> None:
        self._live_unsub()
        self._ctx_unsub()
