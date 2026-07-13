"""Ambient-video store-and-forward — the §9.3 phase-3 twin of store/sync.py.

The zarr SyncEngine copies the tail of each epoch the hub is missing; this does
the same for video, one SEGMENT file at a time. The model is identical (DESIGN
§12.1 applied to the media plane):

  * SYNC (agent -> hub): for every local segment the remote doesn't hold, upload
    its bytes and mark it hub-confirmed locally. The **remote's** reported set of
    held segments is the reconciliation truth, so a wiped hub re-receives
    everything and footage captured while offline backfills naturally.
  * BACKFILL (hub -> agent): on demand, when the local store has no segment
    covering a scrubbed instant, pull the covering segment from the remote and
    import it — the video twin of the read tier's history back-read.

Transport-agnostic: a `transport` provides `have()` / `push_segment()` /
`pull_segment()` (+ optional `cameras()` / `coverage()` for merging the hub's
coverage into the ribbon). `LocalVideoTransport` is the in-process one (tests /
same-box hubs); a gRPC client implements the same protocol in production.

Headless + Qt-free: capture never depends on this — the engine is a *separate*
consumer of the local store, so the network never blocks recording.
"""

from __future__ import annotations

from .videostore import seg_key


class VideoSyncEngine:
    """Reconcile a local VideoStore's segments to a remote via `transport`."""

    def __init__(self, local_store, transport):
        self.local = local_store
        self.transport = transport

    def sync_once(self) -> int:
        """One pass: upload every local segment the remote is missing, marking
        each hub-confirmed. Returns the count uploaded. Idempotent — a no-op once
        the remote has caught up (safe to call on a timer)."""
        remote_have = set(self.transport.have())         # {(cam, seg_key)}
        sent = 0
        for cam in self.local.cameras():
            for e in self.local.segments(cam):
                key = seg_key(e["t0"])
                if (cam, key) in remote_have:
                    if not e.get("synced"):              # remote has it, we forgot
                        self.local.mark_synced(cam, e["t0"])
                    continue
                data = self.local.read_segment_bytes(cam, e["t0"])
                if data is None:                         # file vanished → skip, not fatal
                    continue
                self.transport.push_segment(cam, e["t0"], e["t1"], data)
                self.local.mark_synced(cam, e["t0"])
                sent += 1
        return sent

    def backfill_at(self, cam, t) -> "dict | None":
        """On-demand pull for the scrub preview: return the local segment entry
        covering instant `t`; if there is none, fetch the covering segment from
        the remote, import it locally, and return the now-local entry. None when
        neither side has footage there. Cheap when the local store already has it
        (no network)."""
        here = self.local.segment_entry_at(cam, t)
        if here is not None:
            return here
        got = self.transport.pull_segment(cam, t)        # (t0, t1, data) | None
        if got is None:
            return None
        t0, t1, data = got
        self.local.import_segment(cam, t0, t1, data)
        return self.local.segment_entry_at(cam, t)


class LocalVideoTransport:
    """In-process transport: a second VideoStore stands in for the hub (tests /
    same-box). The gRPC client implements the same four-method protocol."""

    def __init__(self, target_store):
        self.target = target_store

    def have(self) -> set:
        return self.target.have()

    def push_segment(self, cam, t0, t1, data) -> None:
        self.target.import_segment(cam, t0, t1, data)

    def pull_segment(self, cam, t) -> "tuple | None":
        e = self.target.segment_entry_at(cam, t)
        if e is None:
            return None
        data = self.target.read_segment_bytes(cam, e["t0"])
        return None if data is None else (e["t0"], e["t1"], data)

    # -- optional: let the agent surface the hub's coverage in the ribbon --------
    def cameras(self) -> list:
        return self.target.cameras()

    def coverage(self, cam) -> list:
        return self.target.coverage(cam)
