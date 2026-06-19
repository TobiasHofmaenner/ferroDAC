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

import numpy as np

from . import GRPC_AVAILABLE

log = logging.getLogger("ferrodac.readtier")

if GRPC_AVAILABLE:
    from ferrodac_contract.v1 import data_plane_pb2 as pb
    from ferrodac_contract.v1 import data_plane_pb2_grpc as rpc

_TIMEOUT = 4.0          # seconds; a read tier must never hang the GUI thread


class HubReadTier:
    """Resolver tier backed by the hub's Store service (read side)."""

    def __init__(self, channel, token: str = "", timeout: float = _TIMEOUT):
        self.stub = rpc.StoreStub(channel)
        self.token = token
        self.timeout = timeout
        self._dtypes = None                          # cached {key: dtype} catalog

    # -- tier protocol (same shape as RamTier / ZarrStore) -------------------
    def coverage(self, series) -> list:
        try:
            resp = self.stub.GetCoverage(
                pb.CoverageRequest(source=str(series), token=self.token),
                timeout=self.timeout)
            return [(iv.t0, iv.t1) for iv in resp.intervals]
        except Exception as exc:                     # noqa: BLE001 (hub down → no cov)
            log.debug("hub coverage(%s) failed: %s", series, exc)
            return []

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
        """Full-resolution scalars over the wire (for replay/analysis, not just
        the decimated preview)."""
        try:
            resp = self.stub.ReadRaw(
                pb.RawRequest(source=str(series), t0=float(t0), t1=float(t1),
                              token=self.token),
                timeout=self.timeout)
            return np.asarray(resp.t, dtype="f8"), np.asarray(resp.v, dtype="f8")
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub read_raw(%s) failed: %s", series, exc)
            return np.array([]), np.array([])

    def read_raw_trace(self, series, t0, t1) -> list:
        """Full-resolution trace scans over the wire: list of (times[k], Y[k,m],
        x[m]) blocks (the swept axis differs per epoch)."""
        try:
            resp = self.stub.ReadRawTrace(
                pb.RawRequest(source=str(series), t0=float(t0), t1=float(t1),
                              token=self.token),
                timeout=self.timeout)
        except Exception as exc:                     # noqa: BLE001
            log.debug("hub read_raw_trace(%s) failed: %s", series, exc)
            return []
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
