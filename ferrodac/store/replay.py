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

import enum
import time as _time

from ..core.bus import Bus
from ..core.reading import Reading
from ..core.trace import Trace


class Mode(enum.Enum):
    """The display plane's named state (DESIGN §22 I-8) — derived from the
    transport in exactly ONE place, `TimeContext.mode`, never reconstructed
    from boolean combinations at call sites."""
    LIVE = "live"          # head follows now; the feed owns every curve
    PARKED = "parked"      # head fixed; the window query owns stored scalar curves
    PLAYING = "playing"    # head walks forward; the feed owns every curve again


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

    @property
    def mode(self) -> Mode:
        """The named display state (DESIGN §22 I-8). The one derivation."""
        if self.following:
            return Mode.LIVE
        return Mode.PLAYING if self.playing else Mode.PARKED

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

    def park_window(self, t0: float, t1: float):
        """Jump the window to cover exactly [t0, t1] (back..front) and stop motion.
        Like `park`, this is discontinuous navigation (bumps nav) so the controller
        re-streams that slice — used to land on a recording and actually pull its
        data in, not just pan an empty view there. Front edge is clamped to now."""
        t0, t1 = float(t0), float(t1)
        if t1 < t0:
            t0, t1 = t1, t0
        self.following = self.playing = False
        self.head = min(t1, self._now())
        back = min(t0, self.head)
        if self.grow:
            self.anchor = back
        else:
            self.width = max(1e-3, self.head - back)
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

    def read_window(self, sources, t0, t1, on_progress=None, should_stop=None) -> list:
        """Read full-res raw for `sources` over [t0,t1] and return the Readings in
        global time order — the READ half of `stream`, with no emit. Also used to
        backfill a freshly-routed source into one panel (Dashboard route) without
        touching the shared bus. `on_progress` covers the read as its first half."""
        srcs = list(sources)
        rows: list = []
        for i, sid in enumerate(srcs):
            if should_stop is not None and should_stop():
                return []
            dev, _, src = sid.rpartition("/")        # key 'device/source' → Reading
            if self._is_trace(sid):                  # 2-D scans → Trace readings
                for times, Y, x in self.store.read_raw_trace(sid, t0, t1):
                    rows.extend(
                        (float(times[j]),
                         Reading(dev, src, float(times[j]), Trace(x=x, y=Y[j])))
                        for j in range(len(times)))
            else:
                t, v = self.store.read_raw(sid, t0, t1)  # full-res scalars
                if len(t):
                    rows.extend((float(t[j]), Reading(dev, src, float(t[j]), float(v[j])))
                                for j in range(len(t)))
            if on_progress:
                on_progress(0.5 * (i + 1) / max(1, len(srcs)))   # read = first half
        rows.sort(key=lambda r: r[0])                # global time order
        return [rd for _, rd in rows]

    def stream(self, sources, t0, t1, on_progress=None, should_stop=None,
               pump=None) -> int:
        """Read full-res raw for `sources` over [t0,t1], merge by time, and emit
        through the bus in time-ordered chunks. Returns the number of readings
        emitted. `on_progress(frac)` (0..1) reports the load: the read phase is
        the first half, the emit phase the second.

        `should_stop()` is polled between sources and chunks so a superseded
        re-stream (park twice, or go-live mid-load) bails out promptly.

        `pump(batch)` renders a published chunk: in the live app it marshals the
        bus drain onto the GUI thread and blocks until it repaints (so this can
        run on a worker thread — DESIGN §21.3 — while panels stay GUI-only). When
        None (headless/tests/server) it falls back to the synchronous in-caller
        drain, so behaviour is byte-identical to before."""
        rows = self.read_window(sources, t0, t1, on_progress=on_progress,
                                should_stop=should_stop)
        if not rows:
            if on_progress:
                on_progress(1.0)
            return 0
        total, n, batch = len(rows), 0, []
        for rd in rows:
            batch.append(rd)
            if len(batch) >= self.chunk:
                if should_stop is not None and should_stop():
                    return n
                n += self._emit(batch, pump)
                batch = []
                if on_progress:
                    on_progress(0.5 + 0.5 * n / total)           # emit = second half
        if batch:
            n += self._emit(batch, pump)
        if on_progress:
            on_progress(1.0)
        return n

    def _is_trace(self, sid) -> bool:
        sd = getattr(self.store, "source_dtype", None)
        return sd(sid) == "trace" if sd else False

    def _emit(self, batch, pump=None) -> int:
        for r in batch:
            self.bus.publish(r)
        if pump is not None:
            pump(batch)                              # GUI-marshalled drain (worker)
        else:
            while self.bus.drain():                  # fan the chunk + flush any derived
                pass                                 # a processor emits back onto the bus
        return len(batch)


class ReplayController:
    """The L3 spine: one **playback Bus** the whole app subscribes to, fed either
    by the live engine (following now) or by re-streaming the historic slice
    (parked). "Live is just the head at now." Driven by a shared `TimeContext`;
    calls `on_reset` when the view jumps (so consumers clear stale data).

    Source selection is a callable (the routed sources, from the Dashboard).
    Qt-free; the engine it subscribes to may be the Qt Engine — only `subscribe`
    is used. Replay runs synchronously on park for now (off-thread is a later
    optimisation, signalled by the realtime-rate readout)."""

    def __init__(self, engine, store, time_context, sources=None, on_reset=None,
                 on_progress=None, reader=None, runner=None, gui_pump=None):
        self.store = store
        self.tc = time_context
        self.bus = Bus()                             # what the dashboard subscribes to
        # replay reads full-res through `reader` — the RESOLVER (RAM + local store
        # + hub tier) when given, so parking re-streams history the client lacks
        # locally (e.g. from the hub after a local wipe); else the bare store.
        self.playback = PlaybackSource(reader or store, self.bus)
        self._sources = sources or store.sources     # callable → [source keys]
        self.on_reset = on_reset
        self.on_progress = on_progress               # frac 0..1 during a load; None=done
        # Off-GUI park/scrub (DESIGN §21.3): with a `runner` (a TaskRunner) the
        # full-res re-stream runs on a worker thread and `gui_pump(fn)` marshals
        # each chunk's bus drain onto the GUI thread, blocking the worker until it
        # paints (backpressure). Without a runner (headless/tests/server) the load
        # is synchronous exactly as before. `_generation` bumps on every render/
        # go-live so a superseded worker can't smear stale readings into a new view.
        self._runner = runner
        self._gui_pump = gui_pump
        self._generation = 0
        self._was_following = time_context.following
        self._last_nav = time_context.nav            # to detect navigation vs transport
        self._last_window = None                     # last window we rendered (skip if same)
        self._busy = False                           # re-entrancy guard (processEvents)
        self._live_unsub = engine.subscribe(self._on_live)
        self._ctx_unsub = time_context.subscribe(self._on_context)

    def _on_live(self, batch) -> None:
        if self.tc.following:                        # live → straight to the playback bus
            for r in batch:
                self.bus.publish(r)
            while self.bus.drain():                  # loop: flush derived a processor
                pass                                 # emits back onto the bus mid-drain

    def _on_context(self) -> None:
        """Correctness-first (inefficient is fine): the window's data is whatever the
        store holds for [t0,t1], rendered fresh. So **any navigation (scrub/tail) or
        play-step re-streams the exact window**; a live tick just appends (via the
        live pass-through) and a plain pause/freeze does nothing. No coverage
        bookkeeping — there's nothing to drift out of sync."""
        if self._busy:                               # a load is in flight (processEvents
            return                                   # may re-enter) — ignore until done
        t0, t1 = self.tc.window
        navigated = self.tc.nav != self._last_nav    # scrub / tail-drag (vs transport)
        self._last_nav = self.tc.nav
        if self.tc.following:
            # entering live, or the window changed by navigation (tail-drag) while live
            # → render the window once; a plain live tick just appends via _on_live.
            need = (not self._was_following) or navigated
            self._was_following = True               # set BEFORE render: if a render
            if need:                                 # raises, we must NOT re-fire it
                self._render(t0, t1)                 # every tick (that's a load loop)
            return
        self._was_following = False
        if navigated:                                # scrub / seek → full re-render
            self._render(t0, t1)
        elif self.tc.playing and (t0, t1) != self._last_window:
            self._advance(t0, t1)                    # play-step → incremental append

    def _advance(self, t0, t1) -> None:
        """A playback step: the head walked forward, so stream ONLY the newly-entered
        slice (prev-front → new-front) and let the panels append + trim — instead of
        clearing and re-streaming the WHOLE window every frame (which, once park/scrub
        went off-thread, flashed a 'Loading history' task 20×/s). A discontinuity
        (speed seek, or the front jumping backward) falls back to a full render."""
        prev = self._last_window
        self._last_window = (t0, t1)
        if prev is not None and prev[1] <= t1 and t0 <= t1 and prev[1] >= t0 - 1e-9:
            seg0, seg1 = prev[1], t1                 # continuous forward advance
            if seg1 > seg0:
                # small slice → stream inline (no task, no clear); the panel buffers
                # append it and the play tick trims the back edge (like live).
                self.playback.stream(list(self._sources()), seg0, seg1)
        else:
            self._render(t0, t1)                     # discontinuous → re-render fresh

    def _render(self, t0, t1, progress=True) -> None:
        """Clear the panels and re-stream the exact window [t0,t1] in time order."""
        self._last_window = (t0, t1)
        self._generation += 1                        # invalidate any in-flight load
        if self.on_reset:
            self.on_reset()                          # clear stale data (panels re-fit)
        self._load(t0, t1, progress)

    def _load(self, t0, t1, progress=True) -> None:
        """Full-res re-stream of [t0,t1]. With a runner it goes on a worker thread
        (cancellable, GUI-paced); without one it runs synchronously as before."""
        cb = self.on_progress if (progress and self.on_progress) else None
        if self._runner is not None:
            self._load_async(t0, t1, cb)
            return
        self._busy = True
        try:
            self.playback.stream(list(self._sources()), t0, t1, on_progress=cb)
        finally:
            self._busy = False
            if cb:
                cb(None)                             # done → hide the indicator

    def _load_async(self, t0, t1, cb) -> None:
        gen = self._generation
        sources = list(self._sources())
        span = max(0.0, t1 - t0)
        why = (f"Re-streaming {span / 3600:.1f} h of history at full resolution "
               "through the analysis pipeline" if span >= 3600 else
               f"Re-streaming {span:.0f} s of history at full resolution")

        def should_stop():
            return gen != self._generation          # superseded by a newer view

        def pump(_batch):
            # Marshal the bus drain onto the GUI thread and block until it paints
            # — panels stay GUI-only (§21.1); the worker can't outrun rendering.
            if self._gui_pump is not None and not should_stop():
                self._gui_pump(self._drain_gui)

        def work(ctx):
            # progress flows through the TaskRunner UI (ctx.progress → queued to
            # the GUI). The GUI on_progress (cb) is NEVER called from here — it
            # would touch Qt off-thread; `finish` calls it on the GUI thread.
            self.playback.stream(sources, t0, t1,
                                 on_progress=lambda f: ctx.progress(f, ""),
                                 should_stop=should_stop, pump=pump)
            return None

        def finish(_res=None):
            if cb:
                cb(None)                             # GUI thread: hide any indicator

        self._runner.run(work, title="Loading history", why=why, cancellable=True,
                         exclusive="replay", on_busy="supersede",
                         on_done=finish, on_error=finish)

    def _drain_gui(self) -> None:
        """GUI thread: fan the just-published chunk to panels + flush any derived
        processor readings (the same loop the synchronous path used)."""
        while self.bus.drain():
            pass

    def stop(self) -> None:
        self._generation += 1                        # cancel any in-flight load
        self._live_unsub()
        self._ctx_unsub()
