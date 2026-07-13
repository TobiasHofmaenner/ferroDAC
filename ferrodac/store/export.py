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

from ..core.sourceid import uncertainty_at
from .uncertainty import reconstruct

EXPORT_VERSION = 2   # v2 (2026-07-09): time_iso is LOCAL time with UTC offset (matches
#                      the chart's clock axis — a UTC column read against a local-time
#                      graph looked like the data "didn't match"); duplicate source
#                      names are disambiguated; duplicate timestamps are never collapsed


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", str(name)).strip("_") or "source"


def _iso(ts: float) -> str:
    """ISO 8601 in LOCAL time with an explicit UTC offset — the same wall clock the
    chart's time axis shows, so a human can line a CSV row up with the graph. The
    time_epoch_s column stays the timezone-free machine truth."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat()


def _num(v) -> str:
    return f"{float(v):.10g}"


def _column(name: str, unit: str) -> str:
    return f"{name} [{unit}]" if unit else (name or "value")


def _u_column(name: str, unit: str) -> str:
    """GUM standard-uncertainty companion column: ``u(pressure) [mbar]``."""
    base = f"u({name})" if name else "u"
    return f"{base} [{unit}]" if unit else base


def export_window(dest_dir: str, sources: dict, reader, t0, t1, fill: bool = False,
                  tags: list = None, store=None, media_root: str = None) -> dict:
    """Export ``[t0,t1]`` for `sources` ({key: {name, unit, dtype}}) via `reader`.
    Writes the bundle described in the module docstring; returns the manifest.
    `tags` (marker dicts) overlapping the window are written to `tags.csv`.

    When `store` is given, each scalar source that has a declared σ model gets a GUM
    companion column ``u(name) [unit]`` right after its value column (DESIGN §19.0) —
    the standard uncertainty reconstructed over the window from the change-log; the
    manifest records the column, k=1, and the model for reproducibility.

    When `media_root` (the project root) is given, the span's PHOTOS and video
    CLIPS (kind="media" tags whose files live under media_root) are copied into
    ``<dest>/media/`` and recorded in ``manifest["media"]`` — so an exported run
    is self-contained: data + tags + video (DESIGN §9.3 phase 2)."""
    os.makedirs(dest_dir, exist_ok=True)
    t0, t1 = float(t0), float(t1)
    manifest = {
        "ferrodac_export": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "t0": t0, "t1": t1,
        "time_columns": ["time_iso", "time_epoch_s"],
        "time_iso_zone": "local with UTC offset (matches the chart axis); "
                         "time_epoch_s is UTC epoch seconds",
        "values": "raw as stored, in each column's stated unit — a chart may "
                  "display the same data converted to its axis unit",
        "fill": "forward" if fill else "none",
        "sources": [],
    }
    scalars: list = []          # (header, t_array, v_array)
    used_files: set = set()
    used_names: set = set()
    for key, meta in sources.items():
        dtype = meta.get("dtype", "scalar")
        name = meta.get("name") or key.rsplit("/", 1)[-1]
        unit = meta.get("unit", "")
        # Two sources may carry the SAME display name + unit (two gauges of one
        # model). Identical CSV headers are a data-trust hazard — a reader matching
        # a column to a curve can silently pick the wrong channel — so disambiguate
        # deterministically; the manifest maps each key to its exact column.
        base_name, n = name, 1
        while (name, unit) in used_names:
            n += 1
            name = f"{base_name} ({n})"
        used_names.add((name, unit))
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
            t = np.asarray(t, dtype="f8")
            v = np.asarray(v, dtype="f8")
            header = _column(name, unit)
            scalars.append((header, t, v))
            src_entry = {
                "key": key, "name": name, "unit": unit, "dtype": dtype,
                "file": "data.csv", "column": header, "samples": int(len(t))}
            # GUM companion σ column (DESIGN §19.0), reconstructed over the window when
            # a model applies. Only finite σ is written → a blank cell where no σ.
            if store is not None:
                sig = reconstruct(store, key, t, v)
                finite = np.isfinite(sig)
                if finite.any():
                    u_header = _u_column(name, unit)
                    scalars.append((u_header, t[finite], sig[finite]))
                    unc = {"column": u_header, "k": 1,
                           "coverage": "standard uncertainty (k=1, 1σ)"}
                    model = uncertainty_at(store, key, t1)
                    if model is not None:
                        unc["model"] = model.to_dict()
                    src_entry["uncertainty"] = unc
            manifest["sources"].append(src_entry)
    if scalars:
        _write_scalars(os.path.join(dest_dir, "data.csv"), scalars, fill)
    n_tags = _write_tags(os.path.join(dest_dir, "tags.csv"), tags or [], t0, t1)
    if n_tags:
        manifest["tags_file"] = "tags.csv"
        manifest["tags"] = n_tags
    if media_root:
        media = _write_media(dest_dir, tags or [], t0, t1, media_root)
        if media:
            manifest["media_dir"] = "media"
            manifest["media"] = media
    with open(os.path.join(dest_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def _write_media(dest_dir: str, tags: list, t0: float, t1: float,
                 media_root: str, used: set = None) -> list:
    """Copy the span's photos + clips into ``<dest>/media/`` and return manifest
    entries. Enumeration (span filter, containment, multi-part) is done Qt-free by
    MediaService.media_files_in; here we only copy + name. `used` dedupes bundle
    filenames across repeated calls (append_media_to_bundle)."""
    from ..core.media import MediaService
    import shutil
    entries = MediaService.media_files_in(tags, t0, t1, media_root)
    if not entries:
        return []
    mdir = os.path.join(dest_dir, "media")
    os.makedirs(mdir, exist_ok=True)
    used = used if used is not None else set(os.listdir(mdir))
    out = []
    for e in entries:
        bundled = []
        for src in e["files"]:
            fname = _unique(os.path.basename(src), used)
            try:
                shutil.copyfile(src, os.path.join(mdir, fname))
            except OSError:
                continue                            # a vanished file ≠ fatal
            bundled.append(fname)
        if not bundled:
            continue
        out.append({"kind": e["kind"], "label": e["label"], "format": e["format"],
                    "source": e["source"], "t": e["t"], "t_end": e["t_end"],
                    "rec_mid": e["rec_mid"], "files": bundled})
    return out


def append_media_to_bundle(dest_dir: str, tags: list, t0: float, t1: float,
                           media_root: str) -> int:
    """Add media to an ALREADY-written run bundle + patch its manifest.json — for
    clips that land AFTER the recording's auto-export ran (§9.3: clips finalize
    ~seconds after Stop). Idempotent-ish: filenames are deduped against what's
    already in <dest>/media/, and manifest media entries are replaced for the
    same (source, t, files) so a re-slice refreshes rather than piles up.
    Returns the number of media items now in the bundle."""
    mpath = os.path.join(dest_dir, "manifest.json")
    try:
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:                               # noqa: BLE001 — no bundle → nothing
        return 0
    existing = manifest.get("media", [])
    # drop prior entries for the (kind, source) we're refreshing (a re-slice
    # supersedes) — key on the ENTRY kind (clip|photo, derived from format), the
    # same kind _write_media stores, not the tag's "media" kind
    def _ekind(d):
        return "clip" if (d.get("payload") or {}).get("format") == "mp4" else "photo"
    fresh = {(_ekind(d), (d.get("payload") or {}).get("source"))
             for d in tags if d.get("kind") == "media" and not d.get("deleted")}
    kept = [m for m in existing if (m.get("kind"), m.get("source")) not in fresh]
    used = set(os.listdir(os.path.join(dest_dir, "media"))) \
        if os.path.isdir(os.path.join(dest_dir, "media")) else set()
    added = _write_media(dest_dir, tags, t0, t1, media_root, used=used)
    manifest["media"] = kept + added
    if manifest["media"]:
        manifest["media_dir"] = "media"
    with open(mpath, "w") as fh:
        json.dump(manifest, fh, indent=2)
    return len(manifest["media"])


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
    timestamp (one engine cycle) so they line up on a row.

    A k-way merge over per-source cursors — NEVER a ``{t: v}`` dict, which
    silently collapsed two samples sharing a timestamp into one (sample loss in
    a data export). Each row consumes at most ONE sample per source, so a
    duplicate timestamp within a source yields two rows, both values kept."""
    headers = [c[0] for c in cols]
    ts_arrs = [np.asarray(t, dtype="f8") for _h, t, _v in cols]
    vs_arrs = [np.asarray(v, dtype="f8") for _h, _t, v in cols]
    k = len(cols)
    idx = [0] * k
    last = [None] * k
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_iso", "time_epoch_s"] + headers)
        while True:
            heads = [ts_arrs[i][idx[i]] for i in range(k) if idx[i] < len(ts_arrs[i])]
            if not heads:
                break
            ts = min(heads)
            cells = []
            for i in range(k):
                if idx[i] < len(ts_arrs[i]) and ts_arrs[i][idx[i]] == ts:
                    last[i] = vs_arrs[i][idx[i]]
                    idx[i] += 1                      # consume ONE sample per row
                    cells.append(_num(last[i]))
                elif fill and last[i] is not None:
                    cells.append(_num(last[i]))
                else:
                    cells.append("")
            w.writerow([_iso(ts), f"{ts:.6f}"] + cells)


def _write_tags(path: str, tags: list, t0: float, t1: float) -> int:
    """Tags/events overlapping [t0,t1] → tags.csv (absolute time + project ids).
    `tags` are marker dicts (marker_to_dict). Returns the count written (0 → no
    file). The projects column lets a consumer filter without losing the catalog."""
    rows = []
    for d in tags:
        t = float(d.get("t", 0.0))
        te = d.get("t_end")
        end = float(te) if te is not None else t
        if t <= t1 and end >= t0 and not d.get("deleted"):
            rows.append(d)
    if not rows:
        return 0
    rows.sort(key=lambda d: d.get("t", 0.0))
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_iso", "time_epoch_s", "t_end_epoch_s", "label", "kind",
                    "severity", "projects", "comment", "origin"])
        for d in rows:
            t = float(d.get("t", 0.0))
            te = d.get("t_end")
            w.writerow([_iso(t), f"{t:.6f}",
                        f"{float(te):.6f}" if te is not None else "",
                        d.get("label", ""), d.get("kind", ""), d.get("severity", ""),
                        ";".join(d.get("projects") or []), d.get("comment", ""),
                        d.get("origin_kind", "")])
    return len(rows)


def _write_trace(path: str, times, Y, x) -> None:
    """One scan per row: time_epoch_s + intensities; header row = the swept axis."""
    Y = np.asarray(Y)
    x = np.asarray(x)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_epoch_s"] + [f"{mz:g}" for mz in x])
        for i in range(len(times)):
            w.writerow([f"{float(times[i]):.6f}"] + [f"{v:.6E}" for v in Y[i]])
