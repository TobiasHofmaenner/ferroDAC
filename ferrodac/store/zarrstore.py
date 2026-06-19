"""Zarr-backed local store (DESIGN §7.4). See package docstring.

On-disk layout (a Zarr group tree)::

    <root>/
      <source-uuid>/                 group = one source (logical identity)
        .attrs: name, unit, dtype, epochs:[keys...], config:[[t,key,val]...]
        <epoch-key>/                 group = one config-epoch (homogeneous shape)
          .attrs: t0, t1, n, levels, config:{...}
          t   [n]  f8                raw timestamps (epoch seconds, monotonic)
          v   [n]  f8                raw values  (scalar; trace adds a trailing dim)
          r{L}_t / r{L}_min / r{L}_max   the min/max rollup pyramid, L = 1..levels

Reads go through ``query`` (resolution-aware min/max envelope) and ``coverage``.
The config/state stream is sparse, so it lives in the source's attrs as
``[[t, key, value], ...]`` — folded to "state at T" by ``config_at``.
"""

from __future__ import annotations

import math
from urllib.parse import quote

import numpy as np
import zarr

_F = 16              # rollup downsample factor between pyramid levels
_TOP = 512           # build levels until the top tier has <= this many buckets
_CHUNK = 1 << 20     # raw array chunk (~1M samples)


def _downsample(t, mn, mx, factor):
    """One pyramid level: min/max over groups of `factor`, bucket time = mean."""
    n = len(mn)
    nb = math.ceil(n / factor)
    pad = nb * factor - n
    if pad:
        t = np.concatenate([t, np.full(pad, t[-1])])
        mn = np.concatenate([mn, np.full(pad, mn[-1])])
        mx = np.concatenate([mx, np.full(pad, mx[-1])])
    t = t.reshape(nb, factor).mean(axis=1)
    return t, mn.reshape(nb, factor).min(axis=1), mx.reshape(nb, factor).max(axis=1)


class ZarrStore:
    def __init__(self, root, mode: str = "a"):
        self.root = zarr.open_group(store=str(root), mode=mode)

    # -- sources -------------------------------------------------------------
    @staticmethod
    def _gname(key) -> str:
        # Zarr maps each group name to a directory, so the group name must be a
        # valid filename on EVERY platform. Source keys carry '/' (device/source)
        # and device ids like 'sim:gauge:A' carry ':' — and ':*?"<>|\\ are ILLEGAL
        # in Windows paths. Percent-encode every non-safe char into one flat,
        # reversible, cross-platform name (the original key is kept in attrs['key']).
        # `quote(safe="")` leaves only [A-Za-z0-9_.-~] and maps '/'→%2F, '%'→%25
        # exactly as the old scheme did, so colon-free keys keep the same group.
        return quote(str(key), safe="")

    def add_source(self, uuid, name="", unit="", dtype="scalar"):
        g = self.root.require_group(self._gname(uuid))
        if "key" not in g.attrs:                     # init once, original key kept
            g.attrs["key"] = str(uuid)
            g.attrs["name"], g.attrs["unit"], g.attrs["dtype"] = name, unit, dtype
            g.attrs["epochs"] = []
            g.attrs["config"] = []
        return g

    def sources(self) -> list:
        return [self.root[n].attrs.get("key", n) for n in self.root.group_keys()]

    def source_dtype(self, uuid) -> str:
        """The stored datatype tag of a source ("scalar" | "trace" | …) — lets
        the replay path pick read_raw vs read_raw_trace. "scalar" if unknown."""
        try:
            return self._source(uuid).attrs.get("dtype", "scalar")
        except KeyError:
            return "scalar"

    def source_meta(self, uuid):
        """(name, unit, dtype) for a recorded source — so the dashboard can show
        historic channels as routable ports even with no live device."""
        try:
            a = self._source(uuid).attrs
            return a.get("name", ""), a.get("unit", ""), a.get("dtype", "scalar")
        except KeyError:
            return "", "", "scalar"

    # -- store-and-forward sync (epoch-incremental copy, DESIGN §12.1) --------
    def epoch_lengths(self) -> dict:
        """{(source_key, epoch): n} — per-epoch sample counts. The hub reports
        these as the sync truth; the agent uploads any epoch tail the hub lacks."""
        out = {}
        for n in self.root.group_keys():
            g = self.root[n]
            key = g.attrs.get("key", n)
            for ep in g.attrs.get("epochs", []):
                out[(key, ep)] = int(g[ep].attrs.get("n", 0))
        return out

    def read_epoch(self, uuid, epoch, start, end) -> dict:
        """Raw samples [start:end] of one epoch BY INDEX — the unsynced tail to
        upload. Self-describing so the hub can apply it verbatim."""
        eg = self._source(uuid)[epoch]
        a = eg.attrs
        if a.get("modality") == "trace":
            return {"dtype": "trace", "t": np.asarray(eg["t"][start:end]),
                    "y": np.asarray(eg["y"][start:end]), "x": np.asarray(eg["x"][:])}
        return {"dtype": "scalar", "t": np.asarray(eg["t"][start:end]),
                "v": np.asarray(eg["v"][start:end])}

    def apply_chunk(self, uuid, epoch, chunk) -> int:
        """Append a synced chunk (from read_epoch) at the same source/epoch — the
        hub side of store-and-forward. Idempotent-friendly: returns the new n."""
        if chunk["dtype"] == "trace":
            self.add_source(uuid, dtype="trace")
            t, Y, x = chunk["t"], chunk["y"], chunk["x"]
            for i in range(len(t)):
                self.append_trace(uuid, float(t[i]), x, Y[i], epoch=epoch)
        else:
            self.add_source(uuid)
            self.append(uuid, chunk["t"], chunk["v"], epoch=epoch)
        try:
            return int(self._source(uuid)[epoch].attrs.get("n", 0))
        except KeyError:
            return 0

    def _source(self, uuid):
        return self.root[self._gname(uuid)]

    # -- config / state stream (sparse; folds to state-at-T) -----------------
    def emit_config(self, uuid, t: float, key: str, value) -> None:
        g = self._source(uuid)
        ev = list(g.attrs.get("config", []))
        ev.append([float(t), str(key), value])
        g.attrs["config"] = ev

    def config_at(self, uuid, t: float) -> dict:
        state: dict = {}
        for et, k, v in self._source(uuid).attrs.get("config", []):
            if et <= t:
                state[k] = v
        return state

    def config_events(self, uuid, t0=None, t1=None) -> list:
        return [(et, k, v) for et, k, v in self._source(uuid).attrs.get("config", [])
                if (t0 is None or et >= t0) and (t1 is None or et <= t1)]

    # -- write samples (chunk-wise append into the current/declared epoch) ---
    def append(self, uuid, t, v, epoch: str = None) -> None:
        g = self._source(uuid)
        t = np.asarray(t, dtype="f8").ravel()
        v = np.asarray(v, dtype="f8").ravel()
        if len(t) == 0:
            return
        epochs = list(g.attrs.get("epochs", []))
        key = epoch or (epochs[-1] if epochs else "e0")
        if key not in epochs:
            epochs.append(key)
            g.attrs["epochs"] = epochs
        eg = g.require_group(key)
        ta = eg["t"] if "t" in eg else eg.create_array(
            "t", shape=(0,), chunks=(_CHUNK,), dtype="f8")
        va = eg["v"] if "v" in eg else eg.create_array(
            "v", shape=(0,), chunks=(_CHUNK,), dtype="f8")
        n0 = ta.shape[0]
        ta.resize((n0 + len(t),)); ta[n0:] = t
        va.resize((n0 + len(v),)); va[n0:] = v
        eg.attrs["t0"] = float(ta[0])
        eg.attrs["t1"] = float(t[-1])
        eg.attrs["n"] = int(n0 + len(t))
        eg.attrs["dirty"] = True

    def finalize_rollups(self, uuid, epoch: str = None) -> None:
        """(Re)build the min/max pyramid for an epoch (call on flush/close)."""
        g = self._source(uuid)
        keys = [epoch] if epoch else list(g.attrs.get("epochs", []))
        for key in keys:
            eg = g[key]
            t = np.asarray(eg["t"][:]); v = np.asarray(eg["v"][:])
            if len(t) == 0:
                continue
            lvl, ct, cmn, cmx = 0, t, v.copy(), v.copy()
            while len(cmn) > _TOP:
                lvl += 1
                ct, cmn, cmx = _downsample(ct, cmn, cmx, _F)
                self._put(eg, f"r{lvl}_t", ct)
                self._put(eg, f"r{lvl}_min", cmn)
                self._put(eg, f"r{lvl}_max", cmx)
            eg.attrs["levels"] = lvl
            eg.attrs["dirty"] = False

    def _put(self, g, name, arr):
        if name in g:
            g[name].resize(arr.shape); g[name][:] = arr
        else:
            g.create_array(name, shape=arr.shape,
                           chunks=(max(1, len(arr)),), dtype=arr.dtype)
            g[name][:] = arr

    # -- read (the resolver tier protocol) -----------------------------------
    def coverage(self, uuid) -> list:
        try:
            g = self._source(uuid)
        except KeyError:                             # source not in this store yet
            return []
        out = []
        for key in g.attrs.get("epochs", []):
            a = g[key].attrs
            if a.get("n", 0):
                out.append((float(a["t0"]), float(a["t1"])))
        return out

    def read_raw(self, uuid, t0, t1):
        """FULL-RESOLUTION raw samples in [t0,t1] across epochs — **no rollup,
        no downsampling** (the analysis path: downsampling would low-pass-filter
        the physics). Returns (t, v) in time order. The window bounds memory."""
        try:
            g = self._source(uuid)
        except KeyError:                             # source not in this store yet
            return np.array([]), np.array([])
        ts, vs = [], []
        for key in g.attrs.get("epochs", []):
            eg = g[key]
            a = eg.attrs
            if not a.get("n", 0) or a.get("modality") == "trace":   # scalar reader
                continue
            if a["t1"] < t0 or a["t0"] > t1:
                continue
            t = np.asarray(eg["t"][:])
            i0 = int(np.searchsorted(t, t0, side="left"))
            i1 = int(np.searchsorted(t, t1, side="right"))
            if i1 > i0:
                ts.append(t[i0:i1])
                vs.append(np.asarray(eg["v"][i0:i1]))
        if not ts:
            return np.array([]), np.array([])
        t = np.concatenate(ts)
        v = np.concatenate(vs)
        if len(ts) > 1:                              # epochs are ordered, but be safe
            order = np.argsort(t, kind="stable")
            t, v = t[order], v[order]
        return t, v

    # -- traces (2-D: a spectrum/scan per timestamp) -------------------------
    def append_trace(self, uuid, t, x, y, epoch: str) -> None:
        """Append one scan (axis `x`, intensities `y`) at time `t`. The axis is
        fixed within an epoch; the writer rolls to a new epoch on an axis change
        (config-epoch, DESIGN §7.4)."""
        g = self._source(uuid)
        x = np.asarray(x, dtype="f8").ravel()
        y = np.asarray(y, dtype="f8").ravel()
        if len(y) == 0:
            return
        m = len(y)
        epochs = list(g.attrs.get("epochs", []))
        if epoch not in epochs:
            epochs.append(epoch)
            g.attrs["epochs"] = epochs
        eg = g.require_group(epoch)
        if "y" not in eg:                            # first scan: arrays + axis
            eg.create_array("t", shape=(0,), chunks=(4096,), dtype="f8")
            eg.create_array("y", shape=(0, m), chunks=(256, m), dtype="f8")
            self._put(eg, "x", x)
            eg.attrs["modality"] = "trace"
            eg.attrs["m"] = int(m)
        ta, ya = eg["t"], eg["y"]
        if ya.shape[1] != m:                         # shape mismatch — should not happen
            return
        n0 = ta.shape[0]
        ta.resize((n0 + 1,)); ta[n0] = float(t)
        ya.resize((n0 + 1, m)); ya[n0] = y
        eg.attrs["t0"] = float(ta[0]); eg.attrs["t1"] = float(t); eg.attrs["n"] = n0 + 1

    def read_raw_trace(self, uuid, t0, t1) -> list:
        """FULL-RES trace scans in [t0,t1] as per-epoch blocks (the axis differs
        per epoch): list of (times[k], Y[k, m], x[m]). For analysis/replay."""
        try:
            g = self._source(uuid)
        except KeyError:                             # source not in this store yet
            return []
        out = []
        for key in g.attrs.get("epochs", []):
            eg = g[key]; a = eg.attrs
            if not a.get("n", 0) or a.get("modality") != "trace":
                continue
            if a["t1"] < t0 or a["t0"] > t1:
                continue
            t = np.asarray(eg["t"][:])
            i0 = int(np.searchsorted(t, t0, side="left"))
            i1 = int(np.searchsorted(t, t1, side="right"))
            if i1 > i0:
                out.append((t[i0:i1], np.asarray(eg["y"][i0:i1]),
                            np.asarray(eg["x"][:])))
        return out

    def query_trace(self, uuid, t0, t1, max_scans=400) -> list:
        """For the waterfall *display*: scans in the window, time-decimated to
        ~max_scans representative spectra (display only — never for math)."""
        out = []
        for (t, Y, x) in self.read_raw_trace(uuid, t0, t1):
            if len(t) > max_scans:
                idx = np.linspace(0, len(t) - 1, max_scans).astype(int)
                t, Y = t[idx], Y[idx]
            out.append((t, Y, x))
        return out

    def query(self, uuid, t0, t1, max_points=2000):
        """Windowed, resolution-aware min/max envelope, stitched across epochs.

        Picks the coarsest pyramid level that still yields >= the requested
        points in the window, so a wide query reads a tiny tier rather than raw.
        Returns (x, y) with NaN gaps between epochs."""
        try:
            g = self._source(uuid)
        except KeyError:                             # source not in this store yet
            return np.array([]), np.array([])
        epochs = [k for k in g.attrs.get("epochs", [])
                  if g[k].attrs.get("n", 0)
                  and g[k].attrs.get("modality") != "trace"   # scalar reader only
                  and g[k].attrs["t1"] >= t0 and g[k].attrs["t0"] <= t1]
        if not epochs:
            return np.array([]), np.array([])
        budget = max(50, max_points // len(epochs))
        xs, ys = [], []
        for key in epochs:
            ex, ey = self._query_epoch(g[key], max(t0, g[key].attrs["t0"]),
                                       min(t1, g[key].attrs["t1"]), budget)
            if len(ex):
                if xs:                       # break the polyline across epochs
                    xs.append([np.nan]); ys.append([np.nan])
                xs.append(ex); ys.append(ey)
        if not xs:
            return np.array([]), np.array([])
        return np.concatenate(xs), np.concatenate(ys)

    def _query_epoch(self, eg, a, b, budget):
        n = int(eg.attrs["n"])
        span = max(1e-12, eg.attrs["t1"] - eg.attrs["t0"])
        wc = max(1.0, n * (b - a) / span)            # ~raw samples in the window
        levels = int(eg.attrs.get("levels", 0))
        factor = wc / budget
        # finest level that still fits the budget: buckets = wc / F^L <= budget
        # ⟺ L >= log_F(factor) ⟹ ceil; clamp to what the pyramid actually has.
        lvl = 0 if factor <= 1 else min(levels, math.ceil(math.log(factor) / math.log(_F)))
        if lvl <= 0:                                  # raw (window is small enough)
            t = np.asarray(eg["t"][:])
            i0 = int(np.searchsorted(t, a, side="left"))
            i1 = int(np.searchsorted(t, b, side="right"))
            tx, vy = t[i0:i1], np.asarray(eg["v"][i0:i1])
            if len(tx) > budget * 2:                  # raw denser than asked → bucket
                txd, mn, mx = _downsample(tx, vy, vy, max(2, len(tx) // budget))
                return _interleave(txd, mn, mx)
            return tx, vy
        rt = np.asarray(eg[f"r{lvl}_t"][:])
        i0, i1 = np.searchsorted(rt, [a, b])
        return _interleave(rt[i0:i1], np.asarray(eg[f"r{lvl}_min"][i0:i1]),
                           np.asarray(eg[f"r{lvl}_max"][i0:i1]))


def _interleave(t, mn, mx):
    """A min/max envelope as a single polyline: (t,min),(t,max) per bucket."""
    if len(t) == 0:
        return np.array([]), np.array([])
    x = np.repeat(t, 2)
    y = np.empty(len(t) * 2)
    y[0::2], y[1::2] = mn, mx
    return x, y
