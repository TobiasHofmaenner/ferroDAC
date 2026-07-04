"""Cracking-pattern deconvolution: a measured spectrum -> gas composition.

A residual-gas spectrum is, to first order, a non-negative linear combination of
the gases' fragmentation patterns:  measured(m/z) = sum_g  x_g * pattern_g(m/z).
We solve for the non-negative amounts x_g (NNLS), using *all* peaks so the
fragments disambiguate overlapping masses (28 = N2 vs CO vs a CO2 fragment is
resolved by the 12/14/16/44 ratios). Partial pressures are x_g / sensitivity.

Robustness layers against noise (calibrated per the §19.7 review):
- masses are weighted by 1/noise and sub-noise masses are dropped, so noise
  can't drive a confident wrong attribution;
- a gas only enters the fit if its strongest in-range fragment survived the
  noise cut — a gas seen *only* through minor fragments is a phantom (a blip on
  a shared minor mass used to manufacture multi-sigma "detections"). A gated-out
  gas reports a one-sided detection limit instead of a fake symmetric sigma;
- `deconvolve_mc` runs the fit many times on the spectrum perturbed by its own
  measured noise (a parametric bootstrap). The reported value is the
  DETERMINISTIC unperturbed fit; the bootstrap contributes only the spread, as
  an ASYMMETRIC (sigma_lo, sigma_hi) from the 16th/84th percentiles — near the
  x>=0 physical bound the distribution folds, so a symmetric +/-sigma would dip
  below zero and understate the upside. Strongly anti-correlated pairs are
  flagged as unresolvable (e.g. N2 vs CO when their distinguishing fragments
  are buried).
"""

from __future__ import annotations

import numpy as np

# 1-sigma equivalent percentiles of the bootstrap distribution
_PCT_LO, _PCT_HI = 15.865, 84.135


def _rsf(g) -> float:
    """Relative-sensitivity guard: a missing / zero / negative rsf must not blow
    up or sign-flip the partial pressure — treat it as 1.0 (uncorrected)."""
    r = getattr(g, "rsf", None)
    return float(r) if r and r > 0 else 1.0


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
    """Robust baseline-noise estimate from the whole spectrum, guaranteed POSITIVE
    whenever the spectrum has any signal (§19.7 fix B1). MAD first; on sparse /
    stick spectra (more than half the bins identical — typically hard zeros) the
    MAD collapses to 0, which used to freeze the bootstrap into 0±0 false
    certainty. Fall back to the std of the sub-median (baseline) bins, then to a
    dynamic-range floor — a single scan can't resolve arbitrarily far below its
    largest peak, so zero noise is never a credible estimate. Estimates below
    ~1e-9 of the peak are float ULP artifacts (np.std of a bit-identical baseline
    is not exactly 0), not physics — treated as collapsed, or a quantized flat
    baseline yields absurdly tight bands."""
    yf = y[np.isfinite(y)]
    if yf.size == 0:
        return 0.0
    mx = float(np.max(np.abs(yf)))
    if mx <= 0.0:
        return 0.0

    def credible(v: float) -> bool:
        return np.isfinite(v) and v > mx * 1e-9

    med = float(np.median(yf))
    s = float(1.4826 * np.median(np.abs(yf - med)))
    if not credible(s):
        base = yf[yf <= med]
        s = float(np.std(base)) if base.size else 0.0
    if not credible(s):
        s = mx * 1e-3
    return s


def _win(x: np.ndarray, m: float) -> np.ndarray:
    """The half-open ±0.5 window of integer mass m. Half-open so a sample on the
    half-mass boundary belongs to exactly ONE mass — a closed window counted it
    for both neighbours on fine grids."""
    return (x >= m - 0.5) & (x < m + 0.5)


def _at(x: np.ndarray, arr: np.ndarray, m: float) -> float:
    """Value of `arr` (aligned to x) at integer mass m: peak in its ±0.5 window."""
    sel = _win(x, m)
    if not sel.any():
        return 0.0
    v = arr[sel]
    v = v[np.isfinite(v)]
    return float(v.max()) if v.size else 0.0


def _prepare(x, y, gases, sigma, min_snr):
    """Build the fit problem: kept masses, intensities b, per-mass noise sb, the
    pattern matrix M, inverse-noise weights w, plus per-gas gating:

    - ``active[i]`` — gas i's strongest in-range fragment survived the noise cut.
      Only active gases enter the fit: quantifying a gas from minor fragments
      alone is the §19.7 phantom mechanism (a noise blip on a shared minor mass
      is attributed to a gas whose base peak is demonstrably absent).
    - ``limit[i]`` — the one-sided detection limit (in fit units, before /rsf):
      the OBSERVED base-peak intensity plus the noise cut, over the base fraction
      — the standard "measured + k·σ" upper-limit construction. (A noise-only
      limit that ignored the observed intensity excluded the truth in EVERY
      frame where a just-above-limit gas fluctuated below the cut — §19.7.)
      NaN when the gas has no fragment in the scanned range (no information).

    None if there's no signal."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    glob = _noise_sigma(y)
    lo, hi = x.min() - 0.5, x.max() + 0.5
    all_masses = sorted({m for g in gases for m in g.pattern if lo <= m <= hi})
    if not all_masses:
        return None
    b_all = np.clip([_at(x, y, m) for m in all_masses], 0.0, None)
    if b_all.max() <= 0:
        return None
    if sigma is None:
        sb_all = np.full(len(all_masses), glob)
    elif np.isscalar(sigma):
        sb_all = np.full(len(all_masses), float(sigma))
    else:                                       # per-mass sigma aligned to x
        sb_all = np.array([_at(x, np.asarray(sigma, float), m) for m in all_masses])
        sb_all = np.where(sb_all > 0, sb_all, glob)
    # The per-mass value is a WINDOW MAX: on a fine grid the max of n noise
    # samples sits at ~sqrt(2 ln n)·σ, so a bare min_snr cut is structurally
    # dead there (pure noise kept 5-7 "present" gases — §19.7). Raise the cut
    # by the expected noise-max (look-elsewhere within the window); n=1 sticks
    # are unaffected.
    n_win = np.array([max(int(np.count_nonzero(_win(x, m))), 1)
                      for m in all_masses], dtype=float)
    xfac = np.where(n_win > 1, np.sqrt(2.0 * np.log(np.maximum(n_win, 2.0))), 0.0)
    cut_all = (min_snr + xfac) * sb_all
    # No fallback when nothing beats the cut: "keep the strongest mass anyway"
    # manufactured a detection of the largest NOISE excursion. An empty kept set
    # is honest — every gas gates out to its one-sided upper limit.
    keep = b_all > cut_all                      # drop sub-noise masses (no info)
    kept = {m for m, k in zip(all_masses, keep) if k}
    idx = {m: i for i, m in enumerate(all_masses)}
    active = np.zeros(len(gases), dtype=bool)
    limit = np.full(len(gases), np.nan)
    for i, g in enumerate(gases):
        in_range = [m for m in g.norm_pattern if lo <= m <= hi]
        if not in_range:
            continue                            # unmeasurable here → limit stays NaN
        base = max(in_range, key=lambda m: g.norm_pattern[m])
        frac = g.norm_pattern[base]
        if frac > 0:
            j = idx[base]
            limit[i] = (b_all[j] + cut_all[j]) / frac
        active[i] = base in kept
    masses = [m for m, k in zip(all_masses, keep) if k]
    b, sb = b_all[keep], sb_all[keep]
    M = np.array([[g.norm_pattern.get(m, 0.0) for g in gases]
                  for m in masses]).reshape(len(masses), len(gases))
    floor = max(float(np.median(sb)) if sb.size else 0.0,
                float(b.max()) * 1e-3 if b.size else 1.0)
    w = 1.0 / (sb + floor)                       # inverse-noise weighting
    return masses, b, sb, M, w, active, limit


def _solve_full(M, b, w, ngas, sparsity, active=None):
    """Weighted NNLS over the active gases, with optional iterative sparsity
    pruning. Returns ``(x_sol, x_first)`` — the final solution and the FIRST
    (pre-pruning) pass: a gas zeroed by the sparsity heuristic still has real
    fitted support there, which bounds its honest upper limit (§19.7 — the noise
    detection limit lies by 35× for a bright-but-relatively-small peak)."""
    keep = [j for j in range(ngas) if active is None or active[j]]
    x_sol = np.zeros(ngas)
    x_first = np.zeros(ngas)
    if not keep:
        return x_sol, x_first
    for it in range(ngas):
        sol = _nnls(M[:, keep] * w[:, None], b * w)
        x_sol = np.zeros(ngas)
        for i, k in enumerate(keep):
            x_sol[k] = sol[i]
        if it == 0:
            x_first = x_sol.copy()
        if sparsity <= 0 or x_sol.max() <= 0:
            break
        new = [k for k in keep if x_sol[k] >= sparsity * x_sol.max()]
        if len(new) == len(keep):
            break
        keep = new or keep
    return x_sol, x_first


def _solve(M, b, w, ngas, sparsity, active=None):
    return _solve_full(M, b, w, ngas, sparsity, active)[0]


def deconvolve(x, y, gases, sparsity: float = 0.0, min_snr: float = 2.0, sigma=None):
    """Single fit -> ``({gas: partial}, residual)``. `sigma` may be a scalar or a
    per-mass array (aligned to x); if None, a MAD baseline estimate is used."""
    gases = list(gases)
    prep = _prepare(x, y, gases, sigma, min_snr) if gases else None
    if prep is None:
        return {g.name: 0.0 for g in gases}, 1.0
    masses, b, sb, M, w, active, limit = prep
    x_sol = _solve(M, b, w, len(gases), sparsity, active)
    resid = 1.0 if b.size == 0 else \
        float(np.linalg.norm(M @ x_sol - b) / (np.linalg.norm(b) or 1.0))
    amounts = {g.name: float(x_sol[i] / _rsf(g)) for i, g in enumerate(gases)}
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
    perturbed by its own measured noise. Returns ``(amounts, err, residual, pairs)``:

    - ``amounts`` — the DETERMINISTIC unperturbed fit per gas (identical to
      :func:`deconvolve`); the bootstrap only sizes the error, it never moves the
      value (an MC median flickers frame-to-frame and biases up at the x>=0 bound).
    - ``err`` — ``{gas: (sigma_lo, sigma_hi)}``, the 16th/84th bootstrap
      percentiles around the value, clamped so value - sigma_lo >= 0 (the x>=0
      bound folds the distribution: honest errors there are asymmetric). A gas
      GATED OUT of the fit (base peak below the noise cut) reports a one-sided
      upper limit ``(0, observed_base + cut, over its base fraction)``; a gas the
      fit itself zeroed keeps at least its pre-sparsity fitted support as the
      upper limit; a gas with no fragment in the scanned range — and EVERY gas
      of a spectrum with no signal at all — reports ``(nan, nan)``: no
      information, never a certain 0±0.
    - ``residual`` — relative residual of the deterministic fit.
    - ``pairs`` — unresolvable (anti-correlated) gas pairs.

    Pass a fixed ``seed`` for reproducible errors (the GUI does, so bands don't
    flicker from re-drawn bootstrap noise on an unchanged spectrum)."""
    gases = list(gases)
    nan2 = (float("nan"), float("nan"))
    prep = _prepare(x, y, gases, sigma, min_snr) if gases else None
    if prep is None:                            # no signal → no information (§19.7:
        z = {g.name: 0.0 for g in gases}        # an all-zero/negative-baseline scan
        return z, {g.name: nan2 for g in gases}, 1.0, []   # must NOT claim 0±0)
    masses, b, sb, M, w, active, limit = prep
    rsf = np.array([_rsf(g) for g in gases])
    names = [g.name for g in gases]
    x0, x_first = _solve_full(M, b, w, len(gases), sparsity, active)  # the reported fit
    center = x0 / rsf
    resid = 1.0 if b.size == 0 else \
        float(np.linalg.norm(M @ x0 - b) / (np.linalg.norm(b) or 1.0))
    rng = np.random.default_rng(seed)
    sols = np.zeros((runs, len(gases)))
    for k in range(runs):
        bk = np.clip(b + rng.normal(0.0, sb), 0.0, None)   # perturb by real noise
        sols[k] = _solve(M, bk, w, len(gases), sparsity, active)
    P = sols / rsf                                          # -> partial pressures
    lo_q = np.percentile(P, _PCT_LO, axis=0)
    hi_q = np.percentile(P, _PCT_HI, axis=0)
    err = {}
    for i, n in enumerate(names):
        ul = limit[i] / rsf[i]                              # NaN = no info in range
        if not active[i]:
            err[n] = (0.0, float(ul)) if ul == ul else nan2
            continue
        s_lo = float(min(center[i], max(center[i] - lo_q[i], 0.0)))  # band floor at 0
        s_hi = float(max(hi_q[i] - center[i], 0.0))
        if center[i] <= 0.0:
            # The fit zeroed an active gas. If sparsity pruned it, its pre-pruning
            # fitted support is real evidence — the honest upper limit is at least
            # that (§19.7: "He < noise-limit" while a 70σ He peak is in the scan),
            # and never below the base-peak detection limit.
            s_hi = max(s_hi, float(x_first[i] / rsf[i]),
                       float(ul) if ul == ul else 0.0)
        elif s_hi <= 0.0:                      # bootstrap collapsed (no upward spread):
            s_hi = float(ul) if ul == ul else 0.0   # detection limit, not ±0 certainty
        err[n] = (s_lo, s_hi)
    amounts = {n: float(center[i]) for i, n in enumerate(names)}
    return amounts, err, resid, _degenerate_pairs(P, names)
