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

**v0.1 — source-management MVP.** The first vertical slice of Phase 0: the
`Source` contract, a module-based driver registry, background discovery, and a
minimal Qt UI to add/remove sources (no plotting, no server yet). Two fake
discoverable sources prove the loop end-to-end with no hardware.

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
  core/base.py       BaseSource convenience base (status state-machine)
  core/registry.py   loads source modules, collects Source subclasses
  core/manager.py    background discovery + available/active state
  sources/fake.py    hardware-free example sources
  ui/app.py          minimal source-management UI (nested source/channel cards)
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
