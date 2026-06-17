"""ferroDAC local store — the durable, Qt-free data tier (DESIGN §7.4).

A Zarr-backed store: **group per source** (logical identity), **sub-array per
config-epoch** (a contiguous span of homogeneous shape), a min/max **rollup
pyramid** per epoch (so wide queries stay cheap), and a sparse **config/state
stream** per source. Exposes the resolver tier protocol — ``coverage(uuid)`` and
``query(uuid, t0, t1, max_points)`` — identical to what the live RAM ring and the
remote hub implement, so the same read code serves every tier.

Qt-free and dependency-light (zarr + numpy) so the hub can run the same engine
server-side. Scalar sources for now; trace/waveform epochs are the next slice.
"""

from .zarrstore import ZarrStore

__all__ = ["ZarrStore"]
