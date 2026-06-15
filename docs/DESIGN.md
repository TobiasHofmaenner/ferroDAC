# ferroDAC — North-Star Design

> This document describes the **ideal, complete** system. It is intentionally
> bigger than any first release. The deliberate scope-reduction into an MVP and
> phases lives in [ROADMAP.md](ROADMAP.md). Decisions still open are tracked
> there too; this document records what we've *agreed*.

---

## 1. Purpose & vision

ferroDAC is a **local-first, plain-files lab data-acquisition + electronic
lab-notebook platform**. One tool that:

- talks to **any instrument** in its library (vacuum gauges, temperature
  modules, power supplies, cameras, digitizers …);
- **streams** all of it live and lets you build **customizable dashboards**;
- **records** experiments, on demand, into a **portable project folder** that
  survives the tool;
- makes **documenting** an experiment easy and passively nudges the user toward
  good provenance;
- can run **standalone at the bench** or **stream to a server** for remote
  viewing;
- meets physicists where they are — analysis is **Python-native**.

It supersedes two single-purpose apps (TPG-256A gauge monitor, Modbus temp
monitor); each becomes simply a *driver* in the library.

---

## 2. Guiding principles (the invariants)

1. **The folder is the system of record — for captures.** Anything *recorded*
   lives in a portable project folder in open, self-describing formats. It must
   be fully usable in 10 years with no ferroDAC present.
2. **Two planes, two rule-sets.** The always-on **live** plane may use any
   performant/proprietary tech (it is replaceable and *not* the record). The
   **Record** plane is durable and open-format only. *Lock-in is forbidden only
   in the Record plane.*
3. **Local-first; remote is additive.** A bench rig works fully with no server
   and no network. The server is an upgrade (live sharing, aggregation), never a
   dependency.
4. **One universal boundary: the ingest contract.** Every data source — curated
   or custom, local or remote — reaches the hub through the same documented,
   versioned, language-neutral contract.
5. **Self-describing, extensible drivers.** A device is added via a declarative
   YAML description (structured protocols) or a code driver (everything else).
   The platform *generates* UI forms, the catalog, and authz checks from what a
   driver declares.
6. **Design every slot; build incrementally.** Control, multi-station,
   waveforms, and video are *designed for now*, implemented later. Slots are
   cheap; retrofits are expensive.
7. **Open formats over clever formats.** Text (CSV/TSV) for low-rate tabular
   data; HDF5/Parquet for high-rate arrays; standard containers (mp4/H.264,
   JPEG) for media. No proprietary record formats, ever.

---

## 3. Glossary

| Term | Meaning |
|---|---|
| **Source** | A producer of data with one or more channels. A device, a camera, a custom process. |
| **Channel** | A single addressable signal from a source, with a stable global ID, a unit, and a **modality**. |
| **Modality** | `scalar` (point/timeseries) · `waveform` (block/array) · `image` · `video`. |
| **Reading / Sample** | One datum on a channel (a point, a block, a frame …) with a timestamp. |
| **Driver** | The code/description that makes an instrument speak the ingest contract. YAML or code. |
| **Orchestrator** | A portable supervisor that spawns & manages **curated** driver processes on a station. Not in the data path. |
| **Station** | A machine running an orchestrator + sources (e.g. the PC next to a rig). |
| **Hub / Server** | The aggregation point: live store, catalog, subscribe, workspace store, auth. Local-at-minimum; shared ⇒ remote. |
| **Project** | A directory = the durable record of an experiment (Nextcloud-synced). |
| **Run** | One Record session → a subfolder of a project. |
| **Record** | The on-demand action that materialises a slice of the live stream into the project folder. |
| **Workspace** | A serializable dashboard layout (panels + channel/axis assignments). References channels by ID; carries no data. |
| **Catalog** | The live registry of stations/devices/channels and their capabilities. |
| **Principal** | An authenticated identity: a user, an agent/station, or a service token. |

---

## 4. Architecture at a glance

```
                          ┌─────────────────────────── clients ───────────────────────────┐
                          │ Qt app (bench ⊕ agent ⊕ viewer ⊕ analysis)  ·  Grafana (RO)    │
                          │ Python SDK / notebooks                                          │
                          └──────▲──────────────────────▲────────────────────────▲─────────┘
                                 │ subscribe (ws/gRPC)   │ catalog                 │ media (WebRTC/HLS)
  ┌──────────────── HUB / SERVER (local-at-minimum; shared = remote) ─────────────┴────────┐
  │  Catalog · Live store (telemetry TSDB | waveform blocks; bounded retention)             │
  │  Subscribe/stream · Workspace store · [Command bus — reserved]                          │
  │  AuthN (OIDC) · AuthZ (tuples: principal × action × resource)                           │
  └──────▲───────────────────────────────────────────────────────────────────────▲─────────┘
         │  INGEST CONTRACT  (gRPC .proto: auth · describe · stream · command)     │ (same contract)
  ┌──────┴──────── Orchestrator (portable supervisor) ──────────┐           ┌──────┴───────┐
  │  spawn · configure · supervise CURATED sources              │           │ CUSTOM source │
  │   ┌───────────┐  ┌───────────┐  ┌───────────┐               │           │ (your process,│
  │   │YAML driver│  │YAML driver│  │code driver│  …            │           │ self-managed) │
  │   └────┬──────┘  └────┬──────┘  └────┬──────┘               │           └──────┬───────┘
  │        └──── shared transport lib (RS232/RS485/Modbus/TCP/SCPI/…) ────┘        │
  └────────┼──────────────┼──────────────┼──────────────────────────────┘         │
       ┌───┴──┐       ┌───┴──┐       ┌────┴───┐                              ┌─────┴────┐
       │device│       │device│       │ camera │  …                           │  exotic  │
       └──────┘       └──────┘       └────────┘                              └──────────┘

   RECORD (on demand) ───────────────► Project folder (Nextcloud, open files) ◄─ system of record
```

Two orthogonal "planes" run across this:

- **Data plane** — sources → hub → consumers (live). Performant, replaceable.
- **Lifecycle plane** — the orchestrator spawns/supervises curated sources. Not
  in the data path.

And two **data regimes** (see §11): **telemetry** (scalar, ≤~kHz) and
**waveform** (blocks, kHz–GHz) — different pipelines, chosen by modality.

---

## 5. HAL / driver layer

### 5.1 Out-of-process driver servers + orchestrator

Each source runs as its **own process** (a *driver server*). This is the
proven model of large control systems (EPICS IOCs, Tango device servers, ROS
nodes). Benefits: **fault isolation** (a hung serial read or a segfaulting
vendor DLL kills only that device), **hot-plug lifecycle**, **language
independence** (Python/Rust/C++), **distribution** (a driver can run on a Pi
next to the instrument), and **no GIL contention**.

The **orchestrator** is a *portable supervisor* (single implementation, runs on
Windows and Linux; does its own child-process spawn + heartbeat + restart — **no
systemd / no Windows SCM**). It spawns curated sources, hands them config +
hub address + a scoped token, and watches their health. **It is not in the data
path.**

### 5.2 Curated vs custom sources — one contract

- **Curated** sources are the vetted library (YAML + code drivers) the
  orchestrator knows how to run.
- **Custom** sources are any process that speaks the ingest contract and
  authenticates with a `publish`-scoped token. They bypass the orchestrator and
  talk to the hub directly; they self-supervise.

The only difference is *who manages lifecycle*. The **data path is identical**,
and both appear identically in the catalog once publishing.

### 5.3 The ingest contract (the universal boundary)

A documented, **versioned, language-neutral** API (canonical form: a gRPC
`.proto`), implemented by every source:

```
Describe()        -> capabilities: channels, commands, config schema   (drives UI/catalog/authz)
Configure(cfg)    -> validate + apply
Start() / Stop()
StreamReadings()  -> server-stream of timestamped samples (scalar/waveform blocks)
InvokeCommand(c)  -> ack/result            # RESERVED — enables control later
Health()          -> liveness / last error
```

`Describe()` makes drivers **self-describing**: UI "add device" forms, the
catalog, and permission checks are all generated from it. For a **camera**,
`Describe()` advertises a WebRTC/RTSP endpoint instead of streaming frames over
gRPC — high-bandwidth media stays on its own plane (§9); the contract carries
metadata + control only.

### 5.4 Two tiers of driver authorship

1. **Declarative (YAML).** A generic interpreter driver-server loads a YAML
   device description, uses the shared transport lib, and serves the contract —
   **no code**. Excellent for structured families (Modbus, SCPI, simple
   request/response ASCII).

   ```yaml
   device: modbus_temp_rtu
   transport:
     kind: modbus_rtu
     params: {port: {ui: true}, baudrate: {default: 9600, ui: true}, slave_id: {default: 1, ui: true}}
   probe: {read: {register: 0x0000, type: int16}}        # answers ⇒ present
   channels:
     - {id: temp, name: Temperature, unit: "°C",
        read: {register: 0x0000, type: int16}, scale: 0.1, poll_hz: 5}
   commands: []          # reserved for control
   ```

   The transport binding's `ui: true` params auto-generate the "Add device"
   form. **Discipline:** decoding is typed (int/float/bitfield/endianness) and
   scaling is a **constrained, safe arithmetic expression** only — *no
   scripting*. The moment a device needs real logic, it becomes a code driver.
   (The TPG-256A's stateful `mnemonic → ACK → ENQ → parse` protocol is the
   schema's stress test: if the YAML can express that transaction sequence it
   covers ~90% of serial instruments; if not, the TPG stays a code driver.)

2. **Code (SDK).** Implement the same contract directly (Python/Rust/C++) for
   the hard cases: stateful protocols, vendor SDKs/DLLs, binary framing, cameras,
   digitizers.

Both tiers sit on a **shared transport lib** (RS232, RS485, Modbus RTU/TCP,
TCP/IP; later SCPI/VISA). Build that once.

The **library** of YAML defs + code drivers is itself a shareable, versioned
artifact — "add any module as long as it's in the library."

---

## 6. Data model

- **Channel IDs** are hierarchical and human-readable, e.g.
  `station/device/channel` (`rig-1/tpg256a/ch3`), backed by a UUID so renames
  don't break references. **Multi-station namespacing from day one.**
- **Reading/Sample** is modality-tagged:
  - `scalar`  → `(t, value, unit, status)`
  - `trace`   → `(t, x[], y[], x_unit, y_unit, status)` — a spectrum/curve with
    an explicit (possibly non-uniform, possibly *changing*) x-axis, e.g. RGA m/z
  - `waveform`→ `(t0, dt, array, unit, status)` (a block, not N points)
  - `image`   → reference to a stored frame
  - `video`   → reference to a stream/segment
- The **catalog** publishes the live tree of stations/devices/channels and their
  declared capabilities (channels + commands + config), sourced from
  `Describe()`.

### 6.1 Device identity & resolution (decided 2026-06-12)

The UUID above is the load-bearing identity. It makes layouts portable and is
the same key whether a device is local or remote.

- **Identity = UUID, minted at onboarding**, the first time a user *adds* a
  device. Hardware can't carry our UUID (a UVC webcam only has its vendor
  descriptor), so the UUID lives on a **registry record**, not the device:
  `{ uuid, friendly_name, fingerprint:{driver, hardware_id} }`. The registry is
  the **UUID ↔ hardware** bridge — a local `registry.json` now, the **hub** in
  the networked phase. `instance_id` stays a *physical address* (how the driver
  reaches hardware, e.g. `/dev/video0`); the **UUID is the data-plane identity**
  (Readings, routes, layouts all key on it). Only things *we* mint get UUIDs;
  endpoints are addressed compositionally as `(device-uuid, source/sink-id)`.
- **Resolution** maps a referenced UUID → a concrete data source, reconciled
  continuously on every discovery tick / hub event:
  `local registry match → bind LOCAL` · *(later)* `hub online → bind REMOTE` ·
  `else → UNRESOLVED placeholder`.
- **Disappearance is not deletion.** *Desired routing* (declarative, persisted)
  is decoupled from *binding status* (live, reactive). A referenced-but-absent
  device — never added, not on the server, or unplugged mid-session — keeps its
  slot as a greyed **placeholder**; its sources emit **NaN (a visible gap, never
  a frozen line)**; it **auto-rebinds** when the same UUID reappears; the user
  can **manually re-bind** a slot to a different device. One mechanism covers
  local-absent, remote-absent, and vanished-mid-session — and makes
  save/restore and shared dashboards the *same* code path.

---

## 7. The two planes: Live & Record

| | **Live plane** (always on) | **Record** (on demand) |
|---|---|---|
| When | the DAQ chain is always streaming | only when the user hits **Record** |
| Tech | anything performant — binary wire, TSDB, ring/block buffers | open formats in the **folder** |
| Durability | ephemeral / bounded retention | permanent (Nextcloud, 10-yr) |
| Lock-in | fine (replaceable, not the record) | forbidden |

- **Record materialises a slice** of the live stream into a `runs/<ts>/` folder.
  Live data nobody records simply never touches the folder.
- **Pre-trigger / pre-roll (configurable).** Because the stream is always
  buffered, Record reaches *backwards*: capture the last *N* seconds before you
  pressed it (like a scope's hardware pre-trigger or a dashcam). Same for video.
- **Crash-safe.** Record **appends** to the file as it goes; a power cut leaves a
  valid partial file. (One-writer-per-run + append-only also makes Nextcloud
  sync conflict-free.)
- **One aggregated file per run, shared timeline** (telemetry → CSV; waveform →
  HDF5), so analysis is easy. Multi-rate sources are aligned to a configurable
  record cadence (aggregation rule is an open decision — leaning forward-fill).
- **Record always writes to the project folder** — even when triggered by a
  remote viewer, it writes to the acquiring station's project (which syncs to
  Nextcloud for everyone). A separate **Export** is for personal local slices.
- **Wire format ≠ storage format.** The live wire may be binary/msgpack; the
  durable record is always open files.

### 7.1 Record mechanics & the marker model (decided 2026-06-15)

Record separates **capture** (always-on, crash-safe) from **selection** (movable
markers) — so dragging a marker never re-serialises a long run.

- **Capture = append-only raw file**, opened the instant you hit Record:
  long format `t, uuid, source, value, status`, one row per reading. Append-only
  ⇒ a crash leaves every recorded reading intact and nothing is ever rewritten.
  A `_recording.json` sidecar (raw path, capture set, start time) lets a relaunch
  detect an unfinalised capture and recover it.
- **Two markers are a *selection window*, not a re-serialise trigger.** The clean
  **wide `data.csv`** (columns-per-source, §8) is **materialised once at Stop**
  by slicing the raw capture to `[start, stop]`; dragging *start* before the
  press backfills from the always-on bounded **history buffer** (the pre-roll).
- **Evolution:** once the persistence store exists, the live store is *always*
  capturing, so Record stops needing its own file — it becomes two bookmarks over
  the store and `data.csv` becomes a query-export. The marker UI is unchanged;
  only the backing store graduates (per-record file → TSDB).
- **Markers/tags are one primitive on a shared session time base.** Event **tags**
  (timestamp + comment) and record **start/stop** are the same draggable vertical
  time-marker, held in one `MarkerModel`; every chart renders them (pyqtgraph
  `InfiniteLine`) against a single session clock, so they stay **synced across all
  graphs**. Tags are session/run metadata: saved with the layout, auto-appended to
  `log.md` (§10), and (later) jump-to points on the replay timeline.

### 7.2 Historic plane: reimport, replay & the query interface (decided 2026-06-15)

**Invariant: everything we produce is reimportable.** A capture is never a dead
export — any recorded run can be brought *back into the running app* with its
original signature intact, so the same layouts, routes and analysis pipelines
rebind to it untouched.

- **Two tiers, by the operator's intent.** *Ambient* (un-recorded) live data is
  performance-format, retention-bounded, **expendable** — it feeds live charts and
  scroll-back and may be lost. *Recorded* captures are the sacred, self-describing,
  **pinned** tier (§8). Small captures (gauges, RGA) are materialised in full on
  Stop, so "always recoverable on restart" holds transparently and forever; a
  genuine firehose stays ambient and is kept only via deliberate **Export**.

- **Reimport / replay is a playback *Device*, not a bespoke loader.** A capture is
  opened as a **`FileDevice`** whose Sources are the captured channels,
  **reclaiming the original `(uuid, source-id)` keys** from the run manifest.
  Because identity matches, the §6.1 placeholder **auto-rebind** is the entire
  mechanism: load the file → the run's greyed ports come online → every route,
  processor and panel bound to them lights up with replayed data, **zero
  reconfiguration**. One component is the offline reimporter, the restart-recovery
  loader, *and* the replay input; the server mirror is *historic-query-as-a-source*.
  (A bare, manifest-less CSV still imports, but as a lower-fidelity *new* device
  whose columns are mapped by hand.)

- **Replay runs in its own clock.** Nothing in the data path reads wall-time
  directly: a `SessionClock` (live) and a `PlaybackClock` (driven by the file's
  timestamps) implement one interface. A loaded capture plays in its **own
  context**, so historic timestamps never collide with live data (or a still-
  connected source of the same UUID). v1 is **bulk-load-and-render** (emit rows in
  order; charts repopulate, processors fire once per scan); scrub / play-at-speed
  is a later use of the same clock seam.

- **Store raw, recompute derived.** Only measured series are truth; processor
  outputs (gas composition, cursors, normalisation) are **never persisted as
  data** — they are re-derived by replaying raw through the pipeline. The pipeline
  config travels in the run manifest, so replay offers *as-recorded* vs
  *with-current-pipeline*. ⇒ Replaying the **analysis** pipeline requires the raw
  **traces** in the capture (§8 format rule), not just the scalar outputs.

- **One windowed, resolution-aware query interface** is the only way panels read
  data, live or historic: `query(series, t0, t1, max_points)` returning **min/max
  envelope buckets** (so peaks survive downsampling) + `subscribe(series)` for the
  live tail. Live is just the special case "window = follow the tail." The same
  call powers live charts, historic navigation, and feature-hunting a downsampled
  firehose streamed back from the server. Backed by the RAM ring + per-run files
  now; VictoriaMetrics + object store later — panels don't change.

- **Autoscale is viewport-scoped.** Y auto-ranges over the data **inside the
  current X window**, never all of history — so a week-old capture loading on
  restart can't squash the view. The live chart holds only its bounded tail;
  recorded runs are *not* poured into it — they open on demand (region zoom / the
  time navigator), which sets the X window and queries that range at display
  resolution.

---

## 8. Project folder = system of record

A **project is a directory** (lives in a Nextcloud-synced folder), self-describing:

```
MyExperiment/
  project.yaml      manifest: title, people, created, FORMAT version
  README.md         the experiment writeup (markdown)
  FORMAT.md         one page describing this layout (the 10-year insurance)
  notes/            extra markdown docs (methods, results…)
  runs/
    2026-06-12T14-03_run1/
      data.csv        telemetry, shared timeline — header carries channels+units
      data.h5         (waveform captures, if any)
      meta.yaml       provenance (see below)
      log.md          run notes + auto-logged chart annotations
      media/  setup_001.jpg  clip_001.mp4
  media/            project-level photos/video
  workspaces/       UI layouts (JSON — exempt from the human-readable rule)
  .daq/             regenerable index/cache (safe to delete)
```

- **Auto-provenance** (`meta.yaml`, written automatically): device serials,
  **firmware** (we already read the TPG's `PNR`), driver versions, full config,
  sampling rates, operator, software version, start/stop times. The best
  documentation is the boring part done for you.
- **Format rule:** text for low-rate tabular; **traces/spectra as a 2-D CSV
  matrix** — a timestamp column + one column per x-bin, the **header row carrying
  the x-axis** (self-describing and reimportable; a *changed* axis starts a new
  segment); HDF5/Parquet reserved for genuine high-rate waveform/firehose arrays;
  standard containers for media. The invariant is *open + self-describing +
  reimportable*, not literally "everything is text".
- **Nextcloud:** folder-first via the desktop sync client now (zero special
  integration); optional **WebDAV** later (headless agents, sharing, versioning).

---

## 9. Multimodal sources & the media plane

A camera and a gauge are both "sources", differing only by **modality**. Media
is a **separate transport plane** but shares the catalog, permissions, and
workspaces — a video feed is "just another source" you drop into a dashboard
tile.

| Modality | Live transport | Durable artifact | Panel |
|---|---|---|---|
| scalar timeseries | WebSocket (batched) | `data.csv` | chart |
| waveform/block | WebSocket (blocks) | `data.h5` | scope |
| image / snapshot | HTTP upload | `media/*.jpg` | image tile |
| video feed | **WebRTC** / HLS | `media/*.mp4` | video tile |

Media sources: built-in webcam, USB/IP cameras (RTSP), a **phone via the web
client** ("take photo" → uploads into `media/` and drops a timestamped reference
into `log.md`), or a dashboard screenshot. Video is the heaviest dependency and
is **staged**: snapshots + recorded clips first, live WebRTC later. Because media
is timestamped against the run clock, scrubbing the chart can surface
time-correlated photos/frames.

---

## 10. Documentation / ELN behaviours

The goal: **automate the boring, ask only for the "why".**

- **Auto-provenance** (§8) — zero-effort metadata capture.
- **Annotations → notes** — every vertical **note marker** dropped on a chart
  (a feature already built in TPG-256A) auto-appends a timestamped line to the
  run's `log.md`. Your documentation writes itself as you work.
- **Templates** — a new run starts from a markdown template (Objective / Setup /
  Procedure / Observations / Results).
- **Passive nudges** — gentle, dismissible, never blocking: "no description",
  "setpoint changed at t=120 s but no note", "no setup photo yet".
- **Time-correlated media** — photos/clips on the run timeline.

---

## 11. Performance & data regimes

The observability stack (Prometheus/VM + Grafana) fits **telemetry** and is the
wrong tool for **waveforms** — a data-model mismatch, not a tuning problem.

| | Telemetry / slow control | Waveform / signal |
|---|---|---|
| Examples | pressure, temp, setpoints | scope traces, ADC streams, RF, pulses |
| Rate | 0.01 Hz – ~1 kHz | kHz – GHz |
| Datum | a point `(t, value)` | a block `(t0, dt, N samples)` |
| Live store | TSDB | block store (HDF5/binary) |
| Display | metrics panel / Grafana | **decimated scope view** |

**"Sample at 1 MHz and display live?"** Yes, but *not* through a metrics stack,
and "display" is always a **decimated** min/max-envelope view (a 4K screen is
~4000 px wide — you never draw 1 M points/s). The instrumentation pipeline:
**acquire in blocks → reduce near the source for live (decimate/envelope/FFT) →
render with a streaming plotter (pyqtgraph / WebGL) → record full-rate to a block
store on trigger.** This maps exactly onto Record + pre-roll (digitizers have
hardware pre-trigger). Prior art: GNU Radio, HDF5/areaDetector, sigrok,
pyqtgraph.

Consequence: **the telemetry and waveform planes are different pipelines; the
source modality decides which.**

> First realization of the array/waveform modality: an **RGA mass spectrum**
> (intensity vs m/z) from a Pfeiffer Prisma QMS 200 — see the plan in
> [ROADMAP.md](ROADMAP.md). A spectrum source emits a whole array per scan and
> routes (datatype-gated) to a spectrum panel, not a time-series chart.

---

## 12. Comms / server / transport

- **The hub always exists** — locally at minimum (embedded on the bench machine;
  packaging hides it so a solo user never sees it). "Remote" is just a shared
  hub over the network. One uniform data path, no special in-process case.
- **Ingest** (source → hub): the gRPC contract (§5.3).
- **Subscribe/stream** (hub → clients): gRPC for native clients; **WebSocket**
  for browsers. (WebSocket's bidirectionality also carries the reserved command
  path.)
- **Catalog API**, **Workspace store**, **Command bus (reserved)**.
- **Live store:** reuse — a TSDB (TimescaleDB/VictoriaMetrics/InfluxDB) for
  telemetry; a block store (HDF5) for waveforms. **Bounded retention** — the hot
  store is for live + recent scrubbing; anything worth keeping is *recorded* to
  the folder.

---

## 13. Permissions & auth

Resource-scoped RBAC expressed as **`(principal, action, resource)`** grants —
the tuple model reads as "3 simple roles" today and scales to fine-grained
relationship-based authz (Zanzibar-style: OpenFGA / Ory Keto) with **no schema
change**.

- **Principals:** users (OIDC/SSO or local), **agents/stations** (a rig
  authenticates as itself to publish), **service tokens** (SDK/notebooks).
- **Resources (hierarchical, perms inherit):** Station → Device → Channel; and
  **Workspace** (a first-class owned/shared resource).
- **Actions:** data → `read` · `subscribe` · `command` · `publish` ·
  `configure`; workspace → `view` · `edit` · `share` · `delete`; system →
  `admin`.
- **Control is reserved:** `command` is a real action, simply ungranted until the
  control phase.
- **v1 pragmatics:** ship 3 roles (Viewer = read+subscribe, Operator = +command,
  Admin) + per-workspace sharing (private / shared-with / org-public).
- **Reuse:** AuthN via Keycloak/OIDC; AuthZ tuples via OpenFGA or Ory Keto.

---

## 14. UI layer

Two surfaces, one built on the other:

- **Explore / Sources (raw).** Browse the catalog, subscribe to channels, do
  ad-hoc analysis, export. This is also exactly what the **Python SDK / notebook**
  hits. Power-user, bring-your-own-analysis.
- **Workspaces (curated).** User-created, shareable dashboards that *reference*
  sources by ID — owned, versioned, shareable. **A workspace is just a saved
  recipe of what you could assemble in Explore**, so it's not a second system.

**Data model vs view model** are separated: channels have stable IDs; a
**Workspace** is a serializable JSON document of panels + (channel → axis)
assignments + styling + notes. Because it references channels by ID only, it is
**viewer-neutral and shareable** — store workspaces on the hub (pull "the rig-1
dashboard" by name) plus file export/import.

Panel types map to modalities: **chart · scope · image · video · markdown**. The
existing TPG-256A chart (scientific-notation axis, secondary axis, draggable note
markers, settable title, dark theme) becomes the reusable **ChartPanel**.

**Primary client: Qt (PySide6 + pyqtgraph).** One app is simultaneously bench
tool ⊕ publishing agent ⊕ remote viewer (over the network) ⊕ analysis launcher.
This is the right call for the fast/waveform plane (a browser can't do a MHz
scope view; pyqtgraph can) and keeps everything one stack.

**Remote viewer is the most swappable, most deferrable layer.** The load-bearing
parts are the folder-as-record, the ingest contract, the catalog, and the
drivers. Since the workspace format is viewer-neutral, a web viewer can be added
later without touching the core. **Grafana pointed at the telemetry TSDB** is a
near-zero-cost, read-only "glance from your phone" window for the casual remote
case — assembled, not built. (See ROADMAP for the open viewer decision.)

---

## 15. Analysis / SDK (Python-native)

Analysis runs on the **recorded folder**, so it is independent of the live-viewer
choice. Provide:

- A **Python client lib**: `run = ferrodac.load_run("…/run1")` → a tidy object
  (pandas/xarray) with channels, units, events, media — one line to load. The
  same lib can `subscribe(...)` to the live stream for scripted/automated
  analysis (the Explore/SDK surface).
- A **scaffolded notebook per run**: on record-finish, drop an `analysis.ipynb`
  into the run folder, pre-wired to *that* run (paths, channel names, units from
  `meta.yaml`) with starter cells. The physicist double-clicks; it just works.

---

## 16. Reuse vs build

**Reuse the heavy, solved infrastructure; build the novel glue.**

| Concern | Reuse (OSS) | Verdict |
|---|---|---|
| Live store (TSDB) | TimescaleDB / VictoriaMetrics / InfluxDB | reuse |
| Ingest bus / fan-in | NATS or MQTT | reuse |
| Source↔hub contract | gRPC | reuse framework, define schema |
| Remote dashboard (telemetry) | Grafana | reuse (read-only glances) |
| Video plane | MediaMTX / LiveKit (WebRTC) | reuse |
| AuthN | Keycloak (OIDC) | reuse |
| AuthZ (fine-grained) | OpenFGA / Ory Keto (Zanzibar tuples) | reuse |
| Project store / sync | Nextcloud (WebDAV) + DataLad/git-annex | reuse |
| Driver code to mine | PyMeasure · QCoDeS · InstrumentKit · Telegraf | mine |
| YAML-driver precedent | ESPHome · Telegraf-modbus | study |
| Waveform storage | HDF5 | reuse |
| Fast-plane DSP/stream | GNU Radio · pyqtgraph | reuse/mine |

**Build ourselves (the differentiators):** the source→hub ingest contract +
orchestrator-as-supervisor (curated vs custom); the two-plane / Record /
folder-as-record semantics with pre-roll; the declarative driver schema; the Qt
app that's bench ⊕ agent ⊕ viewer; the ELN behaviours (auto-provenance,
annotations→notes, nudging); the Python SDK + scaffolded notebooks.

**Closest prior art to study** (none fits the whole, but each informs us):
**Bluesky / Ophyd / Tiled** (run "document" model ≈ our Record/run — study this),
**Tango Controls + Sardana + Taurus** (device-server + Qt control GUIs),
**ThingsBoard** (ingest + catalog + dashboards + multi-tenant RBAC).

---

## 17. Cross-cutting requirements

- **Cross-platform:** Windows + Linux, one implementation (no OS-specific
  supervision). macOS best-effort.
- **Packaging:** per the existing pattern — PyInstaller one-file exes built on a
  Windows CI runner for the Qt app; agents likewise. The local hub is embedded.
- **Versioning:** the ingest contract and the `FORMAT.md`/project layout are
  versioned; old folders remain readable.
- **Observability of ferroDAC itself:** structured logs + health from every
  driver process and the orchestrator.
