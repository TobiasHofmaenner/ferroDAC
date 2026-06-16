# ferroDAC server (hub)

The networked **hub**: the rendezvous between acquiring **agents** (today: the Qt
app publishing its local devices) and **viewers** (other clients that see those
devices *as if local*). Local-first — it runs in Docker on your bench now, and
the same images deploy to a k8s cluster later. See `docs/DESIGN.md` §5.3, §12,
§12.1 for the architecture.

## Status — Milestone 1 (in progress)

**Goal:** a hub that accepts live data from an agent and re-offers those devices
to other clients, transparently — **no storage, no auth, no control yet.**

- [x] `data_plane.proto` — the wire contract (this is the load-bearing artifact).
- [ ] hub: in-memory catalog + ingest `Session` + `Subscribe`/`Catalog`.
- [ ] agent role in the Qt app (publish local devices over `Ingest.Session`).
- [ ] viewer role in the Qt app (remote devices resolve via the §6.1 "bind
      REMOTE" branch and render live).
- [ ] `docker-compose.yml` (just the hub for now).
- [ ] headless end-to-end integration test (agent → hub → viewer).

Scope guard: **read-only, live-only.** Remote sinks are *visible but inert* —
control transparency is a later milestone. Storage (VictoriaMetrics / MinIO /
Postgres) and the historic `query()` half of the read interface come after this.

## Layout

```
server/
  proto/ferrodac/v1/data_plane.proto   the contract (source of truth)
  proto/gen.sh                         dockerised codegen (no host toolchain)
  gen/ferrodac/v1/*_pb2*.py            generated stubs (committed)
  requirements.txt                     hub runtime deps
  hub/                                 the hub app (next step)
```

## The contract (`data_plane.proto`)

Two services, one per role:

- **`Ingest.Session`** (agent → hub) — a single **bidirectional** stream the
  agent **dials out** to open (egress-only; the lab takes no inbound). Up:
  `Hello` → `DeviceDescriptor` announces → `ReadingBatch`es → `Retire`/
  `Heartbeat`. Down: `Welcome`/`Ack`, and a **reserved** `Command` (control is
  later — unused in M1, but the channel exists so it never needs the hub to dial
  back into the lab).
- **`Viewer`** (client → hub) — `GetCatalog` + `WatchCatalog` (remote devices
  appear with their **original `(device_uuid, source_id)` keys**, so a layout or
  route built against them works unchanged) and `Subscribe` (the **live** half of
  the unified read interface; the historic `query()` half lands with storage).

Invariants baked into the contract:

- **Full resolution on the wire** — nothing is decimated before sending; the
  store always holds raw. Decimation is read-time, viewer-sized.
- **Compositional identity** `(device_uuid, source_id)` — the UUID is portable.
- **Auth is a reserved seam** — a `token` rides the handshake/requests, accepted
  unconditionally for now; enforcement is a later flip to a metadata interceptor.
- **Versioned** — `contract_version` is negotiated in the handshake.

## Regenerating the stubs

```sh
server/proto/gen.sh        # runs protoc in a container; writes server/gen/
```

The stubs are committed so neither the hub nor the (pip-locked) Qt host needs a
protoc toolchain to import the contract.
