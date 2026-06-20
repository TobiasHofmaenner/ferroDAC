"""Store-and-forward sync — copy the local Zarr to a remote (hub) store,
epoch-incrementally (DESIGN §12.1 / §7.4).

The local store is the **ledger**: it's group-per-source, append-only sub-array-
per-epoch, so sync is simply "upload the tail of each epoch the remote doesn't
have yet." Reconciliation is against the **remote's** reported per-epoch lengths
(the remote is the truth) — so it's robust to a wiped hub and naturally backfills
history recorded while offline: a freshly-connected hub reports 0 everywhere, and
the agent uploads everything; a hub that already has the first N samples of an
epoch gets only `[N:]`.

Headless: the write path (acquisition) doesn't depend on this at all — the
SyncEngine is a *separate consumer* of the local store, so the network never
blocks acquisition.

Transport-agnostic: a `transport` provides `state()` (remote per-epoch lengths)
and `push(source, epoch, chunk)` (append a chunk). In-process for tests /
same-box hubs; a gRPC client in production. Qt-free.
"""

from __future__ import annotations

import numpy as np


class SyncEngine:
    """Reconcile a local store to a remote via `transport`, epoch tail at a time."""

    def __init__(self, local_store, transport, chunk: int = 20000,
                 max_values: int = 250_000):
        self.local = local_store
        self.transport = transport
        self.chunk = chunk                           # max ROWS per push (scalars)
        self.max_values = max_values                 # max VALUES per push (caps wide traces)

    def sync_once(self) -> int:
        """One reconciliation pass: for every local epoch, upload whatever the
        remote is missing (`[n_remote : n_local]`), in order, in chunks. Returns
        the number of samples uploaded. Safe to call repeatedly (idempotent: a
        no-op once the remote has caught up)."""
        remote = self.transport.state()              # {(source, epoch): n_remote}
        local = self.local.epoch_lengths()           # {(source, epoch): n_local}
        sent = 0
        for (source, epoch), n_local in local.items():
            n_remote = int(remote.get((source, epoch), 0))
            if n_remote >= n_local:                  # remote already has it (or ahead)
                continue
            step = self._row_step(source, epoch, n_remote, n_local)
            i = n_remote
            while i < n_local:                       # chunked, in time order
                j = min(i + step, n_local)
                chunk = self.local.read_epoch(source, epoch, i, j)
                self.transport.push(source, epoch, chunk)
                sent += j - i
                i = j
        return sent

    def _row_step(self, source, epoch, i, n_local) -> int:
        """Rows per push. A trace row carries `m` bins, so a fixed ROW count can
        blow past the gRPC message cap on wide spectra (DESIGN §12.1). Probe one row
        to learn the width and bound the step by `max_values` (≈ a couple MB)."""
        try:
            probe = self.local.read_epoch(source, epoch, i, min(i + 1, n_local))
        except Exception:                            # noqa: BLE001 — fall back to row count
            return self.chunk
        if probe.get("dtype") == "trace":
            y = np.asarray(probe["y"])
            m = int(y.shape[1]) if y.ndim == 2 else max(1, int(y.size))
            return max(1, min(self.chunk, self.max_values // max(1, m)))
        return self.chunk


class LocalTransport:
    """In-process transport: applies chunks straight into a target ZarrStore (a
    same-box / test hub). The gRPC client implements the same two-method protocol."""

    def __init__(self, target_store):
        self.target = target_store

    def state(self) -> dict:
        return self.target.epoch_lengths()

    def push(self, source, epoch, chunk) -> None:
        self.target.apply_chunk(source, epoch, chunk)
