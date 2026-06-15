# ferroDAC — Roadmap & open decisions

[DESIGN.md](DESIGN.md) is the **north star** (the full ideal). This document is
the **scale-back**: the order we'd actually build it (smallest-useful-first), and
the decisions still open. This file is expected to churn; DESIGN.md is stable.

> Method (agreed): *design the best version, then scale back to a manageable
> scope.* So every phase below only **implements** a slice — the **slots** for
> the rest already exist in the design.

---

## Phasing (smallest-useful-first)

### Phase 0 — Unified local core (MVP)

The goal: replace **both** existing tools with one extensible app, at the bench,
no server, no auth, no remote.

- Core model: `Channel`, `Reading` (scalar only), catalog, the **ingest
  contract** (running against a local embedded hub).
- **Orchestrator** (basic spawn + supervise) + **driver SDK**.
- Two first drivers proving both tiers:
  - **Modbus temp** → pure **YAML** driver.
  - **TPG-256A** → **code** driver (and a stress-test of the YAML schema).
- **Qt app**: Explore + a single chart Workspace, reusing the TPG-256A ChartPanel
  (sci axis, secondary axis, notes, title, dark theme).
- **Record → folder**: one aggregated telemetry CSV + `meta.yaml`
  (auto-provenance) + `log.md` with annotations→notes. Append-only.
- Local-only (embedded hub); no waveform, video, auth, or remote.

*Exit criterion:* a bench user runs both instruments, builds a chart, hits
Record, and gets a portable project folder — using one app.

### Phase 1 — Persistence & docs polish

- Bounded-retention **live store**; **pre-roll** Record.
- **Workspace** save/load + file share; viewer-neutral JSON.
- Markdown docs + templates + passive **nudges**.
- **Python SDK** (`load_run`, `subscribe`) + **scaffolded notebook** per run.

### Phase 2 — Remote (read-mostly)

- Networked hub; subscribe over the network; **Qt remote mode** (a
  `RemoteSubscription` feed).
- **AuthN/AuthZ** v1: 3 roles + per-workspace sharing (Keycloak + OpenFGA/Keto).
- **Grafana** on the telemetry TSDB for zero-build read-only glances.
- Custom-source path documented (third parties implement the ingest contract).

### Phase 3 — Media

- Snapshots + recorded clips into `media/`; phone capture via web upload.
- Then **live video** (WebRTC via MediaMTX/LiveKit) as a video panel.

### Phase 4 — Waveform plane (only if the requirement is real)

- Block transport on the contract; **HDF5** records; **scope** renderer
  (decimated live). Triggered capture + pre-trigger.

### Phase 5 — Control

- Implement the reserved command path end-to-end: driver `commands()` →
  command bus → authz (`command` action / Operator role) → UI controls.
- Declarative **command grammar** for YAML drivers.

### Phase 6 — Scale & hardening

- Multi-station aggregation; deeper Nextcloud (WebDAV, sharing/versioning);
  packaging/installers; community driver library.

---

## Open decisions (with current leaning)

| # | Decision | Leaning | Notes |
|---|---|---|---|
| 1 | **Remote viewer tech** (Qt-primary vs web vs both) | Qt-primary + Grafana for read-only glances | Hard-tilts to Qt if the waveform plane is in scope. Deferrable — most swappable layer. |
| 2 | **Fast/waveform plane in scope?** | TBD — *needs the requirement* | How many fast channels, what rate, continuous-display vs triggered-capture, and which digitizer/SDK? Decides Phase 4's size (or removal). |
| 3 | **Multi-rate → one CSV aggregation rule** | forward-fill at a configurable record cadence | vs interpolate vs row-per-sample-with-blanks. |
| 4 | **What one Record captures** | the active dashboard's channels (with explicit add/remove) | one synchronized file per run. |
| 5 | **Live-store tech** | VictoriaMetrics or TimescaleDB (telemetry); HDF5 (waveform) | Swappable by definition (not the record). |
| 6 | **Identity provider** | Keycloak/OIDC, with local-accounts fallback | Doesn't block Phase 0–1. |
| 7 | **Nextcloud access** | folder-first (desktop sync) now; WebDAV later | WebDAV unlocks headless agents + sharing. |
| 8 | **YAML command grammar timing** | telemetry-only YAML now; add command grammar in Phase 5 | Code drivers can do commands earlier. |
| 9 | **Pre-roll default** | configurable per project; small fixed default (e.g. 60 s) | Determines hot-history retention. |
| 10 | **Repo / GitHub** | local for now (`/home/kali/ferroDAC`) | Offer: push as a private repo (as with the other two projects). |
| 11 | **License** | TBD | Business decision (Ferrovac). |
| 12 | **Name** | `ferroDAC` (working) | "Data Acquisition & Control". |

---

## Feature: embedded Python console / scripting (added 2026-06-12)

An in-app Python console + script editor where **every object has a generated
handle** (from its descriptor), so users can read data, set values, and inject
**middleware** into the live pipeline — analysis, data conditioning, or
closed-loop control (e.g. read a gauge, set a PSU) — in plain Python.

This is the **Explore/SDK surface made live**: the same facade should work in-app
(against the running engine) and standalone (against a recorded folder/stream).

How it maps onto the architecture (mostly falls out of existing pieces):
- **Handles** are generated from `SourceDescriptor.channels` / `.controls` /
  config — same self-describing mechanism as the config UI.
- **Setters** (`psu.voltage = 5`) route through `manager.invoke(...)` — already
  built.
- **Middleware** = user-registered **sinks** (read/condition) and **virtual
  sources** (emit derived signals) on the data plane; **closed-loop** = a sink
  that calls `invoke`.

Implications / constraints:
- **The data plane must expose a general `subscribe(sink)` + uniform emit path**
  (not a chart-only consumer) so scripts hook the same door. *(Honoured in the
  Phase-0 data-plane build.)*
- **Thread-safety:** user callbacks are delivered on the engine's drain thread,
  never the acquisition thread.
- **Security:** arbitrary Python = RCE. Local scripting is fine (it's the user's
  machine, like Jupyter); **remote** scripting must be gated by a `script` /
  `configure` permission and is off by default. Closed-loop actuation needs the
  `command` permission.
- **Persistence:** middleware scripts can later be saved with the project /
  workspace so they re-run.

Lands after the data plane + a viewer exist (roughly Phase 1–2), but the data
plane is being shaped now so it slots straight in.

## Decided: Device identity & resolution (2026-06-12)

Settled ahead of **workspace save/restore** (Phase 1), because the layout format
must address devices in a way that survives moving to another machine/user.

- **UUID per device, minted at onboarding**, persisted in a **registry**
  (`uuid ↔ fingerprint{driver, hardware_id}`) — local `registry.json` now, the
  **hub** later. `instance_id` = physical address; **UUID = data-plane identity**
  (Readings, routes, layouts key on it). See [DESIGN.md §6.1](DESIGN.md).
- **Resolver**: `UUID → local | remote(Phase 2) | unresolved`, reconciled on
  every discovery/hub event. The server is *only the second branch* — adding it
  never changes the layout format.
- **Disappearing devices**: desired routing (persisted) is decoupled from binding
  status (live). Absent device → greyed placeholder + NaN gaps, **never** dropped
  routes; **auto-rebinds** on return; manual re-bind available. Same mechanism
  for local-absent / remote-absent / unplugged-mid-session, and it makes
  save/restore and shared dashboards one code path.
- **Build order**: (1) registry + resolver (local branch) + resilient routes;
  (2) full-session save/restore to viewer-neutral JSON (+ Qt dock blob); (3) the
  remote branch in Phase 2.

## Decided: Record mechanics + markers/tags (2026-06-15)

MVP client features that double as on-ramps to the server data plane. See
[DESIGN.md §7.1](DESIGN.md).

- **Shared session time base** across panels (one clock) — unblocks tags,
  record markers, and the future replay timeline. Charts key x on a shared
  origin so vertical lines align across graphs.
- **One MarkerModel** holds event **tags** (timestamp + comment) and record
  **start/stop** — the same draggable vertical-marker primitive; every chart
  renders them (pyqtgraph `InfiniteLine`), **synced across all graphs**.
- **Record** = append-only raw capture (crash-safe, long format) + markers as a
  selection window; the wide `data.csv` is materialised at Stop (pre-roll
  backfill from a bounded always-on history buffer). Crash → recover the
  unfinalised capture on relaunch. Records all currently-routed sources.
- **On-ramp**: the history buffer is the data plane's hot tier; the raw capture
  is the precursor to the always-on persistence sink; markers become timeline
  cursors/jump-points. Tags → `log.md` annotations (§10).
- **Build order**: (1) shared clock + MarkerModel + synced chart tags + Events
  dock; (2) history buffer + Recorder + Record/Stop UI + recovery.

## Next: RGA / mass-spectrum modality — Pfeiffer Prisma QMS 200 (researched 2026-06-15)

The first **array-valued** source: a quadrupole RGA produces a *mass spectrum*
(intensity vs m/z), realising the waveform/spectrum modality of DESIGN §9/§11.

**Hardware**: Pfeiffer **Prisma QMS 200** (QMG electronics; `SQA` reports type 4),
RS-232-C (300…19200 baud, 8N1; LAN optional). *Not* the newer PrismaPlus QMG 220
(Ethernet/OPC).

**Protocol = the same ACK/ENQ framing as the TPG-256A** (Pfeiffer doc
BG 805 204 BE): `"MNEMONIC[,param]\r"` → `<ACK 0x06>` → `<ENQ 0x05>` → `"data\r\n"`.
First `CMO ,1` for ASCII/computer control. Our hardened TPG `_Link` (lstrip + retry)
transfers directly; factor a shared `_pfeiffer` link. Command set (clean-room from
the protocol; reference only: GPL CINF/PyExpLabSys `pfeiffer_qmg420/422.py`):
- identify/state: `SQA`, `CMO`, `ESQ` (16-bit), `ERR`/`EWN`
- filament/emission: `FIE ,1/0`, `EMI ,<cur>`; detector: `SEM`, `SHV`, `SDT`, `DTY`
- scan: `MMO ,0`, `MFM`=first mass, `MWI`=width, `MSD`=speed(s/amu), `MST`=steps/amu,
  `MRE`=resolution, range `AMO`/`ARA`/`ARL`, start `CRU ,2`
- read: poll `MBH` (field[3]=samples ready), read each point `MDB` → intensity array
- single-mass trend: `MMO ,3`, `MFM ,<mass>` → `MDB`

**Phased build:**
- **A — spectrum datatype + display (hardware-free).** `spectrum` dtype + a
  `Spectrum(mass, intensity)` Reading value; a `SpectrumPanel` (intensity vs m/z,
  log-y); a **simulated RGA** (Gaussian peaks at common masses) to prove the
  modality + routing without hardware.
- **B — QMS 200 driver (read).** Shared Pfeiffer ACK/ENQ link; cached serial
  discovery (identify via `SQA`); scan config as device **Options**; emit a
  Spectrum per scan. Validate on the real unit. Read-only first.
- **C — control sinks.** Filament on/off, emission current, SEM on/off, detector
  type. (Hardware action — never auto-actuated; user enables.)
- **D — trend channels + recording.** Selected-mass partial-pressure *scalar*
  sources (route to charts/record); spectrum recording to the folder (HDF5/block
  store per §11 — the waveform record plane).

## Explicitly deferred (designed, not built yet)

Control · multi-station · waveform/video planes · web viewer · auth · WebDAV ·
community driver library. All have **slots** in [DESIGN.md](DESIGN.md); none is
implemented before its phase.
