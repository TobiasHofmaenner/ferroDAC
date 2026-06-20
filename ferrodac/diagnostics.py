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
