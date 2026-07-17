"""Crash & threading diagnostics — turn a bare segfault into a stack trace.

Segfaults in this app come from a C extension, and almost always that's Qt/
PySide: the classic cause is a Qt call from a RAW worker thread (the gRPC sync
threads are plain ``threading.Thread``s, not ``QThread``s). Qt prints e.g.

    QBasicTimer::start: QBasicTimer can only be used with threads started with QThread

…then corrupts the heap and SIGSEGVs some time later — so the crash trace alone
points nowhere useful. Two aids, both cheap and always-on (``FERRODAC_NO_DIAG=1``
to disable):

  * **faulthandler** — dumps a Python traceback of EVERY thread on a fatal signal
    (SIGSEGV/SIGABRT/SIGFPE/SIGBUS), and on ``SIGUSR1`` on demand (for a hang:
    ``kill -USR1 <pid>``).
  * **a Qt message handler** — echoes Qt messages AND, when one smells of a
    cross-thread misuse (or is emitted off the main thread), prints the offending
    thread name + Python stack RIGHT THEN — i.e. at the warning, before the
    crash — so you see exactly which call touched Qt from the wrong thread.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback

_crash_file = None        # kept open so we can also persist the trace

# Qt message fragments that mean "Qt was touched from the wrong thread".
_THREAD_FLAGS = (
    "QBasicTimer", "Timers can only be used", "QObject::startTimer",
    "QObject::killTimer", "Cannot create children for a parent in a different",
    "QSocketNotifier", "different thread", "moveToThread", "QPixmap",
)


def install(logdir: str = "") -> None:
    """Install both aids. `logdir` (the app's log folder) also gets the trace."""
    if os.environ.get("FERRODAC_NO_DIAG"):
        return
    _install_faulthandler(logdir)
    _install_qt_message_handler()


_gc_timer = None        # kept alive so the GUI-thread collector keeps running


def install_gui_thread_gc(interval_ms: int = 2000):
    """Garbage-collect ONLY on the GUI thread — the fix for the long-standing
    segfault.

    Python's cyclic GC runs on whichever thread crosses the allocation threshold.
    The data plane's worker threads (notably zarr's 'zarr_io' de/compression loop)
    allocate heavily, so GC fires *there* — and if it frees a QObject that owns a
    timer, that's a CROSS-THREAD Qt destruction (``QObject::~QObject: Timers cannot
    be stopped from another thread`` / ``QBasicTimer``) which corrupts Qt's state and
    SIGSEGVs. Disabling automatic GC and draining it from a GUI-thread ``QTimer``
    keeps every QObject finalisation on the GUI thread. Returns the timer.
    """
    global _gc_timer
    import gc
    import logging
    import time as _time

    from qtpy.QtCore import QTimer
    gc.disable()                                 # no GC on a worker thread, ever
    try:
        gc.freeze()                              # startup/import objects → permanent
    except Exception:                            # generation: collections skip them,
        pass                                     # so the pause tracks CHURN not heap
    # Cheap generational collect most ticks; a full gen-2 sweep only occasionally
    # (DESIGN §21 Tier-1) — the old full gc.collect() every 2 s cost O(live heap)
    # and grew with the session. Duration is logged when a sweep is slow, so
    # retained-object growth is visible instead of silently jittering the UI.
    state = {"i": 0}
    log = logging.getLogger("ferrodac")

    def _collect():
        state["i"] += 1
        gen = 2 if state["i"] % 15 == 0 else 1
        t0 = _time.monotonic()
        freed = gc.collect(gen)
        dt = (_time.monotonic() - t0) * 1000.0
        if dt > 80.0:
            log.warning("gc.collect(gen=%d) took %.0f ms (freed %d objects) — "
                        "retained-object growth", gen, dt, freed)

    _gc_timer = QTimer()
    _gc_timer.timeout.connect(_collect)          # …drained here, on the GUI thread
    _gc_timer.start(max(250, int(interval_ms)))
    return _gc_timer


_wd_thread = None       # kept alive: the GUI-stall watchdog checker


def install_gui_watchdog(threshold_ms: int = 500, beat_ms: int = 100):
    """Make GUI-thread stalls LOUD (DESIGN §21.2): a GUI QTimer stamps a
    heartbeat; a plain daemon thread watches it and, when the gap exceeds
    `threshold_ms`, logs the stall WITH the main thread's Python stack — i.e.
    it names the blocking call while it is still blocking. Logs the total
    duration when the GUI comes back. The audit's months-invisible per-tick
    costs become log lines. Returns the heartbeat timer."""
    global _wd_thread
    if os.environ.get("FERRODAC_NO_DIAG"):
        return None
    import time as _time

    from qtpy.QtCore import QTimer
    cell = {"beat": _time.monotonic(), "stalled_since": None}
    timer = QTimer()
    timer.timeout.connect(lambda: cell.__setitem__("beat", _time.monotonic()))
    timer.start(max(20, int(beat_ms)))

    def _watch() -> None:
        main = threading.main_thread()
        while True:
            _time.sleep(0.25)
            now = _time.monotonic()
            gap = now - cell["beat"]
            if gap > threshold_ms / 1000.0:
                if cell["stalled_since"] is None:
                    cell["stalled_since"] = cell["beat"]
                    frame = sys._current_frames().get(main.ident)
                    stack = "".join(traceback.format_stack(frame)) if frame else "?"
                    _write(f"[watchdog] GUI thread stalled > {gap * 1000:.0f} ms — "
                           f"main-thread stack:\n"
                           + "".join("    " + ln for ln in stack.splitlines(True)))
            elif cell["stalled_since"] is not None:
                total = (now - cell["stalled_since"]) * 1000.0
                cell["stalled_since"] = None
                _write(f"[watchdog] GUI thread responsive again "
                       f"(stall ≈ {total:.0f} ms)\n")

    _wd_thread = threading.Thread(target=_watch, name="fd-gui-watchdog",
                                  daemon=True)
    _wd_thread.start()
    timer._fd_watchdog = _wd_thread     # keep both alive together
    return timer


def _write(s: str) -> None:
    try:
        sys.stderr.write(s)
        sys.stderr.flush()
    except Exception:
        pass
    if _crash_file is not None:
        try:
            _crash_file.write(s)
            _crash_file.flush()
        except Exception:
            pass


def _install_faulthandler(logdir: str) -> None:
    global _crash_file
    if logdir:
        try:
            os.makedirs(logdir, exist_ok=True)
            _crash_file = open(os.path.join(logdir, "ferrodac.crash.log"),
                               "w", encoding="utf-8")
        except Exception:
            _crash_file = None
    # The fatal-signal dump goes to a real fd: the crash log if we have one (it
    # survives a closed terminal), else stderr.
    faulthandler.enable(file=_crash_file or sys.stderr, all_threads=True)
    try:
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)   # on-demand dump
    except (AttributeError, ValueError, OSError):
        pass                                  # no SIGUSR1 (e.g. Windows)


def _install_qt_message_handler() -> None:
    try:
        from qtpy.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return
    main_thread = threading.main_thread()

    def handler(mode, context, message):
        _write(f"[Qt] {message}\n")
        off_main = threading.current_thread() is not main_thread
        smells = any(f in message for f in _THREAD_FLAGS)
        if (smells or off_main) and mode != QtMsgType.QtDebugMsg:
            _write(f"  ^^ emitted on thread '{threading.current_thread().name}' "
                   f"(off the GUI thread: {off_main}) — Python stack:\n")
            _write("".join("    " + ln for ln in traceback.format_stack()))
            _write("  ^^ (a Qt call from a non-QThread worker — likely the "
                   "segfault's root cause)\n")

    qInstallMessageHandler(handler)


_ui_trace = None        # kept alive: the installed event filter


def install_ui_trace():
    """Log every TOP-LEVEL window Qt shows (class, objectName, title, geometry)
    with a ms timestamp — the tool for 'a window popped up and I don't know whose
    it is' (e.g. per-object popup spam during a project load). Enable with
    ``FERRODAC_UI_TRACE=1``; events go to the 'ferrodac.uitrace' logger (→ the
    app log). Cheap: one isWindow() check per Show/Hide event."""
    global _ui_trace
    import logging
    import time as _time

    from qtpy.QtCore import QEvent, QObject
    from qtpy.QtWidgets import QApplication, QWidget

    log = logging.getLogger("ferrodac.uitrace")
    t0 = _time.monotonic()

    class _Tracer(QObject):
        def eventFilter(self, obj, ev):  # noqa: N802
            if ev.type() in (QEvent.Show, QEvent.Hide) and isinstance(obj, QWidget) \
                    and obj.isWindow():
                g = obj.geometry()
                log.info("%+9.3fs %s %s(%r) title=%r %dx%d@%d,%d floating=%s",
                         _time.monotonic() - t0,
                         "SHOW" if ev.type() == QEvent.Show else "HIDE",
                         type(obj).__name__, obj.objectName(), obj.windowTitle(),
                         g.width(), g.height(), g.x(), g.y(),
                         getattr(obj, "isFloating", lambda: "-")())
            return False

    app = QApplication.instance()
    if app is not None:
        _ui_trace = _Tracer()
        app.installEventFilter(_ui_trace)
        log.info("UI trace installed")
