"""The Prompt entity — a device→app→device REQUEST/RESPONSE (DESIGN §7.3).

A **Prompt** is the fourth device↔app primitive, alongside the three that exist:

  * **source** — device→app data, fire-and-forget (a Reading);
  * **sink**   — app→device control, operator-initiated (a write);
  * **tag**    — device→app event, fire-and-forget (``emit_tag`` → a Marker).

A prompt is the missing shape: a **device-INITIATED request that needs a
correlated answer before the device proceeds** — "Have you retracted the arm?
[Yes]/[No]". It does not decompose into source+sink (a source can't block for an
answer; a sink is operator-, not device-, initiated), so it warrants a first-class
channel. It is the request/response analogue of the §7.3 device→tag emitter:
``BaseDevice.ask`` mirrors ``emit_tag`` — a platform-injected sink so drivers need
zero per-device plumbing.

This module is deliberately **Qt-free** (like ``core.tag``) so the net layer can
serialize prompts without importing a GUI toolkit. The Qt store that owns the live,
open prompts — the ``PendingInteractions`` model — lives in ``ui/interactions.py``
(the tag analogue of ``markers.py``'s ``TagStore``).
"""

from __future__ import annotations

import time
import uuid as _uuid
from dataclasses import dataclass, field

# kind — the SHAPE of the answer the operator gives (a small CLOSED enum). The
# store/UI auto-render the answer controls FROM this (confirm→[Yes]/[No],
# choice→option buttons, text→field+submit, acknowledge→[OK]), so a new device's
# new prompt renders with zero new UI code — the structured prompt IS the UI spec.
CONFIRM = "confirm"          # yes / no   → answer is a bool
CHOICE = "choice"            # one of `options`  → answer is an option string
TEXT = "text"                # freeform   → answer is a string
ACKNOWLEDGE = "acknowledge"  # just OK    → answer is True
KINDS = (CONFIRM, CHOICE, TEXT, ACKNOWLEDGE)

# severity — routine (info/warn) vs critical, borrowing the tag vocabulary. Routine
# prompts are quiet (toast + inbox); a CRITICAL prompt raises a sticky banner that
# never auto-dismisses and NEVER auto-resolves on timeout (it aborts or stays
# pending — never silently proceeds). See PendingInteractions._timed_out.
SEVERITIES = ("info", "warn", "critical")

# on_timeout — the policy when `timeout` elapses with no answer: abort the device's
# workflow, stay pending forever, or apply a default answer (any other value — a
# bool for confirm, an option for choice, a string for text). Critical prompts honour
# only ABORT/STAY (a default answer is refused for them — never silently proceed).
ABORT = "abort"
STAY = "stay"


@dataclass
class Prompt:
    """A device-initiated request awaiting an operator answer. JSON-serializable so
    it crosses the control-surface wire (and, later, the hub to a remote operator).

    A driver builds one on its poll/reader thread and hands it to ``BaseDevice.ask``
    with an ``on_response(answer)`` callback; the platform marshals it to the GUI
    thread, files it in the shared ``PendingInteractions`` store, and renders it in
    the Requests inbox. The FIRST responder (inbox / toast / control surface / a
    future hub operator) wins — resolving invokes ``on_response`` exactly once."""

    device_id: str                # the raising device's data_id (uuid, else instance_id)
    question: str = ""            # the full question ("Have you retracted the arm?")
    kind: str = CONFIRM           # confirm|choice|text|acknowledge (see KINDS)
    id: str = ""                  # UUID hex — correlates the answer back; auto if blank
    title: str = ""               # optional short headline (else the UI names the device)
    options: list = field(default_factory=list)   # for kind=choice — the answerable options
    severity: str = "info"        # info|warn|critical (see SEVERITIES)
    timeout: float | None = None  # seconds before on_timeout fires (None = wait forever)
    on_timeout: object = STAY     # "abort" | "stay" | a default answer (see ABORT/STAY)
    created: float = 0.0          # epoch seconds it was raised (auto if 0)

    def __post_init__(self):
        # A driver may leave id/created blank for ergonomics — mint them once here so
        # every prompt is correlatable and time-stamped without boilerplate per driver.
        if not self.id:
            self.id = _uuid.uuid4().hex
        if not self.created:
            self.created = time.time()

    @property
    def is_critical(self) -> bool:
        return self.severity == "critical"


def prompt_to_dict(p: Prompt) -> dict:
    return {"id": p.id, "device_id": p.device_id, "question": p.question,
            "kind": p.kind, "title": p.title, "options": list(p.options),
            "severity": p.severity, "timeout": p.timeout,
            "on_timeout": p.on_timeout, "created": p.created}


def prompt_from_dict(d: dict) -> "Prompt | None":
    if not d or d.get("device_id") is None:
        return None
    return Prompt(
        device_id=str(d["device_id"]), question=d.get("question", ""),
        kind=d.get("kind", CONFIRM), id=d.get("id", ""), title=d.get("title", ""),
        options=list(d.get("options") or []), severity=d.get("severity", "info"),
        timeout=d.get("timeout"), on_timeout=d.get("on_timeout", STAY),
        created=float(d.get("created", 0.0)))
