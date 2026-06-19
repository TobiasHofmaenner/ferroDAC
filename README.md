# ferroDAC

*A local-first, plain-files lab data-acquisition, control & documentation platform.*

ferroDAC unifies live multi-instrument acquisition, a **universal routing
patch-bay** (any data source → any datatype-compatible display or control),
on-demand recording to portable open-format files, and lightweight
electronic-lab-notebook annotation — extensible to any instrument via a driver
library, local-first with a networked hub planned.

It grew out of two single-purpose tools — a Pfeiffer **TPG-256A** vacuum-gauge
monitor and a **Modbus RTU temperature** monitor — generalised into one platform.

## Status — v0.14 (early, but usable for real experiments)

```bash
pip install -r requirements.txt
python -m ferrodac
```

What works today:

- **Universal patch-bay.** Every data endpoint is a port. Any **Source** routes
  to any datatype-compatible **Sink** (float / bool / image / string) — including
  a device source straight into another device's control input (closed loop).
  Virtual sources (slider / button / toggle) and virtual sinks (chart / 7-seg /
  camera view) sit on the same graph as device ports.
- **Devices.** Hardware-free **simulated** gauge / thermometer / bench power
  supply; a host **camera** (Qt Multimedia, with format/resolution selection);
  and the **Pfeiffer TPG-256A** vacuum gauge controller over serial
  (auto-discovery, per-gauge pressures, gauge on/off, hardened framing).
- **Computer-vision sources.** Point a camera at a gauge or LCD, draw a box over
  the value, and OCR (Tesseract) turns it into a routable number — a *soft
  sensor* for instruments with no digital output.
- **Stable identity & resilience.** Each device gets a **UUID** (registry +
  fingerprint resolver), so layouts are portable. A device that disappears keeps
  its routes as offline placeholders and **auto-rebinds** when it returns.
- **Record & timeline.** Crash-safe **append-only** capture; each recording is a
  draggable **region** you can zoom to / export to CSV / export plots from. Event
  **tags** (timestamp + note) render **synced across every chart** on a shared
  clock. Full-session **save / restore** (portable JSON) + autosave.
- **Dockable IDE shell.** Add and tile panels; an *Edit layout* toggle locks them
  for clean interaction.

### Building a Windows executable

CI builds a one-file Windows `.exe` on every tag push (`v*`) via
[GitHub Actions](.github/workflows/build-windows.yml) and attaches it to the
GitHub Release. To build locally **on Windows** (PyInstaller can't
cross-compile):

```bat
pip install -r requirements.txt pyinstaller
pyinstaller packaging/ferrodac.spec      :: -> dist\ferroDAC.exe
```

The camera-OCR feature additionally needs [Tesseract](https://github.com/tesseract-ocr/tesseract)
installed and on `PATH`; it degrades gracefully without it.

## Concepts

- **Device / Source / Sink.** A **Device** is an instrument (a driver instance);
  it exposes **Sources** (data outputs) and **Sinks** (control inputs). Endpoints
  are addressed by `(device-uuid, id)`, so routes and saved layouts survive
  renames, re-plugs and moving between machines.
- **Two planes.** An always-on **live** plane (sources push `Reading`s into an
  `Engine` that fans them out to sinks) and an on-demand **Record** plane that
  materialises a slice to open files. Lock-in is forbidden only in the second.
- **Local-first.** Everything works at the bench with no server; the networked
  hub is additive (the data plane and identity already address devices the way
  the hub will).

## Layout

```
ferrodac/
  core/
    device.py     the Device contract (Device/Source/Sink descriptors + ABC)
    base.py       BaseDevice convenience base (status machine + poll loop)
    reading.py    Reading — the unit of the push stream
    engine.py     data-plane hub: fan-out to sinks, latest cache, drain timer
    manager.py    background discovery + available/active + UUID onboarding
    registry.py   loads device modules, collects Device subclasses
    identity.py   UUID registry + fingerprint resolution
    markers.py    shared session clock + tag / recording markers
    history.py    bounded in-memory hot buffer (live display + RAM read tier)
  store/          durable data plane (DESIGN §7.4); Qt-free:
    zarrstore.py  Zarr store — group/source, sub-array/config-epoch, rollup pyramid
    resolver.py   tiered read: RAM ring -> local store -> hub, nearest-wins + stitch
    writer.py     always-on durable writer (the crash-safe write path)
    replay.py     TimeContext + PlaybackSource + ReplayController (head-driven)
    export.py     read-time CSV bundle export of any window (via the resolver)
    sync.py       store-and-forward sync to the hub (DESIGN §12.1)
  net/            hub client, Qt-free: agent (publish) + viewer + read tier + sync
  devices/
    fake.py       hardware-free simulated instruments
    camera.py     host webcam via Qt Multimedia (image source)
    tpg256a.py    Pfeiffer TPG-256A vacuum gauge controller (serial)
  vision/
    detector.py   OCR text-detection source (ROI -> parsed value)
    ocr.py        Tesseract backend + OpenCV preprocessing
    runner.py     worker thread driving detectors off the GUI
  ui/
    app.py        the shell: Devices/Sources/Sinks/Events docks, menus, Record
    workspace.py  the dashboard router (patch-bay) + dockable panel area
    panels.py     chart / 7-seg / camera-view / slider / button / toggle
```

## Guiding principles (short version)

1. **The folder is the system of record.** Captured data lives in a portable,
   human-readable / open-format project folder (Nextcloud-friendly) still usable
   in 10 years with no tool.
2. **Two planes.** An always-on *live* plane (performant, replaceable, any tech)
   and an on-demand *Record* plane (durable, open files). Lock-in is forbidden
   only in the second.
3. **Local-first.** Everything works at the bench with no server; remote is
   additive, never required.
4. **Self-describing, extensible drivers.** Add an instrument by dropping a
   driver into the library; the UI, routing and config are generated from its
   descriptor.
5. **Meet physicists where they are.** Analysis is Python-native: the folder + a
   client SDK + a scaffolded notebook per run.
6. **Design the whole; build incrementally.** Every slot for the full vision
   (the networked hub, a historic-replay timeline, waveforms, video) is designed
   now and implemented in phases.

## Testing

`pytest` runs the whole suite (also gated in CI on every push — see
`.github/workflows/tests.yml`):

```bash
pip install pytest            # plus the runtime deps (requirements.txt)
make test                     # everything (offscreen Qt)
make test-core                # fast gate: Qt-free data plane + in-process gRPC e2e
make test-ui                  # PySide6 smoke tests only
```

- **data plane** (`tests/test_dataplane.py`) — the Qt-free store / resolver /
  replay / sync / dataflow-graph self-tests, wrapped as real pass/fail.
- **export** (`tests/test_export.py`) — the read-time CSV bundle: absolute time,
  honest sparse-vs-forward-fill, trace matrices, self-describing manifest.
- **gRPC e2e** (`tests/test_grpc_e2e.py`, marker `integration`) — real grpc.aio,
  in-process hub: store-and-forward sync + the hub-as-resolver-tier read path.
- **UI smoke** (`tests/test_ui_smoke.py`, marker `ui`) — offscreen widget build
  + the paths that have actually regressed (replay, time-axis waterfall, labels).

## Design docs

- [docs/DESIGN.md](docs/DESIGN.md) — the full architecture (the ideal we aim at).
- [docs/ROADMAP.md](docs/ROADMAP.md) — phasing, MVP scope, and open decisions.

## License

TBD.
