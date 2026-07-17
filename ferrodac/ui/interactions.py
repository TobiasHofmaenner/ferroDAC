"""The Requests inbox — the device→app→device request/response surface (core.interaction).

The Qt half of the interaction channel (the tag-store analogue of ``markers.py``):

  * :class:`PendingInteractions` — a QObject store of OPEN prompts (id → prompt + its
    driver ``on_response`` callback + who/when answered). It is the SINGLE source of
    truth the inbox, the arrival toast, the control surface and (future) the hub all
    read + resolve, so answering is first-responder-wins: ``resolve`` invokes the
    stored callback exactly once, records the responder, drops the prompt, and signals.
  * :func:`build_answer_controls` — the answer widgets AUTO-GENERATED from a prompt's
    ``kind`` (confirm→[Yes]/[No], choice→option buttons, text→field+submit,
    acknowledge→[OK]). The structured prompt IS the UI spec, so a new device's new
    prompt renders with zero new UI code — used by BOTH the inbox and the toast.
  * :class:`RequestsPanel` — the persistent inbox dock (a badge count + one card per
    open request, critical ones pinned + highlighted).
  * :class:`RequestToast` — a non-blocking arrival banner, answerable inline, that
    auto-tucks into the inbox after a few seconds (a CRITICAL one is sticky).

Threading: a prompt arrives on the manager's ``device_prompt`` signal (QueuedConnection),
so everything here runs on the GUI thread; the driver's ``on_response`` runs here too and
must be GUI-safe (the driver schedules any blocking hardware act off-thread itself).
"""

from __future__ import annotations

import logging
import time

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import

from qtpy.QtCore import QObject, Qt, QTimer, Signal
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.interaction import (
    ABORT, CHOICE, CONFIRM, STAY, TEXT, prompt_to_dict)

log = logging.getLogger("interaction")

# severity → the card/banner accent colour (routine is quiet; critical shouts).
_SEVERITY_COLOR = {"info": "#4dabf7", "warn": "#ffa94d", "critical": "#f03e3e"}

# Flood guard: a driver bug that calls ask() every poll cycle mints a fresh-id prompt
# each time (so id-dedup can't help) and would fill the store unbounded. Past this many
# OPEN prompts for one device we drop new ones (and log) rather than melt the inbox.
_MAX_OPEN_PER_DEVICE = 64


class _Open:
    """One open prompt + its driver callback + the answer bookkeeping (who/when)."""

    def __init__(self, prompt, on_response):
        self.prompt = prompt
        self.on_response = on_response
        self.answer = None
        self.answered_by = None       # "operator" | "connector:<name>" | "timeout:*"
        self.answered_at = None       # epoch seconds
        self.ok = True                # did on_response run cleanly? (False → audit records the failure)


class PendingInteractions(QObject):
    """The open-prompt store — the shared, first-responder-wins answer book.

    Signals:
      * ``changed``   — any mutation (coarse, no-arg) → the inbox re-renders + badges update.
      * ``added``     — a NEW prompt arrived (the Prompt) → the arrival toast pops.
      * ``resolved``  — a prompt was answered (the :class:`_Open` record: prompt + answer
                        + answered_by/at) → the app auto-emits the provenance tag.
    """

    changed = Signal()
    added = Signal(object)
    resolved = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._open: dict[str, _Open] = {}
        self._timers: dict[str, QTimer] = {}

    # -- mutations -----------------------------------------------------------
    def add(self, prompt, on_response=None) -> str:
        """File a device-raised prompt. Idempotent by id (a re-delivered prompt is
        ignored). Arms the timeout timer if the prompt declares one. A device that
        floods the store past _MAX_OPEN_PER_DEVICE open prompts is throttled (the new
        prompt is dropped + logged) so a driver bug can't melt the inbox."""
        if prompt.id in self._open:
            return prompt.id
        if sum(1 for e in self._open.values()
               if e.prompt.device_id == prompt.device_id) >= _MAX_OPEN_PER_DEVICE:
            log.warning("device %s already has %d open prompts — dropping %s (flood guard)",
                        prompt.device_id, _MAX_OPEN_PER_DEVICE, prompt.id)
            return prompt.id
        self._open[prompt.id] = _Open(prompt, on_response)
        if prompt.timeout and prompt.timeout > 0:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda pid=prompt.id: self._timed_out(pid))
            t.start(int(prompt.timeout * 1000))
            self._timers[prompt.id] = t
        self.added.emit(prompt)
        self.changed.emit()
        return prompt.id

    def resolve(self, pid: str, answer, by: str = "operator") -> bool:
        """Answer prompt ``pid`` with ``answer`` (recording WHO via ``by``). The FIRST
        caller wins: if it is already gone (answered by another surface) this is a no-op
        returning False. Invokes the driver's ``on_response(answer)`` once, drops the
        prompt, and emits ``resolved`` + ``changed``. Runs on the GUI thread."""
        entry = self._open.pop(pid, None)
        if entry is None:
            return False                     # already answered — first-responder wins
        timer = self._timers.pop(pid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()              # don't accumulate dead timers on the long-lived store
        entry.answer = answer
        entry.answered_by = by
        entry.answered_at = time.time()
        cb = entry.on_response
        if cb is not None:
            try:
                cb(answer)                   # the driver acts (must be GUI-safe — see module doc)
            except Exception:                # a bad driver callback must not break the store
                entry.ok = False             # …but the audit tag must not claim it succeeded
                log.exception("prompt %s on_response failed", pid)
        self.resolved.emit(entry)
        self.changed.emit()
        return True

    def _timed_out(self, pid: str) -> None:
        """Apply the prompt's on_timeout policy. A CRITICAL prompt never silently
        proceeds: only an explicit ABORT is honoured, else it stays pending."""
        entry = self._open.get(pid)
        if entry is None:
            return
        policy = entry.prompt.on_timeout
        if policy == STAY:
            return                           # left pending forever (re-timer not re-armed)
        if entry.prompt.is_critical and policy != ABORT:
            return                           # critical never auto-answers on a default
        if policy == ABORT:
            self.resolve(pid, None, by="timeout:abort")
        else:
            self.resolve(pid, policy, by="timeout")   # a literal default answer

    def withdraw(self, *device_ids) -> int:
        """Drop every OPEN prompt raised by one of these device ids WITHOUT invoking
        on_response — the request is being RETIRED, not answered (the device was removed,
        or resolved it locally / on another transport). This is the counterpart to
        first-responder-wins for the case where the answer never comes through this app:
        it clears the inbox/toast + kills the timer, but emits no ``resolved`` (no
        provenance tag, no callback into a possibly-dead driver). Returns how many went.
        Match on device_id (the prompt carries data_id = uuid-or-instance_id), so pass
        both the uuid and the instance_id, like ``has_pending``."""
        ids = {d for d in device_ids if d}
        return self._drop([pid for pid, e in self._open.items() if e.prompt.device_id in ids])

    def withdraw_ids(self, *prompt_ids) -> int:
        """Retire specific OPEN prompts BY id, WITHOUT invoking on_response — the device (its
        front panel / another transport) or a future hub peer already resolved them, so they
        must leave the inbox without this app answering (no double-answer, no provenance tag).
        The per-prompt counterpart to :meth:`withdraw` (which retires by device). Returns how
        many were dropped."""
        return self._drop([pid for pid in prompt_ids if pid in self._open])

    def _drop(self, victims: list) -> int:
        """Remove open prompts (by id) + their timers and signal — the shared body of
        withdraw / withdraw_ids. Never touches on_response (a withdrawal is not an answer)."""
        for pid in victims:
            self._open.pop(pid, None)
            t = self._timers.pop(pid, None)
            if t is not None:
                t.stop()
                t.deleteLater()
        if victims:
            self.changed.emit()
        return len(victims)

    def clear(self) -> None:
        for t in self._timers.values():
            t.stop()
            t.deleteLater()
        self._timers.clear()
        if self._open:
            self._open.clear()
            self.changed.emit()

    # -- queries -------------------------------------------------------------
    def get(self, pid: str):
        entry = self._open.get(pid)
        return entry.prompt if entry is not None else None

    def pending(self) -> list:
        """Open prompts, oldest first (critical-first is a VIEW concern, not the model)."""
        return [e.prompt for e in sorted(self._open.values(),
                                         key=lambda e: e.prompt.created)]

    def count(self) -> int:
        return len(self._open)

    def has_pending(self, *device_ids) -> bool:
        """True if any open prompt was raised by one of these device ids (uuid or
        instance_id) — drives the raising device's '⏳ awaiting operator' indicator."""
        ids = {d for d in device_ids if d}
        return any(e.prompt.device_id in ids for e in self._open.values())

    def to_list(self) -> list:
        """The open prompts as JSON-able dicts — what the control surface returns."""
        return [prompt_to_dict(p) for p in self.pending()]


# --------------------------------------------------------------------------- #
#  Answer controls — AUTO-GENERATED from the prompt's kind (the UI spec)
# --------------------------------------------------------------------------- #
def build_answer_controls(prompt, respond) -> list:
    """The answer widgets for ``prompt``, wired to call ``respond(answer)``. Derived
    entirely from ``prompt.kind`` so a new device's new prompt renders with zero new
    UI code. Shared by the inbox card and the arrival toast."""
    kind = prompt.kind
    if kind == CONFIRM:
        yes, no = QPushButton("Yes"), QPushButton("No")
        yes.clicked.connect(lambda: respond(True))
        no.clicked.connect(lambda: respond(False))
        return [yes, no]
    if kind == CHOICE:
        btns = []
        for opt in (prompt.options or []):
            b = QPushButton(str(opt))
            b.clicked.connect(lambda _=False, o=opt: respond(o))
            btns.append(b)
        return btns or [_ack_button(respond)]      # a choice with no options degrades to OK
    if kind == TEXT:
        edit = QLineEdit()
        edit.setPlaceholderText("Type an answer…")
        submit = QPushButton("Submit")

        def _submit(*_a, e=edit, r=respond):
            txt = e.text()
            if txt.strip():                 # ignore an empty/blank submit — don't answer with ""
                r(txt)
        submit.clicked.connect(_submit)
        edit.returnPressed.connect(_submit)
        return [edit, submit]
    # ACKNOWLEDGE (and any unknown kind) — a single OK
    return [_ack_button(respond)]


def _ack_button(respond) -> QPushButton:
    ok = QPushButton("OK")
    ok.clicked.connect(lambda: respond(True))
    return ok


def _age_text(prompt) -> str:
    secs = max(0.0, time.time() - prompt.created)
    if secs < 90:
        return f"{secs:.0f}s ago"
    return f"{secs / 60:.0f}m ago"


# --------------------------------------------------------------------------- #
#  Requests inbox (dock) — a persistent list + a badge of pending prompts
# --------------------------------------------------------------------------- #
class RequestsPanel(QWidget):
    """The persistent Requests inbox: a badge count + one card per open prompt, each
    with answer controls auto-generated from its kind. Critical prompts sort first and
    are highlighted. Answering resolves through the shared store (first-responder-wins).
    ``device_name`` maps a device_id → a friendly name for the card header (optional)."""

    def __init__(self, store: PendingInteractions, device_name=None, parent=None):
        super().__init__(parent)
        self.store = store
        self._device_name = device_name or (lambda did: did)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        self._label = QLabel("Requests")
        self._label.setStyleSheet("font-size:12px; font-weight:700; color:#c7d0db;")
        root.addWidget(self._label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        host = QWidget()
        self._layout = QVBoxLayout(host)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)
        self._age_labels = []      # (QLabel, prompt) — refreshed in place on the age tick
        store.changed.connect(self._rebuild)
        # a live age readout WITHOUT rebuilding the cards: the slow tick only re-texts the
        # age labels. (Rebuilding here would destroy an in-progress text answer + its focus
        # every 5 s — a full _rebuild happens only when the prompt SET changes, on changed.)
        self._tick = QTimer(self)
        self._tick.setInterval(5000)
        self._tick.timeout.connect(self._refresh_ages)
        self._tick.start()
        self._rebuild()

    def _refresh_ages(self):
        for lbl, p in self._age_labels:
            try:
                lbl.setText(_age_text(p))
            except RuntimeError:               # a card was torn down since the last rebuild
                pass

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild(self):
        self._clear()
        self._age_labels = []                  # stale after _clear's deleteLater — rebuild below
        prompts = self.store.pending()
        # critical-first, then oldest-first — the view's ordering, not the model's
        prompts.sort(key=lambda p: (not p.is_critical, p.created))
        n = len(prompts)
        self._label.setText(f"Requests  ({n})" if n else "Requests")
        if not prompts:
            ph = QLabel("No open requests.\nDevices ask here when they need an answer "
                        "to proceed.")
            ph.setStyleSheet("color:#7f8a99;")
            ph.setWordWrap(True)
            self._layout.addWidget(ph)
        else:
            for p in prompts:
                self._layout.addWidget(self._card(p))
        self._layout.addStretch(1)

    def _card(self, prompt) -> QFrame:
        accent = _SEVERITY_COLOR.get(prompt.severity, "#4dabf7")
        card = QFrame()
        card.setObjectName("RequestCard")
        border = "2px solid" if prompt.is_critical else "1px solid"
        card.setStyleSheet(
            f"#RequestCard {{ background:#171c26; border:{border} {accent};"
            " border-radius:8px; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(6)
        dot = QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background:{accent}; border-radius:5px;")
        who = QLabel((("🚨 " if prompt.is_critical else "⏳ ")
                      + self._device_name(prompt.device_id)))
        who.setStyleSheet("font-weight:700;")
        age = QLabel(_age_text(prompt))
        age.setStyleSheet("color:#7f8a99; font-size:10px;")
        self._age_labels.append((age, prompt))     # re-texted in place by the age tick
        head.addWidget(dot)
        head.addWidget(who)
        head.addStretch(1)
        head.addWidget(age)
        lay.addLayout(head)
        q = QLabel(prompt.title or prompt.question or "(no question)")
        q.setWordWrap(True)
        lay.addWidget(q)
        if prompt.title and prompt.question:
            sub = QLabel(prompt.question)
            sub.setStyleSheet("color:#8b95a4; font-size:11px;")
            sub.setWordWrap(True)
            lay.addWidget(sub)
        controls = QHBoxLayout()
        controls.setSpacing(4)
        for w in build_answer_controls(prompt, self._responder(prompt.id)):
            controls.addWidget(w)
        controls.addStretch(1)
        lay.addLayout(controls)
        return card

    def _responder(self, pid):
        # resolve through the shared store so the inbox, the toast and the control
        # surface all race into ONE answer book (first-responder-wins).
        return lambda answer: self.store.resolve(pid, answer, by="operator")


# --------------------------------------------------------------------------- #
#  Arrival toast — a non-blocking banner, answerable inline
# --------------------------------------------------------------------------- #
class RequestToast(QFrame):
    """A floating arrival banner for a newly-raised prompt: answerable inline, it
    auto-tucks (hides) after a few seconds so it never blocks — EXCEPT a critical one,
    which is sticky (stays until answered). It resolves through the same store as the
    inbox, so answering it here or there is the same first-responder-wins act. Parent it
    to the main window; call :meth:`present` on each arrival and :meth:`reposition` on
    resize. Never a hard modal."""

    _DISMISS_MS = 7000

    def __init__(self, store: PendingInteractions, device_name=None, parent=None):
        super().__init__(parent)
        self.store = store
        self._device_name = device_name or (lambda did: did)
        self._pid = None
        self.setObjectName("RequestToast")
        self.setFrameShape(QFrame.StyledPanel)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(12, 10, 12, 10)
        self._lay.setSpacing(6)
        self._dismiss = QTimer(self)
        self._dismiss.setSingleShot(True)
        self._dismiss.timeout.connect(self.hide)
        self.hide()
        # if the shown prompt gets answered elsewhere, retire the toast
        store.changed.connect(self._on_changed)

    def present(self, prompt) -> None:
        """Show the banner for ``prompt`` (replacing any current one), answerable inline."""
        self._pid = prompt.id
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        accent = _SEVERITY_COLOR.get(prompt.severity, "#4dabf7")
        self.setStyleSheet(
            f"#RequestToast {{ background:#1b2130; border:2px solid {accent};"
            " border-radius:10px; }")
        title = QLabel((("🚨 " if prompt.is_critical else "⏳ ")
                        + self._device_name(prompt.device_id)))
        title.setStyleSheet("font-weight:700;")
        self._lay.addWidget(title)
        q = QLabel(prompt.title or prompt.question or "(no question)")
        q.setWordWrap(True)
        self._lay.addWidget(q)
        row = QHBoxLayout()
        row.setSpacing(4)
        for w in build_answer_controls(
                prompt, lambda answer, pid=prompt.id: self.store.resolve(
                    pid, answer, by="operator")):
            row.addWidget(w)
        row.addStretch(1)
        later = QToolButton()          # tuck it away NOW without answering (it stays in the inbox)
        later.setText("Later")
        later.setToolTip("Dismiss this banner — the request stays in the Requests inbox")
        later.clicked.connect(self.hide)
        row.addWidget(later)
        self._lay.addLayout(row)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        # routine prompts auto-tuck into the inbox; a critical one is sticky
        if prompt.is_critical:
            self._dismiss.stop()
        else:
            self._dismiss.start(self._DISMISS_MS)

    def reposition(self) -> None:
        """Anchor to the top-right of the parent, inset a little."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.move(max(0, parent.width() - self.width() - 24), 16)

    def _on_changed(self) -> None:
        # the currently-shown prompt was answered (by any surface) → drop the banner
        if self._pid is not None and self.store.get(self._pid) is None:
            self._pid = None
            self.hide()
