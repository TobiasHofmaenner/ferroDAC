# The interaction channel — device→app→device requests

A complex device sometimes needs to **ask the operator something mid-workflow and
get an answer back before it proceeds** — "Have you retracted the arm? [Yes]/[No]",
"Which detector is installed? [FC]/[SEM]", "Enter the sample id". This document
describes the primitive that carries that exchange, and why it is first-class.

## Why it isn't source / sink / tag

ferroDAC already has three device↔app primitives, and this is genuinely a fourth:

| primitive | direction | who starts it | correlated answer? |
|-----------|-----------|---------------|--------------------|
| **source** | device→app | device | no (fire-and-forget data) |
| **sink** | app→device | **operator** | no (a control write) |
| **tag** (`emit_tag`) | device→app | device | no (fire-and-forget event) |
| **prompt** (`ask`) | **device→app→device** | **device** | **yes** |

A prompt is **device-initiated** (a sink is operator-initiated) and **needs a
correlated answer routed back to the device** (a source/tag are fire-and-forget). It
does not decompose into source + sink: a source can't block for an answer, and a sink
is the wrong initiator. So it warrants its own channel rather than a convention bolted
onto the other three.

## Architecture — an exact mirror of the `emit_tag` channel

The device→tag emitter (DESIGN §7.3) is the precedent: a **platform-injected**
device→app channel, so a driver needs zero per-device plumbing. The interaction
channel mirrors it part-for-part:

| device→tag (§7.3) | device→prompt (this) |
|-------------------|----------------------|
| `BaseDevice.emit_tag(marker)` | `BaseDevice.ask(prompt, on_response)` |
| `BaseDevice.set_tag_sink(sink)` (platform-injected, no-op until wired) | `BaseDevice.set_prompt_sink(sink)` (same) |
| `DeviceManager.device_tag = Signal(object)` | `DeviceManager.device_prompt = Signal(object, object)` — `(Prompt, on_response)` |
| injected in `DeviceManager._wire_tags` on add / add_user_device | injected in the same `_wire_tags` |
| `app._on_device_tag` via `QueuedConnection` → `markers.upsert` | `app._on_device_prompt` via `QueuedConnection` → `PendingInteractions.add` |
| store: `MarkerModel` (`markers.py`) | store: `PendingInteractions` (`ui/interactions.py`) |

- **`Prompt`** (`core/interaction.py`) is a **Qt-free**, JSON-serializable dataclass
  (like `core.tag`): `id`, `device_id`, `title`/`question`, `kind`
  (`confirm` | `choice` | `text` | `acknowledge`), `options` (for `choice`),
  `severity` (`info` | `warn` | `critical`), an optional `timeout` + `on_timeout`
  policy (`abort` | `stay` | a default answer), and a created time. It is Qt-free so
  the control surface (and, later, the hub) can serialize it without a GUI toolkit.
- **Threading**: a prompt may be raised from a device poll/reader thread. The manager
  `Signal` + `QueuedConnection` marshals `(prompt, on_response)` onto the GUI thread,
  exactly like `device_tag`. `on_response` is then invoked on the GUI thread when the
  operator answers, so a driver must make it GUI-safe (schedule any blocking hardware
  act off-thread itself — e.g. via `manager.write`).

## The answering model — an inbox, not a modal

Answering is deliberately **non-blocking** (no hard app-modal that freezes the app):

- **One shared store, first-responder-wins.** `PendingInteractions` is the single
  source of truth the inbox, the arrival toast, the control surface and (future) the
  hub all read + resolve. `resolve(id, answer, by=…)` invokes the stored `on_response`
  **exactly once**, records **who** answered + **when**, drops the prompt, and signals.
  A second answer (a race between two surfaces) is a no-op.
- **A persistent Requests inbox** (its own dock, tabbed with Events) with an
  always-visible **pending count** in the dock title.
- **Answer controls AUTO-GENERATED from `kind`** — `confirm`→[Yes]/[No],
  `choice`→one button per option, `text`→field+submit, `acknowledge`→[OK]. The
  structured prompt **is** the UI spec, so a new device's new prompt renders with zero
  new UI code (`build_answer_controls`).
- **A non-blocking arrival toast** (answerable inline) that tucks into the inbox after
  a few seconds.
- **The raising device shows "⏳ awaiting operator"** on its device card while a
  request is open.
- **Severity.** Routine (`info`/`warn`) → quiet toast + inbox. **`critical` → a sticky
  banner that never auto-dismisses** (highlighted), never a hard modal — and critical
  prompts **never auto-resolve on timeout**: they abort or stay pending, never silently
  proceed.
- **Provenance auto-tag.** On every resolve the app drops an `origin=device`,
  immutable timeline tag recording the outcome and who answered ("↩ Yes — answered by
  operator"), so the interaction is a durable fact on the shared clock.

## Answerable by an agent (and, later, a remote operator)

Two control-surface verbs make prompts answerable by an LLM/agent over the localapi,
through the same store (so it is still first-responder-wins):

- **`device.prompts`** (read scope) — list open requests as JSON, each with the `kind`
  that says how to answer.
- **`device.respond {id, answer}`** (control scope) — resolve one. `answer` matches the
  kind (bool for confirm, an option for choice, a string for text, `true` for
  acknowledge).

## Deliberately deferred

- **Hub relay of prompts to a remote operator.** The store, the Qt-free `Prompt`, and
  the verbs were all built so a remote operator can answer over the hub, but the actual
  hub relay (publishing open prompts and routing an answer back to the owning client) is
  a follow-up — the same shape as the §5.3 remote-command leg.
- Toast/banner visual polish (positioning, stacking multiple arrivals) is intentionally
  minimal; the surface is functional and offscreen-constructible for review.
