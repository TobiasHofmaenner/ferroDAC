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

### 4.1 Core foundation: layers, the dataflow graph & extensibility (decided 2026-06-18)

The app grew organically; the base concepts are now settled, so we decouple them
behind defined interfaces. **Survey finding:** the *vocabulary* is already Qt-free
and clean (`Reading`, `Source`, `Sink`, `Device`/`DeviceDescriptor`, `Tag`,
`Trace`, and the `Processor.process()` base in `analysis/`); what's coupled is the
*orchestration* — the `Engine` bus is a `QObject`/`QTimer`, and **the dataflow
graph + processor lifecycle live in the UI** (`ui/workspace.py`). The nodes are
clean; the graph and bus that connect them are stuck in Qt. The decoupling:

**Four layers, Qt only at the top.**

```
L1  Vocabulary (Qt-free)        Reading · Source · Sink · Device · Tag · Trace
L2  Data plane (Qt-free)        Bus · DataTier(coverage/query/read_raw) ·
                                DataflowGraph · Processor(relocatable) · TimeContext
L3  Orchestration (Qt-free)     Executor: runs the graph live|replay, local|distributed
L4  UI (Qt) = VIEWS over L2/L3  panels=sink views · patch-bay=graph editor ·
                                timeline=TimeContext control
    Net (gRPC)                  hub agent/viewer · (future) compute dispatch
```

Key interfaces:
- **`Source`** emits readings · **`Sink`** consumes · **`DataTier`** =
  `coverage`/`query`(downsampled, display) /`read_raw`(full-res, analysis) — the
  resolver/store already implement this.
- **`Bus`** — readings → subscribers, behind a **Qt-free interface**; the GUI
  supplies the event-loop-driven impl (so headless replay/compute need no Qt).
- **`DataflowGraph`** (NEW, lifted out of the UI into core) — nodes
  (devices/sources/processors/sinks) + edges (routes); queryable: `nodes()`,
  `edges()`, `inputs_of()`, `downstream_of()`. The single substrate for the
  patch-bay, **dataflow introspection** (draw the graph), replay, and distribution.
- **`Processor`** — formalised into a **relocatable compute node** (see below).
- **`TimeContext`** (NEW) — the head (following-now | parked) that drives L3.

Decoupling refactor order (each shippable; app stays runnable):
1. **Lift `DataflowGraph` into core** (Qt-free); Dashboard becomes a view/editor.
2. **Qt-free `Bus` interface** (the `QTimer` Engine becomes one impl).
3. **Processor lifecycle onto the graph** (register on the graph, not the UI) +
   the spec/parallel-semantic/placement fields (local-only for now).
4. Then build replay (`TimeContext` + `Executor` + `PlaybackSource`) on this base.

**Distributed compute (reserve the seam; build later).** Turn hub-connected nodes
into a compute cluster: a heavy, parallelisable analysis is split into slices and
run on **other clients** and/or **autoscaled hub pods (HPA on k8s)**. The
architecture fits because gRPC is already the data-plane transport *and* the
**reserved bidi-`Session` down-channel** dispatches work to egress-only lab boxes
with no inbound exposure. The requirement it imposes on L2: a **`Processor` is a
relocatable compute unit** —
- **serializable spec** `(type, params)` (the code lives on each node; only config
  travels);
- **clean data contract** `raw-in → derived-out` (no machine/Qt coupling);
- **declared parallelisation semantic** `map | windowed(lookback) | reduce` (so the
  scheduler partitions *correctly* — wrong partitioning corrupts physics, same
  class of error as downsampling analysis input);
- **placement** `local | peer | hub` on the graph node (everything `local` today).

Composes with replay (stream a raw slice to wherever the processor lives). Clients
**advertise capabilities** (RAM, CPU, GPU, OpenCL/CUDA) on the agent `Hello`, so the
scheduler can place by capability. Auth uses the reserved token seam.

**Extensibility — two tiers, core stays Qt-free.**
1. **Core capability (required, Qt-free):** a **driver** (`Device` subclass) or a
   **processor** (`Processor` subclass) self-registers on import. Discovery extends
   from builtin-only to **entry-point discovery** (`ferrodac.drivers` /
   `ferrodac.processors`), so a pip-installed third-party plugin is found
   automatically. Descriptor `Options`/`Sinks` drive an **auto-generated generic
   UI** — zero UI code needed for a working config surface.
2. **Optional UI companion (Qt):** a plugin *may also* ship a custom Qt widget
   (`make_config_widget(device)` / a custom `Panel`), discovered *alongside* the
   core capability and used if present, else the generic UI. **The core never
   imports the UI tier** — headless deployments (hub, replay, compute nodes) load
   only L1–L3. This is how a vendor ships a rich tuning/calibration panel with
   their driver without coupling core to Qt.

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

### 7.3 Tags / events — a first-class datatype (decided 2026-06-16)

Tags are **events**, a category distinct from sources. Sources (scalar/trace)
are *metrics*: continuous, have a latest value, get plotted, and are
**expendable** (the live tier drops-oldest). A tag is the opposite — discrete,
timestamped, semantic, and **reliable + editable + durable**. This metrics-vs-
events split drives the whole design.

- **Load-bearing rule: tags do NOT ride the Reading/sample stream.** A tag in the
  reading `oneof` would inherit drop-oldest expendability (you'd drop an *alarm*),
  have no edit/delete, and pollute `latest()`. Tags get their **own channel**,
  sharing the hub but with **reliable** delivery.

- **The Tag entity** (evolves `MarkerModel`, not a rewrite): `id` (UUID — so it
  merges / edits / deletes across instances by id) · `t`, `t_end?` (points *and*
  spans) · `label`, `comment` · **`origin`** `{user|device|processor|system, id}`
  (provenance — what makes "devices/processors emit tags" real; enables
  attribution, filtering, future authz) · **`scope`** `global | device:<uuid> |
  source:<key>` · **`severity`** (info/warn/error/critical — a small closed enum)
  · **`kind`** (an *open string*: tag/recording/alarm/calibration/… — new kinds
  need no contract change) · **`payload`** (an *open key→value map* — the
  machine-readable extensibility hatch) · `color` (derived from kind/severity).
  `origin + scope + open kind + open payload` is the whole "don't cut us later"
  kit.

- **Three emitters, one store, one channel.** Emitters: a **user** (＋Tag), a
  **device** (driver fires an injected `emit_tag()` on an event), a **processor**
  (threshold/alarm crossings, gas-detected). Local store: `MarkerModel` graduates
  to a `TagStore` (charts/event-log subscribe to its change signal; persisted with
  the session/run + `log.md`). Hub: a **role-independent** tag API —
  `PublishTag`/`DeleteTag` (any client → hub) + `WatchTags` (any client ← hub,
  ADDED/UPDATED/REMOVED). Role-independent because a *pure viewer* must also be
  able to create tags, so they can't ride the agent-only Session stream.

- **Behaviour vs readings (all deliberate):** reliable (never dropped) · editable/
  deletable by id, **last-write-wins by id + version** (+ tombstones; full CRDT is
  overkill — that's for the markdown editor) · **no patch-bay routing** (tags are
  global annotations with a `scope`; *filter on render*, don't wire tag→sink;
  routable tags stay a future option via `scope`) · **durable** (hub `TagStore` in
  memory now; persists with the storage milestone, so late-joining viewers get
  full history and tags survive a hub restart).

- **What it unlocks** (consume-the-tag-stream, no new plumbing): **alarms**
  (processor emits `severity=warn`), **notifications** (a sink consumes alarm tags
  → email/push), **automation** (a tag triggers an action — meets the reserved
  control plane), and an **audit log** (origin + timestamp = who did what when).

- Caveat: a tag carries the emitter's absolute epoch `t`, rendered against the
  local clock (same skew caveat as readings); in replay it travels inside the run.
  Extends §7.1 (markers as one primitive on the shared clock).

**Status (2026-06-16): cross-instance sync implemented (5/6).** Built and
headless-tested end-to-end:
1. `Marker`/`MarkerModel` evolved into the §7.3 entity + LWW/tombstone store;
   Tag entity extracted to the Qt-free `core/tag.py`.
2. Contract: role-independent `Tags` service (`PublishTag`/`DeleteTag`/
   `WatchTags`) — additive, `CONTRACT_VERSION` unchanged.
3. Hub: durable in-memory TagStore (LWW, tombstones, reliable/undropped fan-out,
   snapshot-then-stream).
4. Net: Qt-free `HubTagSync` (watch + publish, replay-on-reconnect, no echo).
5. Qt glue: `HubController` syncs the local TagStore both ways, role-independent.

Live-validated cross-machine (create / edit / delete over a real hub).

**Deferred — (6) emitter API** (intentionally held until a concrete use case
drives its shape; the user has one in mind): inject `emit_tag()` so devices/
processors raise tags themselves (alarms, gas-detected), not just the ＋Tag
button. Once it exists, alarms / notifications / automation / audit-log all fall
out as tag-stream consumers with **no new plumbing** — the channel is already
built. Seam is ready (`TagOrigin.DEVICE`/`PROCESSOR`, `origin_id`, `scope`,
`severity`, open `payload`).

---

### 7.4 Storage backend: tiering, format & the config stream (decided 2026-06-17)

The concrete realization of §7.2's "one windowed query interface." Validated by
a prototype that browsed **150 GB / 37.5 B points with a ~38 MB app footprint**
(`prototypes/timeline_spike.py`), so the architecture below is measured, not
hoped.

#### Tiered resolver (one query, nearest-wins, coverage-aware)

All reads go through `query(series, t0, t1, max_points)` + `subscribe(series)`.
Behind it a **client-side resolver** composes tiers, each implementing the same
mini-protocol — `coverage(series) → intervals` and `read(series, t0, t1,
max_points) → envelope|raw`:

| tier (near → far) | holds | when |
|---|---|---|
| **Live RAM ring** | recent full-res tail (fast/hot) | always |
| **Local store** | **ambient durable Zarr** — *everything*, written continuously (+ rollups) | always |
| **Remote hub** | the archive: everything streamed up + rollups + other boxes | if connected |

- **Nearest-wins routing:** serve each sub-range from the nearest tier that
  covers it; **stitch** a window that straddles tiers (recorded span → live
  tail). Overlap → use the nearer (fresher + cheaper). v1: if one tier fully
  covers, use it; else stitch.
- **Live = the RAM tier.** Playhead at the right edge subscribes to its appends;
  parked = query the tiers. No separate live/historic code path.
- **Local-first: the remote tier is additive.** No hub → RAM + local, done.
  Connect → the remote tier just *extends coverage backward* and to other boxes.
- **Resolution is per-tier, read-time.** Each tier downsamples its share to its
  slice of `max_points` from its **rollup pyramid** (min/max multi-resolution
  tiers — the one genuinely new build; kills the wide-zoom wall, since min/max
  must otherwise read every sample in the window). Raw is never destroyed.
- **The server is one opaque tier** behind gRPC; it may run RAM+disk internally.
  Contract gains **`GetCoverage`** so the resolver knows what the remote holds
  without fetching.

#### Write path: always-on durable ambient, record = pin + CSV (refined 2026-06-17)

The ambient tier is **durable, not RAM-only** — a strengthening of §7.2's
expendable ambient. A **`StoreWriter`** subscribes to the engine and
**continuously** flushes *all* scalar data into the local Zarr (chunk-wise),
independent of Record. This buys three things the RAM ring can't: **scroll-back
past the ring**, **survival across restart/crash**, and — the big one —
**retroactive recording** (the data you forgot to Record is already on disk; you
just mark + export it). The RAM ring stays the hot cache on top.

- **Record decouples from persistence.** Recording is no longer "start writing";
  it's **pin a span** (mark it, retention-exempt) **+ materialise CSV over the
  marked area** (the human-readable export, scoped to recorded spans only). The
  durable raw was already being written.
- **Grows indefinitely for now** (decided 2026-06-17); a **retention policy**
  (time/size rollover, pinned spans exempt) lands with the search UI.
- **Firehose exception** (§7.2 holds): always-durable is for moderate-rate
  sources; a genuine firehose (e.g. a 326 kHz digitizer) stays RAM-ambient and is
  persisted only when recorded — otherwise it'd fill disk. Per-source, by rate.
- The local store is therefore **app-wide-continuous**, not per-run bundles; a
  recorded run is a *pinned span within it* (still *exportable* as a Zarr+CSV
  bundle for sharing/sync).

#### Format: Zarr everywhere, CSV first-class

- **Zarr is the store** for everything — local *and* on S3. Its store-backend
  abstraction (`LocalStore` vs an S3 store, same array API) means the resolver's
  **local and remote tiers run identical read code**, and **sync ≈ a chunk-set
  copy** (see §12.1). Rollups = **Zarr multiscale**, identical on disk and S3.
- The **RAM ring is the live tier**; Zarr is the **durable** tier, fed
  **chunk-sized flushes** (never per-sample) — so Zarr's append ergonomics never
  bite.
- **CSV is a first-class export *and* import** (slow, fat, rarely used in daily
  work — and that's fine). Every **pristine, marker-bounded recorded run is also
  materialized to CSV** alongside its Zarr, so the experimenter's data exists in
  a form openable with nothing but a text editor in 20 years. Import = §7.2's
  lower-fidelity reimport branch. Zarr itself is open/self-describing/numpy-native
  (no vendor lock) — the timeless guarantee holds even before CSV export.
- Prior art: this is **NeXus/HDF5** territory (measured data + the instrument
  config that produced it); we do it in Zarr for the local=remote=S3 story.

#### The config/state stream — every device, not just data sources

Data is uninterpretable without the state it was produced under (an RGA channel
is noise until filament+SEM; a raw voltage means different things by
configuration). So **every device emits, alongside its data Sources, a
config/state stream**: a sparse, reliable, timestamped sequence of `(t, key,
value)` change-events (filament/SEM/scan-range *and* interpretation metadata:
a channel's quantity/unit/calibration/sensor).

- **Fold** the events to any instant T → the device's full state at T.
- **Store raw, derive meaning** (extends §7.2): data stays raw forever;
  **validity gating, unit conversion, calibration, axis** are *recomputed* from
  `raw + config-state-at-T`. Recalibrate → new event → old data re-derives under
  the old cal, new under the new. Always correct, never destructive.
- **Capture-all + gate-on-read.** Never drop the noise at capture (irreversible);
  "valid data only" (e.g. filament-on) is a **read-time mask** folded from the
  config stream.
- **Rides the §7.3 reliable-event substrate** (sparse, timestamped,
  device-emitted) but is its own stream with distinct semantics: factual (not
  user-editable) and it **folds to state**. Config values are also **plottable**
  (filament on/off, SEM HV as step-channels).
- This is the **concrete activation of the deferred §7.3 Phase-6 emitter**:
  device config changes *are* device-emitted events.

#### Config-epochs & changing shape (one identity, segmented storage)

"One track" = one **logical identity** (the source UUID; routes/layouts bind to
it), **not** one fixed-shape array. Shape/meaning changes are handled by
segmentation, the standard scientific-computing answer (and why Zarr, a
*container* of many chunk-arrays, not a flat file):

- **Zarr layout:** a **group per source** (the identity); **one sub-array per
  config-epoch** (a contiguous span of homogeneous shape) — e.g. RGA `1–50` and
  `40–200` are two sub-arrays of different shape under one group; a group index
  maps time-range → epoch. The m/z **axis is reconstructed from the config
  recipe** (first/width/ppa), not stored per scan.
- **Shape change** (trace length, waveform rate) → **new epoch sub-array**.
  **Meaning-only change** (volts→°C recal, same shape) → **same array** + a
  config-epoch marking reinterpretation. Physical segmentation only on *shape*.
- **CSV materialization** of a run spanning a shape change = **one file per
  epoch** (each with its own header), listed in the run manifest.

#### Hue UX rule — "is this one clean table or not?"

In the track viewer, an **epoch boundary that breaks export-uniformity** is shown
as a **change of the track's hue** — the physicist's at-a-glance "you can't get a
single consistent CSV across here." It fires on **shape change** (→ export
becomes multiple files) *and* **quantity/unit change** (→ a column's meaning
shifts). Plain device-config that leaves the column uniform (filament, SEM HV)
is **a marker/pin, not a hue**. (So hue ⊇ storage segmentation: hue marks the
exported *table* breaking; segmentation marks the stored *bytes* changing shape.)
Shown on the track *and* the finder coverage band; it also enables **epoch-aware
export** (select within one hue → one tidy CSV), and the export action confirms
"this selection spans N epochs → N files" before writing.

### 7.5 Headless acquisition — the data-plane North Star (decided 2026-06-18)

The single governing rule that collapses "live vs replay" and the
write/read tangle:

> **Acquisition is headless. The write path must never depend on the read path.**
> Persist **raw device data only** — nothing derived, ever.

- **Two independent paths.** *Write* (always on): `device → Engine(raw) → persist
  (RAM ring · Zarr · remote)`. It runs identically whether you're live, replaying,
  or the UI is shut — it just faithfully records every **device channel**. *Read*
  (the viewer/analyzer): `query/stream a window → routing graph + processors →
  display (+ live control)`. The read path **never writes to the store.**
- **One pipeline; live is not special.** The routing graph is fed by a single
  switchable **raw source**: live = the just-persisted stream; replay = raw read
  from the store for the window. The graph is byte-identical either way.
- **Derived is transient — never persisted.** Persisting an analyzer's output
  would make "what's recorded" depend on "what the viewer is doing" — the exact
  coupling we forbid. So processors publish derived onto the **pipeline bus, not
  the Engine**; it's recomputed every time the pipeline runs. (No persist flag,
  no checkbox — we deliberately dropped that to keep the write path pure.)
- **Control is always live; replay never gates it.** Commands hit the real device
  *now*, in any view state. There's **no attempt to distinguish closed-loop from
  manual** (we can't reliably) — the user owns that footgun. What makes this safe
  for the *record* is the readback rule below.
- **Readback rule (how control is captured):** we don't record commands — devices
  expose their control settings as **readback channels** (e.g. RGA SEM-voltage),
  and the headless writer records *those* as ordinary raw data. So "what was the
  setpoint at T" is answered by the device's own channel, and correlation is
  post-hoc over recorded raw — acquisition stays self-contained. *(Driver
  guideline: every control input should expose a corresponding readback source.)*
- **Consequence:** ordering is the store's job (tiers are time-ordered → queries
  return ordered data; the chart is a view, not an arrival-order buffer), and a
  replay re-deriving its own analysis can never re-pollute the live record.

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

### 8.1 Project store backends (decided 2026-06-16)

The project folder is reached through a **`ProjectStore`** abstraction, not the
filesystem directly, so *where the folder lives* is pluggable:

- **Local** — a folder on disk. Zero infra, **offline-capable**, single-user. The
  default; what a solo user gets with no server.
- **Server** — the project lives server-side (via the hub) and the folder syncs
  through **Nextcloud** (desktop client now / WebDAV later) for durability +
  sharing. **Required for real-time collaboration** (§10.1).

In both, the files stay the **human-readable system of record**; the backend
changes only *where* the folder lives and *who* can reach it, never the format.

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

### 10.1 Documentation editor (decided 2026-06-16)

The writeup surface (`README.md`, `notes/`, run `log.md`) is edited in a
**polished embedded *web* editor**, not a native Qt widget — polished editing,
collaboration, LaTeX and highlighting are the web ecosystem's strength, and a
`QWebEngineView` lets us build the component **once and reuse the identical one
in the future web client**.

- **Source + preview** (edit ⇄ render), so the **`.md` file stays the literal
  source of truth** — no lossy rich-model serialization. Same invariant as the
  data plane: *the human-readable file is truth; the live layer materialises to
  it.*
- **Stack — all mature, off-the-shelf (we implement none of the hard parts):**
  **CodeMirror 6** (editor + native syntax highlighting; **Shiki** if richer),
  **KaTeX** (inline LaTeX), **Yjs** (CRDT) + **Hocuspocus** (its sync server) for
  real-time multiplayer. Optional **LSP/lint** assist in code blocks — scope TBD.
- **Collaboration is a *server-backend* feature** (it needs the sync hub):
  Hocuspocus runs in the cluster, persists the live doc, and **materialises the
  `.md` into the project folder** (which Nextcloud then syncs). **Nextcloud is the
  file backend/sync, not the collaboration engine.** The local backend gives the
  full editor, **solo**.
- **Cost flagged:** **QtWebEngine** (embedded Chromium) is a heavy dependency
  (~100+ MB to the build; a separate package) plus a small JS build toolchain —
  the price of the web-embed, worth it vs a worse native editor.
- **Phasing:** (1) `ProjectStore` + local backend (§8.1); (2) embedded editor,
  **solo** (edit/render, LaTeX, highlighting), reusable in the web client;
  (3) Yjs + Hocuspocus **collaboration** as a server-backend phase-2.

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

**Decimation is a *view* concern, never an *ingest* one.** What is stored — and
what is shipped to the hub — is always **full-rate**; the min/max-envelope
reduction happens at *read* time, sized to the viewer (the local bench display
for a stream too fast to move whole, or the hub answering a remote query at
~4000 px). The raw is always the record.

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
- **Ingest** (source → hub): the gRPC contract (§5.3), **full-resolution — no
  pre-send decimation** (decimation is read-time and viewer-sized; the store
  always holds raw).
- **Subscribe/stream** (hub → clients): gRPC for native clients; **WebSocket**
  for browsers. (WebSocket's bidirectionality also carries the reserved command
  path.)
- **Catalog API**, **Workspace store**, **Command bus (reserved)**.
- **Live store:** reuse — a TSDB (TimescaleDB/VictoriaMetrics/InfluxDB) for
  telemetry; a block store (HDF5) for waveforms. **Bounded retention** — the hot
  store is for live + recent scrubbing; anything worth keeping is *recorded* to
  the folder.

### 12.1 Topology & store-and-forward (decided 2026-06-16)

The hub may run **remote** (e.g. a k8s cluster — whose infra gives easy
TLS/ingress, storage and horizontal scale) **without** putting the network in the
acquisition path, because the latency-critical work lives on a **local edge
agent** in the lab, not on the hub.

- **Edge agent = the acquiring node** (today: the Qt app on the bench machine,
  same subnet/box as the instruments). It owns **acquisition, the always-on
  buffer, local recording, and any fast/closed-loop control** — all of which stay
  off the WAN. It **dials *out*** to the hub (egress only ⇒ no inbound exposure of
  the lab network; TLS + token/mTLS at the hub edge).
- **Hub = aggregation / serving / sharing** (catalog, durable store, query, remote
  viewers, cross-station). **Never in the real-time acquisition or control loop.**
- **What crosses the WAN is cheap and latency-tolerant:** full-resolution
  telemetry/traces *up* (KB/s–low-MB/s — trivial), queries and live views *down*
  (decimated at the hub to the viewer's screen), and **manual** commands (50–100 ms
  RTT is fine for a human). **Fast automated control loops never cross it.**
- **Store-and-forward is the agent's contract**, doing double duty: a
  **rate-matcher** (records full-rate locally as the durable truth, forwards to the
  hub as fast as the link allows — bursts are absorbed by the local buffer and the
  upload catches up) and an **outage buffer** (a WAN/hub outage pauses *sharing*,
  never *acquisition*; the agent keeps recording and **back-fills on reconnect**).
  The local bundle is durable until its upload completes ⇒ **zero loss**. Built
  from primitives we already have: the crash-safe append-only capture, local
  recorded bundles, and the FileDevice / historic-as-source replay (which *is* the
  back-fill).
- **The one physical wall:** only a *sustained average* rate exceeding the uplink
  (a continuous firehose) can't live-replicate to a remote hub — the buffer fills
  faster than it drains. That tier stays **local** (or gets a fatter pipe, or ships
  async/offline; the hub still eventually holds all of it). Bursty telemetry and
  finite-duration captures drain fine — not a near-term regime.

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
