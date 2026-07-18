"""PeriodicWorker — the ONE shared periodic-service-loop skeleton (DESIGN §21.4).

The 2026-07-17 concurrency audit found the identical hand-rolled loop (plain
daemon thread + Event stop + interval wake + bounded join) copied into
net/sync.py, net/videosync.py and store/prefetcher.py, each with gratuitously
different stop semantics. §21.4 is normative: a long-lived Qt-free service loop
uses THIS skeleton — never a bare thread at a call site.

Contract:

* ``fn`` runs every ``interval`` seconds on a **daemon** thread named exactly
  ``name`` (an ``fd-*`` / ``ferrodac-*`` name, so the watchdog, ``stats()`` and
  a crash dump can attribute it — §21.4).
* ``wake()`` runs the next pass immediately. The sleep is a condition-variable
  wait, not sleep-polling, so a wake is instant — and a wake landing DURING a
  pass is remembered (the following sleep returns at once, no lost trigger;
  the prefetcher's budget-exhausted self-wake relies on this).
* ``stop(timeout)`` = signal + bounded ``join(timeout)`` — shutdown never
  blocks forever, and the thread is a daemon so a timed-out join cannot hang
  process exit either. Idempotent; ``start()`` after ``stop()`` restarts.
* An exception escaping ``fn`` is caught + logged (one line, traceback at
  debug) and NEVER kills the loop — a periodic service survives a bad pass and
  retries next tick. A loop that wants its own per-pass error reporting (the
  sync runners' status callbacks) simply catches inside ``fn``.
* ``run_immediately=True`` runs the first pass at start; otherwise the first
  pass happens after one interval (or the first ``wake()``).
* Optional ``on_start`` / ``on_stop`` hooks run ON the worker thread, before
  the first pass and after the loop exits — a session-owning loop opens its
  network channel on its own thread and closes it there too.

Qt-free, plain ``threading`` per the §21.3 house style; nothing here may touch
the GUI.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("ferrodac.periodic")


class PeriodicWorker:
    """Run ``fn`` every ``interval`` seconds on a named daemon thread."""

    def __init__(self, fn, interval: float, name: str, *,
                 run_immediately: bool = False, on_start=None, on_stop=None):
        self._fn = fn
        self.interval = float(interval)   # read fresh each sleep — tunable live
        self.name = str(name)
        self._run_immediately = bool(run_immediately)
        self._on_start = on_start
        self._on_stop = on_stop
        self._cond = threading.Condition()
        self._stopping = False
        self._wake_pending = False
        self._thread: "threading.Thread | None" = None

    # -- lifecycle -----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> bool:
        """Spawn the loop thread. Returns False (no-op) if already running."""
        if self._thread is not None:
            return False
        with self._cond:
            self._stopping = False        # restartable after a stop()
        self._thread = threading.Thread(target=self._run, name=self.name,
                                        daemon=True)
        self._thread.start()
        return True

    def wake(self) -> None:
        """Run the next pass NOW. Thread-safe; before start / after stop it just
        leaves a pending flag (harmless — at worst an immediate first pass)."""
        with self._cond:
            self._wake_pending = True
            self._cond.notify_all()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the loop to exit and join it (bounded). Idempotent."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        t = self._thread
        if t is not None:
            t.join(timeout)
            self._thread = None

    # -- worker (loop thread) ------------------------------------------------
    def _run(self) -> None:
        if self._on_start is not None:
            try:
                self._on_start()
            except Exception:             # noqa: BLE001 — broken setup aborts the loop
                log.warning("%s: on_start failed — loop aborted", self.name,
                            exc_info=True)
                self._finish()
                return
        try:
            if not self._run_immediately and not self._sleep():
                return                    # stopped before the first pass
            while not self._stopping:
                try:
                    self._fn()
                except Exception:         # noqa: BLE001 — a bad pass never kills the loop
                    log.debug("%s: periodic pass failed", self.name,
                              exc_info=True)
                if not self._sleep():
                    break
        finally:
            self._finish()

    def _sleep(self) -> bool:
        """Wait one interval — or return early on wake()/stop(). False iff stopping."""
        deadline = time.monotonic() + self.interval
        with self._cond:
            while not self._stopping and not self._wake_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            self._wake_pending = False
            return not self._stopping

    def _finish(self) -> None:
        if self._on_stop is not None:
            try:
                self._on_stop()
            except Exception:             # noqa: BLE001
                log.warning("%s: on_stop failed", self.name, exc_info=True)
