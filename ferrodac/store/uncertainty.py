"""store/uncertainty.py — the windowed σ reconstruction engine (DESIGN §19.0).

σ is a **derived lens** over raw data: never stored on the hot path, never computed over
full history — reconstructed from the declared model over the BOUNDED WINDOW a view asks
for. This module is that reconstruction. Given a window's ``(times, values)`` (exactly
what the read path returns, `store.query`), it:

  1. reads the source's σ-model epochs from the device change-log (a model can change
     over time — the Keithley's range-dependent accuracy, X2b);
  2. segments the window at those epoch boundaries;
  3. applies the model in effect at each sample → a standard-uncertainty σ array.

Pure + vectorised + Qt-free, so it runs headless / server-side and off the GUI thread.
Both compute modes (DESIGN §19.3) agree at a LEAF — σ is just the model evaluated — so
this engine serves the analytic scalar-σ path directly; the analytic-vs-exact-`ufloat`
distinction only bites when PROPAGATING through processors (derived sources, X3b).
"""
from __future__ import annotations

import numpy as np

from ..core.sourceid import uncertainty_at
from ..core.uncertainty import Uncertainty


def model_timeline(store, source_key: str) -> list:
    """The source's σ-model epochs — ``[(t, model), ...]`` sorted by time, from the
    device change-log. Empty when there's no store or nothing was logged."""
    if store is None:
        return []
    device_id, _, channel = source_key.partition("/")
    try:
        hist = store.device_meta_history(device_id, f"uncertainty:{channel}")
    except Exception:                        # noqa: BLE001 — old/missing store → no σ
        return []
    out = []
    for t, val in hist:
        try:
            m = Uncertainty.from_dict(val)   # None for an unset field
        except Exception:                    # noqa: BLE001 — unknown/corrupt model type
            m = None
        # KEEP the epoch boundary even when None: a model that became unset / an
        # unknown type at time t means σ = NaN FROM t onward — not "keep the old model".
        out.append((float(t), m))
    return out


def _apply(model, values):
    """``model.sigma(values)`` as a float ndarray. A None model (unset / unknown type),
    or one that can't compute from the value (a Measured σ needs its companion channel),
    yields NaN instead of raising — the caller renders 'no band' there rather than
    crashing or silently reusing a stale model."""
    if model is None:
        return np.full(np.shape(values), np.nan, dtype=float)
    try:
        return np.asarray(model.sigma(values), dtype=float)
    except Exception:                        # noqa: BLE001
        return np.full(np.shape(values), np.nan, dtype=float)


def reconstruct(store, source_key: str, times, values, *, now=None, timeline=None):
    """Standard uncertainty σ (1σ) for a source over a window, shaped like ``values``.
    NaN where no model applies (no declared model, or a Measured σ). Segments the window
    by the change-log's model epochs and applies the model in effect at each sample.

    ``timeline`` (from :func:`model_timeline`) may be passed in to skip the change-log
    read — a live chart caches it per source and refreshes only when a model changes,
    so drawing a band never touches the store lock on the hot path."""
    t = np.asarray(times, dtype=float)
    v = np.asarray(values, dtype=float)
    sigma = np.full(v.shape, np.nan, dtype=float)
    # nothing to do, or times/values aren't paired element-wise → no band, never a crash
    if v.size == 0 or t.shape != v.shape:
        return sigma

    if timeline is None:
        timeline = model_timeline(store, source_key)
    if not timeline:                         # no change-log → the single current model
        anchor = now if now is not None else float(t.flat[-1])
        model = uncertainty_at(store, source_key, anchor)
        return _apply(model, v) if model is not None else sigma

    ts = np.array([tt for tt, _ in timeline])
    # Each sample uses the model whose event-time is the greatest ≤ the sample time.
    # Samples before the FIRST event use the first model (declared at ~startup).
    idx = np.searchsorted(ts, t, side="right") - 1
    idx[idx < 0] = 0
    for seg in range(len(timeline)):
        mask = idx == seg
        if mask.any():
            sigma[mask] = _apply(timeline[seg][1], v[mask])
    return sigma


def band(values, sigma, k: float = 1.0):
    """``(lower, upper) = value ∓ k·σ``. k=1 → 1σ (~68 %), k=2 → ~95 %. A NaN σ gives
    NaN bounds, so a chart simply draws no band at those samples."""
    v = np.asarray(values, dtype=float)
    s = np.asarray(sigma, dtype=float) * float(k)
    return v - s, v + s
