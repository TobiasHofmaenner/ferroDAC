"""Hub-as-resolver-tier — the READ side of the data plane (DESIGN §12.1).

`HubReadTier` adapts the hub's `Store` service (ListSources / GetCoverage /
Query / ReadRaw) to the local resolver's tier protocol (`coverage(series)` +
`query(series, t0, t1, max_points)`), so the hub becomes the **farthest** tier:
local RAM and the local store win where they overlap; the hub fills in history
the client doesn't have locally (e.g. after the local store was wiped, or on a
viewer that never acquired). Synchronous + short-timeout + error→empty, so a
slow/absent hub degrades to "no remote coverage" instead of freezing the UI.

Qt-free; degrades to a no-op import if grpcio is missing.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from . import GRPC_AVAILABLE

log = logging.getLogger("ferrodac.readtier")

if GRPC_AVAILABLE:
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

_TIMEOUT = 4.0          # seconds; a read tier must never hang the GUI thread


class HubReadTier:
    """Resolver tier backed by the hub's Store service (read side)."""

    def __init__(self, channel, token: str = "", timeout: float = _TIMEOUT,
                 coverage_ttl: float = 3.0):
        self.stub = rpc.StoreStub(channel)
        self.token = token
        self.timeout = timeout
        self._dtypes = None                          # cached {key: dtype} catalog
        self._cov_ttl = coverage_ttl
        self._cov_cache: dict = {}                   # series -> (monotonic_ts, intervals)
        self._cov_inflight: set = set()              # series with a bg refresh running
        self._cov_lock = threading.Lock()

    # -- tier protocol (same shape as RamTier / ZarrStore) -------------------
    def coverage(self, series) -> list:
        """Remote coverage, non-blocking ON THE GUI THREAD. The resolver consults
        every tier's coverage when it partitions a window (query / read_raw /
        read_raw_trace / knows) — and that runs on the GUI thread per play tick and
        per redraw (chartfeed.reconcile, replay._render). A synchronous GetCoverage
        there (multi-second worst case over gRPC) freezes the UI (the watchdog
        stalls seen with a connected hub). So on the GUI (main) thread serve the
        cached value immediately (or [] before the first refresh) and refresh in
        the BACKGROUND. A WORKER thread — export, the async read facade, analysis —
        still BLOCKS for a fresh value: it needs accuracy for data trust and can
        afford to wait. GUI correctness holds because a local tier that also covers
        the window wins the partition regardless of the hub's stale answer (§21.2)."""
        ent = self._cov_cache.get(series)
        now = time.monotonic()
        if ent is not None and now - ent[0] < self._cov_ttl:
            return ent[1]                            # warm cache → both paths
        if threading.current_thread() is not threading.main_thread():
            return self._fetch_coverage_blocking(series, ent)   # worker → fresh, may block
        self._refresh_coverage_async(series)         # GUI thread → stale now, refresh bg
        return ent[1] if ent is not None else []

    def _fetch_coverage_blocking(self, series, ent) -> list:
        """The synchronous GetCoverage → cache update (the accurate path). Serves
        the last-known intervals if the hub is unreachable rather than raising."""
        try:
            resp = self.stub.GetCoverage(
                pb.CoverageRequest(source=str(series), token=self.token),
                timeout=self.timeout)
            cov = [(iv.t0, iv.t1) for iv in resp.intervals]
        except Exception as exc:                     # noqa: BLE001 (hub down → last known)
            log.debug("hub coverage(%s) failed: %s", series, exc)
            cov = ent[1] if ent is not None else []
        self._cov_cache[series] = (time.monotonic(), cov)
        return cov

    def _refresh_coverage_async(self, series) -> None:
        """Fetch coverage on a daemon thread + update the cache. Deduped per series
        so a burst of GUI-thread partitions triggers ONE refresh."""
        with self._cov_lock:
            if series in self._cov_inflight:
                return
            self._cov_inflight.add(series)

        def work():
            try:
                self._fetch_coverage_blocking(series, self._cov_cache.get(series))
            finally:
                with self._cov_lock:
                    self._cov_inflight.discard(series)

        threading.Thread(target=work, name="hub-cov-refresh", daemon=True).start()

    def query(self, series, t0, t1, max_points=2000):
        try:
            resp = self.stub.Query(
                pb.QueryRequest(source=str(series), t0=float(t0), t1=float(t1),
                                max_points=int(max_points), token=self.token),
                timeout=self.timeout)
            return np.asarray(resp.x, dtype="f8"), np.asarray(resp.y, dtype="f8")
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub query(%s) failed: %s", series, exc)
            return np.array([]), np.array([])

    # -- extras the resolver/replay can use ----------------------------------
    def read_raw(self, series, t0, t1):
        """Full-resolution scalars over the wire (for replay/analysis). Failure →
        empty (a read tier must never hang / raise into the resolver stitch)."""
        try:
            return self.read_raw_strict(series, t0, t1)
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub read_raw(%s) failed: %s", series, exc)
            return np.array([]), np.array([])

    def read_raw_strict(self, series, t0, t1):
        """Like read_raw but RAISES on an RPC failure instead of swallowing it to
        empty. The prefetcher needs this to tell a GENUINE no-data range (empty
        result, no error → mark it cached, play advances with a NaN break) from a
        transient FAILURE (raises → retry) — conflating them either freezes playback
        or silently skips data (§12.1)."""
        resp = self.stub.ReadRaw(
            pb.RawRequest(source=str(series), t0=float(t0), t1=float(t1),
                          token=self.token), timeout=self.timeout)
        return np.asarray(resp.t, dtype="f8"), np.asarray(resp.v, dtype="f8")

    def read_raw_trace(self, series, t0, t1) -> list:
        """Full-resolution trace scans over the wire; failure → [] (see read_raw)."""
        try:
            return self.read_raw_trace_strict(series, t0, t1)
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub read_raw_trace(%s) failed: %s", series, exc)
            return []

    def read_raw_trace_strict(self, series, t0, t1) -> list:
        """Trace scans, RAISING on failure (see read_raw_strict). list of
        (times[k], Y[k,m], x[m]) blocks (the swept axis differs per epoch)."""
        resp = self.stub.ReadRawTrace(
            pb.RawRequest(source=str(series), t0=float(t0), t1=float(t1),
                          token=self.token), timeout=self.timeout)
        out = []
        for b in resp.blocks:
            m = int(b.m)
            t = np.asarray(b.t, dtype="f8")
            y = (np.asarray(b.y, dtype="f8").reshape(len(t), m)
                 if m and len(t) else np.zeros((len(t), m)))
            out.append((t, y, np.asarray(b.x, dtype="f8")))
        return out

    def sources(self) -> list:
        """[(key, name, unit, dtype)] the hub holds — for the historic catalog."""
        try:
            resp = self.stub.ListSources(pb.SourcesRequest(token=self.token),
                                         timeout=self.timeout)
            srcs = [(s.key, s.name, s.unit, s.dtype) for s in resp.sources]
            self._dtypes = {k: dt for k, _n, _u, dt in srcs}   # refresh dtype cache
            return srcs
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub ListSources failed: %s", exc)
            return []

    def source_dtype(self, series) -> str:
        if self._dtypes is None:
            self.sources()                           # one ListSources, then cached
        return (self._dtypes or {}).get(str(series), "scalar")
