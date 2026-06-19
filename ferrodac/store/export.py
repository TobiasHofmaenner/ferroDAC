"""Read-time CSV export of a time window (DESIGN §7.2 / §7.4).

Materializes any window ``[t0, t1]`` for a set of sources — read through the
**resolver** (RAM + local store + hub), so you can export anything you can see,
not just what's in RAM or a recording — into a self-describing, reimportable
bundle:

    <dest>/
      data.csv        scalars: ABSOLUTE time (time_iso + time_epoch_s) + one
                      column per source. Sparse by default (a cell is blank when
                      that channel wasn't sampled at that instant); forward-fill
                      is opt-in (fill=True).
      trace_<n>.csv   one per trace source (one file per config-epoch): a matrix
                      of time_epoch_s + the swept-axis columns (header = the axis).
      manifest.json   source keys / dtypes / units / files → reimportable.

Qt-free; `reader` is anything exposing ``read_raw(key,t0,t1) -> (t, v)`` and
``read_raw_trace(key,t0,t1) -> [(times, Y, x), ...]`` (the Resolver, a ZarrStore).
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone

import numpy as np

EXPORT_VERSION = 1


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(name)).strip("_") or "source"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _num(v) -> str:
    return f"{float(v):.10g}"


def _column(name: str, unit: str) -> str:
    return f"{name} [{unit}]" if unit else (name or "value")


def export_window(dest_dir: str, sources: dict, reader, t0, t1, fill: bool = False) -> dict:
    """Export ``[t0,t1]`` for `sources` ({key: {name, unit, dtype}}) via `reader`.
    Writes the bundle described in the module docstring; returns the manifest."""
    os.makedirs(dest_dir, exist_ok=True)
    t0, t1 = float(t0), float(t1)
    manifest = {
        "ferrodac_export": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "t0": t0, "t1": t1,
        "time_columns": ["time_iso", "time_epoch_s"],   # UTC ISO + epoch seconds
        "fill": "forward" if fill else "none",
        "sources": [],
    }
    scalars: list = []          # (header, t_array, v_array)
    used_files: set = set()
    for key, meta in sources.items():
        dtype = meta.get("dtype", "scalar")
        name = meta.get("name") or key.rsplit("/", 1)[-1]
        unit = meta.get("unit", "")
        if dtype == "trace":
            blocks = [b for b in reader.read_raw_trace(key, t0, t1) if len(b[0])]
            for i, (times, Y, x) in enumerate(blocks):     # one file per epoch
                stem = _safe(name) + ("" if len(blocks) == 1 else f"_{i + 1}")
                fname = _unique(f"trace_{stem}.csv", used_files)
                _write_trace(os.path.join(dest_dir, fname), times, Y, x)
                manifest["sources"].append({
                    "key": key, "name": name, "unit": unit, "dtype": "trace",
                    "file": fname, "scans": int(len(times)), "bins": int(np.asarray(x).size)})
        else:
            t, v = reader.read_raw(key, t0, t1)
            if len(t) == 0:
                continue
            header = _column(name, unit)
            scalars.append((header, np.asarray(t, dtype="f8"), np.asarray(v, dtype="f8")))
            manifest["sources"].append({
                "key": key, "name": name, "unit": unit, "dtype": dtype,
                "file": "data.csv", "column": header, "samples": int(len(t))})
    if scalars:
        _write_scalars(os.path.join(dest_dir, "data.csv"), scalars, fill)
    with open(os.path.join(dest_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def _unique(fname: str, used: set) -> str:
    base, ext = os.path.splitext(fname)
    out, n = fname, 1
    while out in used:
        n += 1
        out = f"{base}_{n}{ext}"
    used.add(out)
    return out


def _write_scalars(path: str, cols: list, fill: bool) -> None:
    """cols = [(header, t_array, v_array)] → wide CSV on the UNION of timestamps,
    absolute time. A cell is blank where that source has no sample at that instant
    (honest); forward-filled only if `fill`. Channels from one device share a
    timestamp (one engine cycle) so they line up on a row."""
    headers = [c[0] for c in cols]
    maps = [dict(zip(t.tolist(), v.tolist())) for _h, t, v in cols]
    all_ts = sorted(set().union(*(set(t.tolist()) for _h, t, _v in cols))) if cols else []
    last = [None] * len(cols)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_iso", "time_epoch_s"] + headers)
        for ts in all_ts:
            cells = []
            for i, mp in enumerate(maps):
                if ts in mp:
                    last[i] = mp[ts]
                    cells.append(_num(mp[ts]))
                elif fill and last[i] is not None:
                    cells.append(_num(last[i]))
                else:
                    cells.append("")
            w.writerow([_iso(ts), f"{ts:.6f}"] + cells)


def _write_trace(path: str, times, Y, x) -> None:
    """One scan per row: time_epoch_s + intensities; header row = the swept axis."""
    Y = np.asarray(Y)
    x = np.asarray(x)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_epoch_s"] + [f"{mz:g}" for mz in x])
        for i in range(len(times)):
            w.writerow([f"{float(times[i]):.6f}"] + [f"{v:.6E}" for v in Y[i]])
