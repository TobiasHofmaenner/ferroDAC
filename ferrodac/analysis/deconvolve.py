"""Cracking-pattern deconvolution: a measured spectrum -> gas composition.

A residual-gas spectrum is, to first order, a non-negative linear combination of
the gases' fragmentation patterns:  measured(m/z) = sum_g  x_g * pattern_g(m/z).
We solve for the non-negative amounts x_g (NNLS), using *all* peaks so the
fragments disambiguate overlapping masses (28 = N2 vs CO vs a CO2 fragment is
resolved by the 12/14/16/44 ratios). Partial pressures are x_g / sensitivity.

Two robustness layers against noise:
- masses are weighted by 1/noise and sub-noise masses are dropped, so noise
  can't drive a confident wrong attribution;
- `deconvolve_mc` runs the fit many times on the spectrum perturbed by its own
  measured noise (a parametric bootstrap): the spread of each gas across runs is
  its uncertainty, and strongly anti-correlated pairs are flagged as
  unresolvable (e.g. N2 vs CO when their distinguishing fragments are buried).
"""

from __future__ import annotations

import numpy as np


def _nnls(A: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Non-negative least squares; scipy if present, else numpy coordinate descent."""
    try:
        from scipy.optimize import nnls
        return nnls(A, b)[0]
    except Exception:
        AtA = A.T @ A
        Atb = A.T @ b
        diag = np.diag(AtA) + 1e-12
        x = np.zeros(A.shape[1])
        for _ in range(400):
            for j in range(len(x)):
                x[j] = max(0.0, x[j] + (Atb[j] - AtA[j] @ x) / diag[j])
        return x


def _noise_sigma(y: np.ndarray) -> float:
    """Robust baseline-noise estimate from the whole spectrum (MAD-based)."""
    yf = y[np.isfinite(y)]
    if yf.size == 0:
        return 0.0
    return float(1.4826 * np.median(np.abs(yf - np.median(yf))))


def _at(x: np.ndarray, arr: np.ndarray, m: float) -> float:
    """Value of `arr` (aligned to x) at integer mass m: peak in its +/-0.5 window."""
    sel = np.abs(x - m) <= 0.5
    if not sel.any():
        return 0.0
    v = arr[sel]
    v = v[np.isfinite(v)]
    return float(v.max()) if v.size else 0.0


def _prepare(x, y, gases, sigma, min_snr):
    """Build the fit problem: kept masses, intensities b, per-mass noise sb, the
    pattern matrix M and the inverse-noise weights w. None if there's no signal."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    glob = _noise_sigma(y)
    lo, hi = x.min() - 0.5, x.max() + 0.5
    masses = sorted({m for g in gases for m in g.pattern if lo <= m <= hi})
    if not masses:
        return None
    b = np.clip([_at(x, y, m) for m in masses], 0.0, None)
    if b.max() <= 0:
        return None
    if sigma is None:
        sb = np.full(len(masses), glob)
    elif np.isscalar(sigma):
        sb = np.full(len(masses), float(sigma))
    else:                                       # per-mass sigma aligned to x
        sb = np.array([_at(x, np.asarray(sigma, float), m) for m in masses])
        sb = np.where(sb > 0, sb, glob)
    keep = b > min_snr * sb                     # drop sub-noise masses (no info)
    if keep.sum() < 1:
        keep = b >= b.max()
    masses = [m for m, k in zip(masses, keep) if k]
    b, sb = b[keep], sb[keep]
    M = np.array([[g.norm_pattern.get(m, 0.0) for g in gases] for m in masses])
    floor = max(float(np.median(sb)) if sb.size else 0.0, b.max() * 1e-3)
    w = 1.0 / (sb + floor)                       # inverse-noise weighting
    return masses, b, sb, M, w


def _solve(M, b, w, ngas, sparsity):
    """Weighted NNLS with optional iterative sparsity pruning."""
    keep = list(range(ngas))
    x_sol = np.zeros(ngas)
    for _ in range(ngas):
        sol = _nnls(M[:, keep] * w[:, None], b * w)
        x_sol = np.zeros(ngas)
        for i, k in enumerate(keep):
            x_sol[k] = sol[i]
        if sparsity <= 0 or x_sol.max() <= 0:
            break
        new = [k for k in keep if x_sol[k] >= sparsity * x_sol.max()]
        if len(new) == len(keep):
            break
        keep = new or keep
    return x_sol


def deconvolve(x, y, gases, sparsity: float = 0.0, min_snr: float = 2.0, sigma=None):
    """Single fit -> ``({gas: partial}, residual)``. `sigma` may be a scalar or a
    per-mass array (aligned to x); if None, a MAD baseline estimate is used."""
    gases = list(gases)
    prep = _prepare(x, y, gases, sigma, min_snr) if gases else None
    if prep is None:
        return {g.name: 0.0 for g in gases}, 1.0
    masses, b, sb, M, w = prep
    x_sol = _solve(M, b, w, len(gases), sparsity)
    resid = float(np.linalg.norm(M @ x_sol - b) / (np.linalg.norm(b) or 1.0))
    amounts = {g.name: float(x_sol[i] / (g.rsf or 1.0)) for i, g in enumerate(gases)}
    return amounts, resid


def _degenerate_pairs(P, names, thresh=-0.7):
    """Gas pairs that trade off in the bootstrap (strong anti-correlation) — i.e.
    the data can't tell them apart. Returns [(a, b, corr), ...]."""
    out = []
    sd = P.std(axis=0)
    active = [i for i in range(len(names)) if sd[i] > 0]
    if len(active) < 2:
        return out
    C = np.corrcoef(P[:, active].T)
    for ii in range(len(active)):
        for jj in range(ii + 1, len(active)):
            c = C[ii, jj]
            if c <= thresh:
                out.append((names[active[ii]], names[active[jj]], float(c)))
    return sorted(out, key=lambda t: t[2])


def deconvolve_mc(x, y, gases, runs: int = 64, sparsity: float = 0.0,
                  min_snr: float = 2.0, sigma=None, seed=None):
    """Monte-Carlo (parametric bootstrap): fit the spectrum `runs` times, each
    perturbed by its own measured noise. Returns ``(median, sd, residual, pairs)``
    — per-gas median partial pressure, its uncertainty (1 sigma), the residual of
    the median fit, and unresolvable (anti-correlated) gas pairs."""
    gases = list(gases)
    prep = _prepare(x, y, gases, sigma, min_snr) if gases else None
    if prep is None:
        z = {g.name: 0.0 for g in gases}
        return z, dict(z), 1.0, []
    masses, b, sb, M, w = prep
    rng = np.random.default_rng(seed)
    sols = np.zeros((runs, len(gases)))
    for k in range(runs):
        bk = np.clip(b + rng.normal(0.0, sb), 0.0, None)   # perturb by real noise
        sols[k] = _solve(M, bk, w, len(gases), sparsity)
    rsf = np.array([g.rsf or 1.0 for g in gases])
    P = sols / rsf                                          # -> partial pressures
    names = [g.name for g in gases]
    median = {n: float(np.median(P[:, i])) for i, n in enumerate(names)}
    sd = {n: float(np.std(P[:, i])) for i, n in enumerate(names)}
    x_med = np.median(sols, axis=0)
    resid = float(np.linalg.norm(M @ x_med - b) / (np.linalg.norm(b) or 1.0))
    return median, sd, resid, _degenerate_pairs(P, names)
