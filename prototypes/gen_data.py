"""Generate a big on-disk synthetic dataset for the timeline spike.

Writes K scalar sources as flat **float32 memmaps on a regular time grid** — no
timestamps stored (t = t0 + i*dt), so the byte offset of any instant is pure
arithmetic and a windowed read is a single seek + slice. Chunked, so it never
builds a large array in RAM. A manifest.json is written LAST (its presence =
"dataset ready"). Usage:

    python3 prototypes/gen_data.py --gb 150 --sources 16
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time

import numpy as np

CHUNK = 16_000_000          # samples per write (~64 MB float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=150.0)
    ap.add_argument("--sources", type=int, default=16)
    ap.add_argument("--span", type=float, default=7200.0,    # 2 h, aligns with the spike
                    help="seconds of timeline the data covers")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                                                  "assets", "dataset"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    n = int(a.gb * 1e9 / 4 / a.sources)          # samples per source (float32)
    dt = a.span / n
    now = time.time()
    t0 = now - a.span
    rng = np.random.default_rng(11)
    noise = (0.03 * rng.standard_normal(CHUNK)).astype(np.float32)   # reused tile
    started = time.time()

    manifest = {"t0": t0, "now": now, "dt": dt, "n": int(n),
                "span": a.span, "gb": a.gb, "sources": []}
    print(f"generating {a.gb} GB: {a.sources} sources x {n:,} samples "
          f"(dt={dt*1e6:.2f} us ~ {1/dt/1000:.0f} kHz), {a.gb*1e9/4/a.sources/1e9:.1f} GB/src")
    for k in range(a.sources):
        path = os.path.join(a.out, f"src{k:02d}.f32")
        period = 20.0 + 40.0 * k / a.sources     # seconds
        ang = 2 * math.pi * dt / period
        mm = np.memmap(path, dtype=np.float32, mode="w+", shape=(n,))
        for i in range(0, n, CHUNK):
            m = min(CHUNK, n - i)
            phase = (i * ang) + np.arange(m, dtype=np.float64) * ang
            sig = (np.sin(phase) + 0.3 * np.sin(phase * 4.7)).astype(np.float32)
            sig += noise[:m]
            mm[i:i + m] = sig
        mm.flush()
        del mm
        manifest["sources"].append(
            {"id": f"d{k}", "name": f"Digitizer ch{k:02d}",
             "file": f"src{k:02d}.f32", "color": "#74c0fc"})
        gb_done = (k + 1) * a.gb / a.sources
        print(f"  [{k+1:2d}/{a.sources}] {path}  ({gb_done:.0f}/{a.gb:.0f} GB, "
              f"{time.time()-started:.0f}s)", flush=True)

    with open(os.path.join(a.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"DONE: {a.gb} GB in {time.time()-started:.0f}s -> {a.out}")


if __name__ == "__main__":
    main()
