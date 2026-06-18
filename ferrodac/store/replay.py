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

from ..core.reading import Reading


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
        self._subs: list = []

    @property
    def window(self):
        return (self.head - self.width, self.head)

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
        self.following, self.playing, self.head = True, False, self._now()
        self._notify()

    def park(self, head: float):
        self.following, self.head = False, float(head)
        self._notify()

    def set_width(self, width: float):
        self.width = max(1e-3, float(width))
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
            t, v = self.store.read_raw(sid, t0, t1)
            if not len(t):
                continue
            dev, _, src = sid.rpartition("/")        # key 'device/source' → Reading
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

    def _emit(self, batch) -> int:
        for r in batch:
            self.bus.publish(r)
        self.bus.drain()                             # fan the chunk to subscribers
        return len(batch)
