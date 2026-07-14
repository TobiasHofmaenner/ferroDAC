"""TaskRunner — the one reusable worker+progress pattern (DESIGN §21.3).

The user requirement, verbatim: *when the app needs to wait for something it
should never freeze, but tell the user what it is doing, why they have to wait,
and if possible how long it will take.* So every user-triggered wait (park/scrub
re-stream, recording export, later git/zip/downloads) runs as a **Task**: a plain
worker thread runs ``fn(ctx)``; progress/result/errors marshal to the GUI via
queued signals; the status bar shows what/why/ETA with a Cancel button.

Threading contract (DESIGN §21.1): worker code touches ONLY the Qt-free
``TaskContext`` and returns plain data — it must never construct or touch a
QObject (they are finalized on the GUI thread). The ``Task`` QObject is created
on the GUI thread by ``run()`` and only its signals cross threads (thread-safe
emit; delivered with explicit ``Qt.QueuedConnection``).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from qtpy.QtCore import QObject, Qt, Signal
from qtpy.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QToolButton, QWidget

log = logging.getLogger("ferrodac.tasks")


def _fmt_eta(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 90:
        return f"~{s}s left"
    if s < 5400:
        return f"~{round(s / 60)} min left"
    return f"~{round(s / 3600, 1)} h left"


class TaskCancelled(Exception):
    """Raised by ``TaskContext.check()`` when the user cancelled the task."""


# The app installs its TaskRunner as the process-wide default so UI code deep in the
# widget tree (dialogs) can run background work without threading a runner through
# every constructor. run_task() falls back to running synchronously when none is set
# (headless / a dialog built without a runner) — there is no GUI to freeze there.
_default_runner = None


def set_default_runner(runner) -> None:
    global _default_runner
    _default_runner = runner


class _SyncContext:
    """A do-nothing TaskContext for the synchronous fallback path."""
    def progress(self, frac=None, detail: str = "") -> None: ...
    @property
    def cancelled(self) -> bool:
        return False
    def check(self) -> None: ...


def run_task(fn, *, on_done=None, on_error=None, **kw):
    """Run ``fn(ctx)`` on the app's default TaskRunner if one is installed, else
    synchronously (same signature as TaskRunner.run: title/why/exclusive/… are
    accepted and ignored in the fallback). Returns the Task, or None."""
    if _default_runner is not None:
        try:
            return _default_runner.run(fn, on_done=on_done, on_error=on_error, **kw)
        except RuntimeError:                         # runner shut down (window closed) —
            pass                                     # fall through to run synchronously
    try:
        result = fn(_SyncContext())
    except TaskCancelled:
        return None
    except Exception as exc:                     # noqa: BLE001
        if on_error is not None:
            on_error(str(exc) or exc.__class__.__name__)
        return None
    if on_done is not None:
        on_done(result)
    return None


class GuiBridge(QObject):
    """Run a callable on the GUI thread from any worker thread. One instance,
    created on the GUI thread — the single blessed worker→GUI marshal (§21.1)."""

    _call = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._call.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn):
        try:
            fn()
        except Exception:                        # noqa: BLE001 — never kill the loop
            log.exception("GuiBridge callable failed")

    def post(self, fn) -> None:
        """Fire-and-forget: run fn() on the GUI thread."""
        self._call.emit(fn)

    def post_and_wait(self, fn, timeout: float = 5.0, reraise: bool = False):
        """Run fn() on the GUI thread and BLOCK the caller until it finishes (or
        `timeout`). Returns fn()'s result. Used by the replay pump so a worker
        never renders faster than the GUI can paint — natural backpressure.
        Never call from the GUI thread (it would deadlock).

        With reraise=True an exception from fn() is captured and re-raised on the
        CALLER's thread instead of being logged-and-swallowed (which makes the caller
        see None). The control surface uses this so a GUI-thread verb handler's
        ControlError actually reaches the connector rather than becoming a silent null."""
        done = threading.Event()
        box: dict = {}

        def wrapped():
            try:
                box["v"] = fn()
            except Exception as exc:             # noqa: BLE001
                box["exc"] = exc
                if not reraise:                  # legacy: let _run log-and-swallow
                    raise
            finally:
                done.set()

        self._call.emit(wrapped)
        done.wait(timeout)
        if reraise and "exc" in box:
            raise box["exc"]
        return box.get("v")


class TaskContext:
    """Qt-free handle passed to the worker fn. The ONLY thing fn may touch."""

    def __init__(self, task: "Task"):
        self._task = task

    def progress(self, frac, detail: str = "") -> None:
        """Report progress: frac in 0..1, or None for indeterminate."""
        self._task._progress.emit(frac, detail)

    @property
    def cancelled(self) -> bool:
        return self._task._cancel.is_set()

    def check(self) -> None:
        if self.cancelled:
            raise TaskCancelled()


class Task(QObject):
    """GUI-thread handle for one running task. Signals are delivered on the GUI
    thread (queued); `cancel()` sets a cooperative flag the fn polls."""

    _progress = Signal(object, str)        # frac|None, detail  (raw, from worker)
    progress = Signal(object, str, object)  # frac|None, detail, eta_s|None (to UI)
    finished = Signal(object)              # fn's return value
    failed = Signal(str)                   # message (full traceback logged)
    done = Signal()                        # finished|failed|cancelled — always fires

    def __init__(self, title: str, why: str, cancellable: bool, parent=None):
        super().__init__(parent)
        self.title = title
        self.why = why
        self.cancellable = cancellable
        self._cancel = threading.Event()
        self._started = time.monotonic()
        self._progress.connect(self._on_progress, Qt.QueuedConnection)

    def cancel(self) -> None:
        self._cancel.set()

    def _on_progress(self, frac, detail) -> None:
        # GUI thread: extrapolate an ETA from the progress rate and re-emit.
        eta = None
        if isinstance(frac, (int, float)) and 0.05 < frac < 1.0:
            elapsed = time.monotonic() - self._started
            if elapsed > 1.0:
                eta = elapsed * (1.0 - frac) / frac
        self.progress.emit(frac, detail, eta)


class TaskRunner(QObject):
    """Owns the worker pool + the exclusivity registry. Create one per window on
    the GUI thread; call ``run()`` from the GUI thread."""

    task_started = Signal(object)          # Task — for the status-bar UI
    task_ended = Signal(object)            # Task

    def __init__(self, parent=None, max_workers: int = 4):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="fd-task")
        self._active: dict[str, Task] = {}     # exclusive key -> Task
        self._all: set[Task] = set()           # strong refs until done (I-1)

    def run(self, fn, *, title: str, why: str = "", cancellable: bool = False,
            exclusive: str = "", on_busy: str = "supersede",
            on_done=None, on_error=None) -> "Task | None":
        """Run ``fn(ctx)`` on a worker thread. `exclusive` keys serialize a class
        of task: `on_busy="supersede"` cancels the in-flight one and starts this
        one; `on_busy="reject"` returns None (caller shows a toast). `on_done(res)`
        / `on_error(msg)` run on the GUI thread."""
        if exclusive and exclusive in self._active:
            if on_busy == "reject":
                return None
            self._active[exclusive].cancel()   # supersede: the old fn stops at its
            #                                    next check(); we start immediately

        task = Task(title, why, cancellable, parent=self)
        self._all.add(task)
        if exclusive:
            self._active[exclusive] = task
        ctx = TaskContext(task)

        def _cleanup() -> None:
            # GUI thread (queued via `done`, which always fires LAST): retire the
            # task. All registry mutation happens here, single-threaded.
            if self._active.get(exclusive) is task:
                self._active.pop(exclusive, None)
            self._all.discard(task)
            self.task_ended.emit(task)

        task.done.connect(_cleanup, Qt.QueuedConnection)
        if on_done is not None:
            task.finished.connect(on_done, Qt.QueuedConnection)
        if on_error is not None:
            task.failed.connect(on_error, Qt.QueuedConnection)

        def _work() -> None:
            # Worker thread: only emit signals (thread-safe) + return plain data.
            # finished/failed are emitted BEFORE done, both queued, so on_done/
            # on_error run before _cleanup on the GUI thread.
            try:
                result = fn(ctx)
            except TaskCancelled:
                pass                           # cancelled → no finished/failed
            except Exception as exc:           # noqa: BLE001
                log.exception("task %r failed", title)
                task.failed.emit(str(exc) or exc.__class__.__name__)
            else:
                task.finished.emit(result)
            finally:
                task.done.emit()

        self.task_started.emit(task)
        self._pool.submit(_work)
        return task

    def active(self) -> list:
        return [t for t in self._all]

    def shutdown(self, cancel: bool = True) -> None:
        if cancel:
            for t in list(self._all):
                t.cancel()
        self._pool.shutdown(wait=False)


class TaskStatusWidget(QWidget):
    """A compact status-bar readout of running tasks: what / detail / % / ETA,
    with a Cancel button (DESIGN §21.3). Shows the most recent active task and a
    `(+N)` when several run at once. Hidden when idle."""

    def __init__(self, runner: TaskRunner, parent=None):
        super().__init__(parent)
        self._active: list = []                  # Tasks, newest last
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._label = QLabel("")
        self._label.setStyleSheet("color:#9aa4b2;")
        self._bar = QProgressBar()
        self._bar.setMaximumWidth(120)
        self._bar.setMaximumHeight(12)
        self._bar.setTextVisible(False)
        self._cancel = QToolButton()
        self._cancel.setText("✕")
        self._cancel.setToolTip("Cancel")
        self._cancel.setAutoRaise(True)
        self._cancel.clicked.connect(self._on_cancel)
        for w in (self._label, self._bar, self._cancel):
            lay.addWidget(w)
        runner.task_started.connect(self._on_started)
        runner.task_ended.connect(self._on_ended)
        self._refresh()

    def _on_started(self, task) -> None:
        self._active.append(task)
        task.progress.connect(self._on_progress)
        self.setToolTip(task.why)
        self._refresh()

    def _on_ended(self, task) -> None:
        if task in self._active:
            self._active.remove(task)
        self._refresh()

    def _on_progress(self, frac, detail, eta) -> None:
        if not self._active:
            return
        self._current = self._active[-1]
        self._draw(frac, detail, eta)

    def _on_cancel(self) -> None:
        if self._active:
            self._active[-1].cancel()

    def _refresh(self) -> None:
        if not self._active:
            self.setVisible(False)
            return
        self.setVisible(True)
        t = self._active[-1]
        self._cancel.setVisible(t.cancellable)
        self._draw(None, "", None)

    def _draw(self, frac, detail, eta) -> None:
        t = self._active[-1]
        extra = f"  (+{len(self._active) - 1})" if len(self._active) > 1 else ""
        bits = [t.title]
        if detail:
            bits.append(detail)
        eta_s = _fmt_eta(eta)
        if eta_s:
            bits.append(eta_s)
        self._label.setText("  ·  ".join(bits) + extra)
        if isinstance(frac, (int, float)):
            self._bar.setRange(0, 100)
            self._bar.setValue(max(0, min(100, int(frac * 100))))
        else:
            self._bar.setRange(0, 0)             # indeterminate (busy) bar
