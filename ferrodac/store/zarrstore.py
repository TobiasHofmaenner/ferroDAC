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

import functools
import math
import threading
from urllib.parse import quote

import numpy as np
import zarr

_F = 16              # rollup downsample factor between pyramid levels
_TOP = 512           # build levels until the top tier has <= this many buckets
# Raw array chunk (DESIGN §21 Tier-1). 16k samples ≈ 128 KB: a 2-s tail append
# rewrites ONE small chunk, not an 8 MB one (the old 1<<20 forced a growing
# read-modify-write every flush — audit mechanism #2). Chunk size is fixed at
# array creation, so this applies to new epochs; existing stores keep theirs.
_CHUNK = 1 << 14     # raw array chunk (~16k samples)

# Gap/coverage algebra + display decimation live in the shared policy modules
# (DESIGN §22 I-10) — one implementation for the store, RAM tier, and resolver.
from .decimate import downsample as _downsample
from .decimate import interleave as _interleave
from .intervals import GAP_K as _GAP_K
from .intervals import split_intervals as _split_intervals


def _locked(fn):
    """Serialize a public method behind the store's RLock (DESIGN §21.2): one
    writer (the store-writer pump) + many readers (resolver, sync, export, GUI
    shutdown) share this object, and zarr does not promise read-during-resize
    safety on one open group. RLock because publics call publics. Rule for new
    code: public methods take the lock; ``_``-helpers assume it is held."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return wrapper


class ZarrStore:
    _DEVICES = "devices"        # top-level group holding per-device provenance records

    # On-disk format version. Stamped on the store root + every source/device/epoch group
    # so a future breaking layout change has a home and a way to tell old bytes from new
    # (you cannot retrofit a version onto data already written — audit 2026-07-05). Readers
    # branch on `schema_version()`; add entries to MIGRATORS when the layout changes.
    SCHEMA_VERSION = 1
    MIGRATORS: dict = {}        # {from_version: callable(root) -> None}, run in order

    def __init__(self, root, mode: str = "a"):
        self.root = zarr.open_group(store=str(root), mode=mode)
        self._lock = threading.RLock()   # see _locked — cross-thread store access
        self._key_cache: dict = {}       # group name -> source key (immutable mapping)
        self._cov_cache: dict = {}       # uuid -> {"out": [...], "per": {ep: ivs},
        #   "stale": set(ep)} — per-EPOCH coverage memo. An append marks only ITS epoch
        #   stale (it used to nuke the whole memo, so every 2 s flush forced a full
        #   re-read of every epoch on the next play tick / prefetch pass — store-lock
        #   hammering, GUI freezes §21.2); coverage() re-splits just the stale epochs.
        self._epoch_n: dict | None = None   # {(key, epoch): n} — maintained on append;
        #   epoch_lengths() used to sweep EVERY source × epoch group ever created under
        #   the lock (thousands of metadata reads on a lived-in store), and the sync
        #   thread calls it every 5 s — the 1:1-correlated cause of the store-writer
        #   backlog. Lazily built once, then O(1).
        self._handles: dict = {}         # (gname, epoch) -> (eg, t_array, data_array)
        #   append reopened the source group, epoch group and both arrays every flush
        #   (~6 metadata reads, each a sync→async round-trip); the writer is the only
        #   mutator, so cached handles stay coherent (readers open their own).
        self._dtype_cache: dict = {}     # source key -> dtype (immutable; lock-free
        #                                  serve — see source_dtype)
        self._cidx: dict = {}            # (gname, epoch) -> [first t of each chunk] —
        #   the writer-side mirror of the epoch's "cidx" attr (see append): lets a
        #   windowed read bisect to the 1-2 chunks it needs instead of decompressing
        #   the WHOLE t array (O(epoch) per read; the play tick reads per 50 ms).
        # Stamp the version on a NEW (empty) store. An existing store with no stamp is
        # pre-versioning → reads back as 0 (legacy); we don't mislabel it as v1.
        if mode != "r" and "schema_version" not in self.root.attrs \
                and next(self.root.group_keys(), None) is None:
            self.root.attrs["schema_version"] = self.SCHEMA_VERSION

    def schema_version(self) -> int:
        """The store's on-disk format version (0 = written before versioning existed)."""
        return int(self.root.attrs.get("schema_version", 0))

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

    @_locked
    def add_source(self, uuid, name="", unit="", dtype="scalar"):
        g = self.root.require_group(self._gname(uuid))
        if "key" not in g.attrs:                     # init once, original key kept
            g.attrs["schema_version"] = self.SCHEMA_VERSION
            g.attrs["key"] = str(uuid)
            g.attrs["name"], g.attrs["unit"], g.attrs["dtype"] = name, unit, dtype
            g.attrs["epochs"] = []
            g.attrs["config"] = []
            self._dtype_cache[str(uuid)] = dtype     # immutable → lock-free serves
        return g

    def sources(self) -> list:
        """All source keys — deliberately LOCK-FREE. The Timeline's 500 ms tick
        calls this on the GUI thread, and taking the store RLock queued the GUI
        behind the writer/sync side's long holds (an O(epoch) rollup rebuild or
        a sync sweep holds it for hundreds of ms — watchdog stalls, 2026-07-09).
        Safe without the lock: group LISTING is directory metadata (none of the
        array-resize hazard the lock exists for), and the name → key attr is
        IMMUTABLE and read once per group ever (cached). A group racing its own
        creation just resolves on the next tick; a transient listing failure
        serves the last-known catalog."""
        try:
            names = list(self.root.group_keys())
        except Exception:                        # noqa: BLE001 — mid-write listing race
            return list(self._key_cache.values())
        out = []
        for n in names:
            if n == self._DEVICES:
                continue
            k = self._key_cache.get(n)
            if k is None:
                try:
                    k = self.root[n].attrs.get("key", n)
                except Exception:                # noqa: BLE001 — group mid-creation
                    continue                     # → picked up next tick
                self._key_cache[n] = k
            out.append(k)
        return out

    def source_dtype(self, uuid) -> str:
        """The stored datatype tag of a source ("scalar" | "trace" | …) — lets
        the replay path pick read_raw vs read_raw_trace. "scalar" if unknown.

        MEMOISED and served WITHOUT the store lock once known (a source's dtype is
        immutable): the display path calls this on the GUI thread per redraw, and
        taking the RLock queued the GUI behind the read pool's long epoch reads —
        watchdog: 7.7 s waiting for a one-word answer. Unknown sources are not
        cached (they may be added later)."""
        dt = self._dtype_cache.get(str(uuid))
        if dt is not None:
            return dt
        with self._lock:
            try:
                dt = self._source(uuid).attrs.get("dtype", "scalar")
            except KeyError:
                return "scalar"                      # not stored yet → don't cache
        self._dtype_cache[str(uuid)] = dt
        return dt

    @_locked
    def source_meta(self, uuid):
        """(name, unit, dtype) for a recorded source — so the dashboard can show
        historic channels as routable ports even with no live device."""
        try:
            a = self._source(uuid).attrs
            return a.get("name", ""), a.get("unit", ""), a.get("dtype", "scalar")
        except KeyError:
            return "", "", "scalar"

    # -- devices (provenance: identity + metadata change-log, folds to record-at-T)
    # A per-device record stored ALONGSIDE the data so historic measurements carry
    # full provenance (which instrument, what calibration). Keyed by the source-key
    # prefix (uuid|instance_id). `current` is the latest merged snapshot; `meta` is
    # the change-log [[t, field, value], ...] — the emit_config/config_at pattern at
    # device granularity, so the record can be folded to "as of time T".
    def _device(self, device_id):
        return self.root[self._DEVICES][self._gname(device_id)]

    @_locked
    def put_device(self, device_id, fields: dict) -> None:
        """Create/refresh a device's identity + current metadata snapshot."""
        g = self.root.require_group(self._DEVICES).require_group(self._gname(device_id))
        if "device_id" not in g.attrs:
            g.attrs["schema_version"] = self.SCHEMA_VERSION
            g.attrs["device_id"] = str(device_id)
            g.attrs["meta"] = []
        g.attrs["current"] = dict(fields or {})

    @_locked
    def emit_device_meta(self, device_id, t: float, field: str, value) -> None:
        """Append a metadata change [t, field, value] to a device's change-log."""
        g = self._device(device_id)
        ev = list(g.attrs.get("meta", []))
        ev.append([float(t), str(field), value])
        g.attrs["meta"] = ev

    @_locked
    def device_record_at(self, device_id, t: float) -> dict:
        """The device's metadata as of time t: the change-log folded (events with
        et <= t win). Falls back to the current snapshot when the log is empty or t
        precedes it. {} for an unknown device (old stores degrade gracefully)."""
        try:
            g = self._device(device_id)
        except KeyError:
            return {}
        meta = g.attrs.get("meta", [])
        rec: dict = {}
        for et, k, v in sorted(meta, key=lambda e: e[0]):   # by TIME, not insertion
            if et <= t:
                rec[k] = v                                  # greatest et ≤ t wins
        return rec or dict(g.attrs.get("current", {}))

    @_locked
    def device_meta_history(self, device_id, field: str) -> list:
        """Every change-log value for ONE field, sorted by time: [(t, value), ...].
        The timeline the σ reconstruction segments a window on (a field's model epochs).
        Empty for an unknown device / field."""
        try:
            g = self._device(device_id)
        except KeyError:
            return []
        out = [(float(et), v) for et, k, v in g.attrs.get("meta", []) if k == field]
        out.sort(key=lambda e: e[0])
        return out

    @_locked
    def device_records(self) -> list:
        """Current record of every known device — for /dev historic enumeration."""
        if self._DEVICES not in self.root:
            return []
        dg = self.root[self._DEVICES]
        return [dict(dg[n].attrs.get("current", {})) for n in dg.group_keys()]

    @_locked
    def device_ids(self) -> list:
        if self._DEVICES not in self.root:
            return []
        dg = self.root[self._DEVICES]
        return [dg[n].attrs.get("device_id", n) for n in dg.group_keys()]

    # -- store-and-forward sync (epoch-incremental copy, DESIGN §12.1) --------
    @_locked
    def epoch_lengths(self) -> dict:
        """{(source_key, epoch): n} — per-epoch sample counts. The hub reports
        these as the sync truth; the agent uploads any epoch tail the hub lacks.

        Served from an in-memory map maintained by append/append_trace. The old
        implementation swept every source × epoch group ever created (one zarr
        metadata read each) under the store lock — on a lived-in store that is
        thousands of reads, and the sync thread calls this every 5 s: the writer
        pump queued behind it, which was the metronomic store-writer backlog.
        The sweep now runs ONCE per process (first call), then it's a dict copy."""
        if self._epoch_n is None:
            out = {}
            for n in self.root.group_keys():
                if n == self._DEVICES:                   # not a source group
                    continue
                g = self.root[n]
                key = g.attrs.get("key", n)
                for ep in g.attrs.get("epochs", []):
                    out[(key, ep)] = int(g[ep].attrs.get("n", 0))
            self._epoch_n = out
        return dict(self._epoch_n)

    def _note_epoch_n(self, uuid, epoch: str, n: int) -> None:
        """Keep the epoch_lengths map current on write (call with the lock held)."""
        if self._epoch_n is not None:
            self._epoch_n[(str(uuid), epoch)] = int(n)

    @_locked
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

    @_locked
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
    @_locked
    def emit_config(self, uuid, t: float, key: str, value) -> None:
        g = self._source(uuid)
        ev = list(g.attrs.get("config", []))
        ev.append([float(t), str(key), value])
        g.attrs["config"] = ev

    @_locked
    def config_at(self, uuid, t: float) -> dict:
        state: dict = {}
        for et, k, v in self._source(uuid).attrs.get("config", []):
            if et <= t:
                state[k] = v
        return state

    @_locked
    def config_events(self, uuid, t0=None, t1=None) -> list:
        return [(et, k, v) for et, k, v in self._source(uuid).attrs.get("config", [])
                if (t0 is None or et >= t0) and (t1 is None or et <= t1)]

    # -- write samples (chunk-wise append into the current/declared epoch) ---
    def _mark_epoch_stale(self, uuid, epoch: str) -> None:
        """An epoch grew/changed → invalidate ONLY its slice of the coverage memo
        (call with the lock held). Everything else in the memo stays warm."""
        c = self._cov_cache.get(uuid)
        if c is not None:
            c["stale"].add(epoch)

    @_locked
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
        self._mark_epoch_stale(uuid, key)            # coverage: this epoch only
        hk = (self._gname(uuid), key)
        h = self._handles.get(hk)
        if h is None:
            eg = g.require_group(key)
            ta = eg["t"] if "t" in eg else eg.create_array(
                "t", shape=(0,), chunks=(_CHUNK,), dtype="f8")
            va = eg["v"] if "v" in eg else eg.create_array(
                "v", shape=(0,), chunks=(_CHUNK,), dtype="f8")
        else:
            eg, ta, va = h
        n0 = ta.shape[0]
        ta.resize((n0 + len(t),)); ta[n0:] = t
        va.resize((n0 + len(v),)); va[n0:] = v
        n = int(n0 + len(t))
        # ONE metadata write for all epoch attrs (each attrs assignment rewrites the
        # whole epoch zarr.json — three separate rewrites per flush measured; t0 is
        # stamped from the new data so chunk 0 is never re-read, mech #2).
        upd = {"t1": float(t[-1]), "n": n, "dirty": True}
        if n0 == 0:
            upd["schema_version"] = self.SCHEMA_VERSION
            upd["t0"] = float(t[0])
        cidx = self._chunk_index(hk, eg)
        grew = False
        for b in range(-(-n0 // _CHUNK) * _CHUNK, n, _CHUNK):
            cidx.append(float(t[b - n0]))            # first timestamp of a new chunk
            grew = True
        if grew:
            upd["cidx"] = list(cidx)                 # rides the SAME single doc write
        eg = eg.update_attributes(upd)               # returns the refreshed handle
        self._handles[hk] = (eg, ta, va)
        self._note_epoch_n(uuid, key, n)

    def _chunk_index(self, hk, eg) -> list:
        """The writer-side per-chunk first-timestamp index for an epoch (loaded from
        the 'cidx' attr once, then kept in memory; call with the lock held)."""
        cidx = self._cidx.get(hk)
        if cidx is None:
            cidx = self._cidx[hk] = list(eg.attrs.get("cidx", []))
        return cidx

    def finalize_rollups(self, uuid, epoch: str = None) -> None:
        """(Re)build the min/max pyramid for an epoch (call on flush/close).

        An O(epoch) rebuild from raw, run on the rollup worker (never the GUI, never
        the store-writer pump). The store lock is held only around the SNAPSHOT and
        the WRITE-BACK — the downsample itself runs unlocked, so the writer pump and
        readers no longer queue for the whole rebuild (the old whole-method lock held
        for hundreds of ms→seconds as the session epoch grew was a main source of
        both the store-writer backlog and multi-second GUI stalls). Data appended
        between snapshot and write-back simply leaves the epoch dirty (honest flag),
        exactly as an append right after a finalize would."""
        with self._lock:
            g = self._source(uuid)
            gap_k = float(g.attrs.get("gap_k", _GAP_K))
            keys = [epoch] if epoch else list(g.attrs.get("epochs", []))
        for key in keys:
            with self._lock:                     # snapshot this epoch's raw
                eg = g[key]
                if eg.attrs.get("modality") == "trace" or "v" not in eg:
                    continue                     # trace epoch (stores 'y', not 'v') → scalar
                #                                  rollups/gap-split don't apply; never KeyError
                t = np.asarray(eg["t"][:]); v = np.asarray(eg["v"][:])
            if len(t) == 0:
                continue
            lvl, ct, cmn, cmx = 0, t, v.copy(), v.copy()
            tiers = []                           # computed UNLOCKED — the O(epoch) part
            while len(cmn) > _TOP:
                lvl += 1
                ct, cmn, cmx = _downsample(ct, cmn, cmx, _F)
                tiers += [(f"r{lvl}_t", ct), (f"r{lvl}_min", cmn), (f"r{lvl}_max", cmx)]
            # contiguous-recording intervals, computed here (already an O(epoch) pass)
            # and cached on the epoch so coverage() stays a cheap attr read
            ivs = [[s, e] for s, e in _split_intervals(t, gap_k)]
            with self._lock:                     # write-back
                for name, arr in tiers:
                    self._put(eg, name, arr)
                # Attribute writes MUST go through the writer's cached handle when one
                # exists: update_attributes merges into the handle's IN-MEMORY attrs
                # and rewrites the whole doc, so writing through a stale handle
                # silently drops keys another handle wrote (appends during the
                # unlocked compute updated t1/n via the cached one).
                hk = (self._gname(uuid), key)
                h = self._handles.get(hk)
                eg_w = h[0] if h is not None else g[key]     # fresh open = current attrs
                n_now = int(h[1].shape[0] if h is not None else eg_w["t"].shape[0])
                eg_w = eg_w.update_attributes({
                    "levels": lvl,
                    "intervals": ivs,
                    "rolled_n": int(len(t)),     # pyramid watermark: raw samples rolled —
                    #                              _query_epoch tops up the dirty tail past
                    #                              it from raw (coverage() is dirty-aware;
                    #                              query() must be too)
                    "dirty": n_now != len(t),    # appends during the unlocked compute
                })                               # keep the epoch honestly dirty
                if h is not None:
                    self._handles[hk] = (eg_w, h[1], h[2])
                self._mark_epoch_stale(uuid, key)    # intervals/dirty changed

    def _put(self, g, name, arr):
        if name in g:
            g[name].resize(arr.shape); g[name][:] = arr
        else:
            # Fixed chunk size — NOT len(arr): chunk size is frozen at creation, so
            # sizing it to the first write froze early-session pyramids into hundreds
            # of tiny chunks that every later rollup rewrote one file at a time
            # (measured 2.5× the whole-rollup cost on a poisoned epoch). A small
            # array in a 16k chunk is just one partial chunk file.
            g.create_array(name, shape=arr.shape, chunks=(_CHUNK,), dtype=arr.dtype)
            g[name][:] = arr

    # -- read (the resolver tier protocol) -----------------------------------
    @_locked
    def has(self, uuid) -> bool:
        """Does this store hold any data for the source? Cheap presence test (epoch attrs
        only, no sample read) for Resolver.knows — a stored-vs-derived filter."""
        try:
            g = self._source(uuid)
        except KeyError:
            return False
        return any(g[k].attrs.get("n", 0) for k in g.attrs.get("epochs", []))

    @_locked
    def coverage(self, uuid) -> list:
        """Contiguous-recording intervals per source (the tier protocol). One epoch
        can yield SEVERAL intervals when a device went offline mid-session without
        rolling an epoch (the split is cached in ``intervals`` at finalize). A legacy
        epoch (finalized before gap-splitting existed) is migrated once, on demand.

        MEMOISED per source AND per epoch: an append marks only its own epoch stale,
        so a refresh re-splits just the live epoch instead of re-reading every epoch
        of the source (the old whole-source invalidation forced exactly that on every
        2 s flush; this is called per play tick on the GUI thread and repeatedly by
        the prefetch worker — recomputing it every time hammered the store lock +
        zarr sync and froze play, §21.2)."""
        c = self._cov_cache.get(uuid)
        if c is not None and not c["stale"]:
            return c["out"]
        try:
            g = self._source(uuid)
        except KeyError:                             # source not in this store yet
            return []
        gap_k = float(g.attrs.get("gap_k", _GAP_K))
        per = c["per"] if c is not None else {}
        stale = c["stale"] if c is not None else None
        out: list = []
        for key in g.attrs.get("epochs", []):
            ivs = per.get(key)
            if ivs is None or (stale is not None and key in stale):
                ivs = self._epoch_intervals(g, key, gap_k)
                per[key] = ivs
            out.extend(ivs)
        self._cov_cache[uuid] = {"out": out, "per": per, "stale": set()}
        return out

    def _epoch_intervals(self, g, key, gap_k) -> list:
        """One epoch's contiguous-recording intervals (lock held via coverage())."""
        eg = g[key]
        a = eg.attrs
        if not a.get("n", 0):
            return []
        # `intervals` is cached at finalize_rollups, but further append()s (dirty=True)
        # since the last rollup are NOT in it — trusting a stale `intervals` would under-
        # report the epoch and make resolver.read_raw DROP those on-disk samples from
        # exports / the processor re-stream. So gate on `dirty` FIRST and re-split the live
        # tail from its raw timestamps (bounded by the ~50k rollup interval). Splitting even
        # while dirty (not just returning t0..t1) matters because a farther tier's union
        # (_merge) would otherwise swallow a real gap in the recording tail — the live view
        # would ramp across it until the next rollup (C2).
        if a.get("dirty", True):
            ivs = _split_intervals(np.asarray(eg["t"][:]), gap_k)
        else:
            ivs = a.get("intervals")
            if ivs is None:                          # legacy finalized → migrate once
                ivs = _split_intervals(np.asarray(eg["t"][:]), gap_k)
                try:
                    eg.attrs["intervals"] = [[s, e] for s, e in ivs]
                except Exception:                    # read-only store → recompute next time
                    pass
        return [(float(s), float(e)) for s, e in ivs]

    @_locked
    def read_raw(self, uuid, t0, t1, local_only: bool = False):
        """FULL-RESOLUTION raw samples in [t0,t1] across epochs — **no rollup,
        no downsampling** (the analysis path: downsampling would low-pass-filter
        the physics). Returns (t, v) in time order. The window bounds memory.
        `local_only` is accepted for a uniform store interface (a raw store has no
        remote tier — it is always local); it is ignored here."""
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
            # bisect the per-chunk time index to the 1-2 chunks the window touches —
            # reading the WHOLE t array was O(epoch) per read, and the play tick
            # reads a small window every 50 ms. Legacy epochs (no cidx) full-read.
            lo, hi = self._window_slice(a, _CHUNK, t0, t1)
            t = np.asarray(eg["t"][lo:hi])
            i0 = lo + int(np.searchsorted(t, t0, side="left"))
            i1 = lo + int(np.searchsorted(t, t1, side="right"))
            if i1 > i0:
                ts.append(t[i0 - lo:i1 - lo])
                vs.append(np.asarray(eg["v"][i0:i1]))
        if not ts:
            return np.array([]), np.array([])
        t = np.concatenate(ts)
        v = np.concatenate(vs)
        if len(ts) > 1:                              # epochs are ordered, but be safe
            order = np.argsort(t, kind="stable")
            t, v = t[order], v[order]
        return t, v

    @staticmethod
    def _window_slice(a, chunk, t0, t1):
        """[lo, hi) sample-index bounds of the chunks whose time range can overlap
        [t0, t1], from the epoch's per-chunk first-timestamp index ('cidx' attr).
        Falls back to the full array for legacy epochs written before the index."""
        n = int(a.get("n", 0))
        cidx = a.get("cidx")
        if not cidx:
            return 0, n
        import bisect
        k0 = max(0, bisect.bisect_right(cidx, t0) - 1)   # last chunk starting ≤ t0
        k1 = bisect.bisect_right(cidx, t1)               # chunks starting ≤ t1
        return k0 * chunk, min(n, k1 * chunk)

    # -- traces (2-D: a spectrum/scan per timestamp) -------------------------
    @_locked
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
        self._mark_epoch_stale(uuid, epoch)          # coverage: this epoch only
        hk = (self._gname(uuid), epoch)
        h = self._handles.get(hk)
        first = False
        if h is None:
            eg = g.require_group(epoch)
            if "y" not in eg:                        # first scan: arrays + axis
                first = True
                eg.create_array("t", shape=(0,), chunks=(4096,), dtype="f8")
                eg.create_array("y", shape=(0, m), chunks=(256, m), dtype="f8")
                self._put(eg, "x", x)
            ta, ya = eg["t"], eg["y"]
        else:
            eg, ta, ya = h
        if ya.shape[1] != m:                         # shape mismatch — should not happen
            return
        n0 = ta.shape[0]
        ta.resize((n0 + 1,)); ta[n0] = float(t)
        ya.resize((n0 + 1, m)); ya[n0] = y
        # ONE metadata write per scan (was three, plus a chunk-0 decompress every
        # scan just to re-read ta[0] for t0 — the scalar path's mech-#2 fix, now
        # applied here too: t0 is stamped once, at the first scan).
        upd = {"t1": float(t), "n": n0 + 1}
        if first:
            upd.update({"schema_version": self.SCHEMA_VERSION, "modality": "trace",
                        "m": int(m), "t0": float(t)})
        elif n0 == 0:                                # arrays existed but were empty
            upd["t0"] = float(t)
        if n0 % 4096 == 0:                           # first scan of a new t-chunk →
            cidx = self._chunk_index(hk, eg)         # extend the per-chunk time index
            cidx.append(float(t))
            upd["cidx"] = list(cidx)
        eg = eg.update_attributes(upd)
        self._handles[hk] = (eg, ta, ya)
        self._note_epoch_n(uuid, epoch, n0 + 1)

    @_locked
    def read_raw_trace(self, uuid, t0, t1, local_only: bool = False) -> list:
        """FULL-RES trace scans in [t0,t1] as per-epoch blocks (the axis differs
        per epoch): list of (times[k], Y[k, m], x[m]). For analysis/replay.
        `local_only` is accepted for a uniform store interface (ignored — a raw
        store is always local)."""
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
            lo, hi = self._window_slice(a, 4096, t0, t1)   # see read_raw: bisect the
            t = np.asarray(eg["t"][lo:hi])                 # per-chunk time index
            i0 = lo + int(np.searchsorted(t, t0, side="left"))
            i1 = lo + int(np.searchsorted(t, t1, side="right"))
            if i1 > i0:
                out.append((t[i0 - lo:i1 - lo], np.asarray(eg["y"][i0:i1]),
                            np.asarray(eg["x"][:])))
        return out

    @_locked
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

    @_locked
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
        # budget ∝ each epoch's overlap with the window (same rule as the resolver's
        # tier split) — an even per-epoch split let a handful of restart-stub epochs
        # steal a long epoch's resolution the moment they entered the window
        overlap = {k: max(0.0, min(t1, g[k].attrs["t1"]) - max(t0, g[k].attrs["t0"]))
                   for k in epochs}
        total = sum(overlap.values()) or 1.0
        xs, ys = [], []
        for key in epochs:
            budget = max(50, int(max_points * overlap[key] / total))
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
        dirty = bool(eg.attrs.get("dirty", False))
        rolled = eg.attrs.get("rolled_n") if dirty else n
        factor = wc / budget
        # finest level that still fits the budget: buckets = wc / F^L <= budget
        # ⟺ L >= log_F(factor) ⟹ ceil; clamp to what the pyramid actually has.
        lvl = 0 if factor <= 1 else min(levels, math.ceil(math.log(factor) / math.log(_F)))

        def _slice(L):
            """The level-L min/max lanes over [a,b]. Raw (L=0) slices exactly; a
            pyramid level pads ONE bucket each side then clips its times into the
            window — bucket times are member MEANS, so a plain searchsorted clip
            shaved up to half a coarse bucket off each edge (the visible span
            shifted with every level switch)."""
            if L <= 0:
                lo, hi = self._window_slice(eg.attrs, _CHUNK, a, b)   # cidx bisect —
                t = np.asarray(eg["t"][lo:hi])       # never the whole O(epoch) array
                j0 = lo + int(np.searchsorted(t, a, side="left"))
                j1 = lo + int(np.searchsorted(t, b, side="right"))
                v = np.asarray(eg["v"][j0:j1])
                return t[j0 - lo:j1 - lo], v, v
            rt = np.asarray(eg[f"r{L}_t"][:])
            j0, j1 = np.searchsorted(rt, [a, b])
            j0 = max(0, int(j0) - 1)
            j1 = min(len(rt), int(j1) + 1)
            return (np.clip(rt[j0:j1], a, b),
                    np.asarray(eg[f"r{L}_min"][j0:j1]),
                    np.asarray(eg[f"r{L}_max"][j0:j1]))

        if lvl <= 0 or (dirty and rolled is None):
            # raw: window small enough — or a legacy dirty epoch with no pyramid
            # watermark, where raw is the only correct answer (heals at next finalize)
            tx, mn, mx = _slice(0)
            if len(tx) > budget * 2:                  # raw denser than asked → bucket
                txd, mn, mx = _downsample(tx, mn, mx, max(2, len(tx) // budget))
                return _interleave(txd, mn, mx)
            return tx, mn
        ct, cmn, cmx = _slice(lvl)
        # Smooth the F=16 level ladder: the "finest level that fits" can land as low
        # as budget/16 output buckets — a visible texture pop whenever a zoom crosses
        # a level threshold. If the chosen level is much coarser than the budget,
        # read one level finer (bounded: ≤ ~16×budget buckets) and fold it down to
        # ~budget (min/max of min/max composes) so density tracks budget smoothly.
        if len(ct) * 2 < budget and lvl >= 1:
            ct, cmn, cmx = _slice(lvl - 1)
        # whether the slice reached the window edges (the clip anchored it there) —
        # folding averages bucket times and would pull the endpoints back inside
        touch_a = len(ct) > 0 and ct[0] <= a + 1e-12
        touch_b = len(ct) > 0 and ct[-1] >= b - 1e-12
        if len(ct) > budget:
            ct, cmn, cmx = _downsample(ct, cmn, cmx, max(2, -(-len(ct) // budget)))
            if touch_a:
                ct[0] = a                            # re-anchor the window edges
            if touch_b:
                ct[-1] = b
        px, py = _interleave(ct, cmn, cmx)
        rolled = int(rolled)
        if dirty and rolled < n:
            # the pyramid ends at the last finalize; top up the appended-since tail
            # from raw (a chunk-bounded partial read) — otherwise a parked window
            # ending "now" shows a right-edge hole that vanishes on zoom-in, while
            # coverage() (dirty-aware) still reports the span as present
            tt = np.asarray(eg["t"][rolled:])
            j0 = int(np.searchsorted(tt, a, side="left"))
            j1 = int(np.searchsorted(tt, b, side="right"))
            tx, vy = tt[j0:j1], np.asarray(eg["v"][rolled + j0:rolled + j1])
            if len(tx):
                if len(tx) > budget * 2:
                    txd, mn, mx = _downsample(tx, vy, vy, max(2, len(tx) // budget))
                    tx, vy = _interleave(txd, mn, mx)
                px = np.concatenate([px, tx]) if len(px) else tx
                py = np.concatenate([py, vy]) if len(py) else vy
        return px, py


