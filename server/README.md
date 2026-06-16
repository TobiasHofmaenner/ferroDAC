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
- [x] hub: in-memory catalog + ingest `Session` + `Subscribe`/`Catalog`/`Watch`.
- [x] `docker-compose.yml` (just the hub for now) + `Dockerfile`.
- [x] headless end-to-end integration test (agent → hub → viewer) — `tests/e2e.py`.
- [x] **net layer** in the app (`ferrodac/net/`, Qt-free): `HubAgent` (publish),
      `HubViewer` (consume), `convert` (app ↔ wire) — round-trip tested with the
      real app dataclasses incl. `Trace` (`tests/net_e2e.py`).
- [ ] Qt wiring: app publishes its `DeviceManager` devices (agent) + injects
      remote devices into the Dashboard via the §6.1 "bind REMOTE" branch (viewer).

Scope guard: **read-only, live-only.** Remote sinks are *visible but inert* —
control transparency is a later milestone. Storage (VictoriaMetrics / MinIO /
Postgres) and the historic `query()` half of the read interface come after this.

## Layout

```
server/
  proto/ferrodac_contract/v1/data_plane.proto   the contract (source of truth)
  proto/gen.sh                                  dockerised codegen (no host toolchain)
  gen/ferrodac_contract/v1/*_pb2*.py            generated stubs (committed)
  hub/  core.py service.py main.py              the hub (catalog + fan-out + gRPC)
  tests/e2e.py    tests/net_e2e.py              hub e2e · app net-layer round-trip
  Dockerfile  docker-compose.yml  requirements.txt
```

The app-side clients live in the **app** package (`ferrodac/net/`), not here —
they ship with the app. `net_e2e.py` runs them against an in-process hub.

## Run it

```sh
cd server
docker compose up --build            # hub on :50051
docker compose run --rm hub python tests/e2e.py    # end-to-end test
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
