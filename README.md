# ferroDAC

*A local-first, plain-files lab data-acquisition & documentation platform.*

**(working name · design phase — no implementation yet)**

ferroDAC unifies live multi-instrument data acquisition, on-demand recording to a
portable project folder, and lightweight electronic-lab-notebook (ELN)
documentation — extensible to any instrument via a driver library, and viewable
locally or streamed to remote clients.

It generalises two existing single-purpose tools — a Pfeiffer **TPG-256A**
vacuum-gauge monitor and a **Modbus RTU temperature** monitor — into one
extensible platform.

## Status

**v0.4 — configurable dashboard.** The `Source` contract + driver registry +
discovery, a generated **configuration UI**, the **data plane** (sources push
`Reading`s → an `Engine` fans them out to sinks), and an **IDE-style dockable
shell** whose central area is a **workspace of panels** you add yourself:

- **Add** menu creates **Chart** and **7-seg display** panels; each is a dock you
  drag/resize/tile. An **Edit layout** toggle locks panels and hides their title
  bars for clean interaction (chart zoom/pan always works).
- The right **Channels** dock lists every channel; each card has a **Route ▾**
  dropdown selecting which panel(s) it feeds (a channel can feed several).
- The left **Sources** dock (device management) is hidden by default; opened via
  the toolbar / View menu.
- **Input panels** (Slider / Button / Toggle) bind to a device **control** and
  drive it via `invoke` — so the dashboard does **bidirectional** control, not
  just display.

Three fake discoverable sources prove it end-to-end with no hardware, including a
**simulated bench power supply** (set voltage / current-limit / output on-off;
live voltage / current / power over a 100 Ω load with CV→CC behaviour).

```bash
pip install -r requirements.txt
python -m ferrodac
```

Design docs:

- [docs/DESIGN.md](docs/DESIGN.md) — the full architecture (the ideal we aim at).
- [docs/ROADMAP.md](docs/ROADMAP.md) — phasing, MVP scope, and open decisions.

### Layout

```
ferrodac/
  core/source.py     the Source contract (descriptor + ABC) — the deliverable
  core/base.py       BaseSource convenience base (state machine + poll loop)
  core/reading.py    Reading — the unit of the push stream
  core/engine.py     data-plane hub: fan-out to sinks, latest cache, drain timer
  core/registry.py   loads source modules, collects Source subclasses
  core/manager.py    background discovery + available/active + streaming wiring
  sources/fake.py    hardware-free example sources (with simulated data)
  ui/app.py          source-management + config dialog + live cards + chart sink
```

## Guiding principles (short version)

1. **The folder is the system of record.** Captured data lives in a portable,
   human-readable / open-format project folder (Nextcloud-friendly) that is still
   usable in 10 years with no tool.
2. **Two planes.** An always-on *live* plane (performant, replaceable, any tech)
   and an on-demand *Record* plane (durable, open files). Lock-in is forbidden
   only in the second.
3. **Local-first.** Everything works at the bench with no server; remote is
   additive, never required.
4. **Self-describing, extensible drivers.** Add an instrument by dropping a YAML
   description (structured protocols) or a code driver (everything else) into the
   library.
5. **Meet physicists where they are.** Analysis is Python-native: the folder + a
   client SDK + a scaffolded notebook per run.
6. **Design the whole; build incrementally.** Every slot for the full vision
   (control, multi-station, waveforms, video) is designed now and implemented in
   phases.

## License

TBD.
