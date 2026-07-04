"""Deconvolution error calibration — the §19.7 fixes, pinned. The bootstrap sd
used to (a) freeze into 0±0 false certainty on stick spectra (MAD noise → 0),
(b) manufacture multi-sigma phantom "detections" of gases whose base peak was
demonstrably absent, and (c) report symmetric ±sigma dipping below the physical
x>=0 floor. Qt-free."""
import numpy as np

from ferrodac.analysis.deconvolve import (_noise_sigma, _rsf, deconvolve,
                                          deconvolve_mc)
from ferrodac.analysis.library import LIBRARY, Gas, get_gases


def _spectrum(gas_amounts: dict, x_lo=1.0, x_hi=50.0, extra: dict = None):
    """A stick spectrum (integer masses, hard-zero baseline — the §19.7 killer
    case) from gas amounts; `extra` adds raw blips at given masses."""
    x = np.arange(x_lo, x_hi)
    y = np.zeros_like(x)
    for name, amt in gas_amounts.items():
        g = LIBRARY[name]
        for m, frac in g.norm_pattern.items():
            y[np.abs(x - m) <= 0.5] += amt * _rsf(g) * frac
    for m, v in (extra or {}).items():
        y[np.abs(x - m) <= 0.5] += v
    return x, y


def test_noise_sigma_positive_on_stick_spectrum():
    # >half the bins are hard zeros → the MAD is 0; the estimate must NOT be
    # (that zero froze the bootstrap → every gas came out 0±0)
    y = np.zeros(50)
    y[28], y[18] = 100.0, 40.0
    assert _noise_sigma(y) > 0.0


def test_bootstrap_not_frozen_on_stick_spectrum():
    gases = get_gases(["N2", "H2O"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0})
    amounts, err, _resid, _pairs = deconvolve_mc(x, y, gases, runs=64, seed=1)
    for n in ("N2", "H2O"):
        assert amounts[n] > 0.0
        s_lo, s_hi = err[n]
        assert s_hi > 0.0                       # no 0±0 false certainty


def test_value_is_the_deterministic_fit_not_the_mc_median():
    gases = get_gases(["N2", "H2O", "O2"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0, "O2": 10.0})
    single, _ = deconvolve(x, y, gases)
    amounts, _err, _r, _p = deconvolve_mc(x, y, gases, runs=32, seed=3)
    for n, v in single.items():
        assert amounts[n] == v                  # bootstrap sizes the error only


def test_absent_gas_reports_upper_limit_not_a_phantom():
    # CO2's base peak (44) carries no signal → the gas must be gated out of the
    # fit entirely: value exactly 0 with a one-sided (0, upper-limit) error —
    # never a symmetric ±sigma around a manufactured detection.
    gases = get_gases(["N2", "H2O", "CO2"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0})
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=64, seed=1)
    assert amounts["CO2"] == 0.0
    s_lo, s_hi = err["CO2"]
    assert s_lo == 0.0 and np.isfinite(s_hi) and s_hi > 0.0


def test_minor_mass_blip_cannot_manufacture_a_detection():
    # The verified §19.7 phantom: a small blip on a MINOR CO2 fragment (m/z 12)
    # while its base peak (44) is empty used to fit as a large CO2 amount. The
    # base-peak gate must keep CO2 at exactly 0 regardless of the blip.
    gases = get_gases(["N2", "H2O", "CO2"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0}, extra={12: 1.5})
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=64, seed=1)
    assert amounts["CO2"] == 0.0
    assert deconvolve(x, y, gases)[0]["CO2"] == 0.0     # single fit gated too


def test_band_lower_edge_never_below_zero():
    # Partial pressures are physical (x>=0): value - sigma_lo must stay >= 0 for
    # every gas, including weak ones folded against the bound.
    gases = get_gases(["N2", "H2O", "CO2", "O2", "Ar"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0, "CO2": 1.0, "O2": 0.4})
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=64, seed=2)
    for n, (s_lo, s_hi) in err.items():
        if s_lo == s_lo:                        # skip no-information NaNs
            assert amounts[n] - s_lo >= -1e-12, n


def test_seeded_bootstrap_is_reproducible():
    gases = get_gases(["N2", "H2O"])
    x, y = _spectrum({"N2": 100.0, "H2O": 40.0})
    a1 = deconvolve_mc(x, y, gases, runs=32, seed=7)
    a2 = deconvolve_mc(x, y, gases, runs=32, seed=7)
    assert a1[0] == a2[0] and a1[1] == a2[1]    # same seed → same value AND error


def test_gas_outside_the_scanned_range_reports_no_information():
    # He (only fragment m/z 4) in a scan starting at 10: no data at all about it
    # → value 0 with a (nan, nan) error, NOT a confident 0±0.
    gases = get_gases(["N2", "He"])
    x, y = _spectrum({"N2": 100.0}, x_lo=10.0, x_hi=35.0)
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=16, seed=1)
    assert amounts["He"] == 0.0
    assert all(v != v for v in err["He"])


def test_nonpositive_rsf_cannot_blow_up_the_fit():
    bad = Gas("X2", "X2", {28: 100, 14: 7}, rsf=0.0)     # broken sensitivity
    x, y = _spectrum({"N2": 100.0})
    amounts, _ = deconvolve(x, y, [bad])
    assert np.isfinite(amounts["X2"]) and amounts["X2"] > 0.0   # treated as rsf=1


# --- the adversarial-verification round (workflow, 2026-07-04) ------------------

def test_gated_upper_limit_covers_a_truth_just_above_the_cut():
    """A present gas whose base peak fluctuates below the noise cut gets gated —
    the reported upper limit must still COVER the truth (it uses the observed
    intensity + the cut; a noise-only limit excluded the truth in 100% of gated
    frames)."""
    gases = get_gases(["N2", "H2O", "Ar"])
    truth = 3.0
    x = np.arange(1.0, 50.0)
    y = np.zeros_like(x)
    for n, amt in {"N2": 100.0, "Ar": truth}.items():
        g = LIBRARY[n]
        for m, frac in g.norm_pattern.items():
            y[np.abs(x - m) <= 0.5] += amt * _rsf(g) * frac
    rng = np.random.default_rng(3)                 # this seed dips m/z 40 below 2σ
    y = y + rng.normal(0.0, 1.0, y.shape)
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=64, sigma=1.0, seed=0)
    assert amounts["Ar"] == 0.0                    # gated (deterministic w/ this seed)
    assert err["Ar"][1] >= truth                   # the limit covers the truth


def test_sparsity_pruned_gas_keeps_its_fitted_support_as_upper_limit():
    """A 70σ He peak zeroed by the SPARSITY heuristic (0.16% of the dominant H2)
    must not report the noise detection limit ('He < 14' vs truth 500) — its
    pre-pruning fitted support bounds the honest limit."""
    gases = get_gases(["H2", "He"])
    x, y = _spectrum({"H2": 1e5, "He": 500.0})
    y = y + np.random.default_rng(7).normal(0.0, 1.0, y.shape)
    amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=32,
                                         sparsity=0.05, sigma=1.0, seed=1)
    assert amounts["He"] == 0.0                    # pruned (0.16% < 5% sparsity)
    assert err["He"][1] >= 450.0                   # ≥ its actual fitted support


def test_no_signal_spectrum_reports_no_information_not_zero_zero():
    """An all-zero (or negative-baseline, clipped-to-zero) spectrum carries NO
    information: every gas must report (nan, nan), never a certain 0±0."""
    gases = get_gases(["N2", "CO2", "H2O"])
    x = np.arange(1.0, 50.0)
    for y in (np.zeros_like(x),
              -np.abs(np.random.default_rng(3).normal(0.0, 1.0, x.shape))):
        amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=16, seed=1)
        for n in amounts:
            assert amounts[n] == 0.0
            assert all(v != v for v in err[n]), n  # (nan, nan) — no info


def test_half_open_window_counts_a_boundary_sample_once():
    """A sample exactly on the half-mass boundary must belong to ONE mass window
    (fine grids: a closed ±0.5 window fed a real peak's shoulder to BOTH masses)."""
    from ferrodac.analysis.deconvolve import _at
    x = np.array([20.5])
    y = np.array([5.0])
    assert _at(x, y, 20) == 0.0                    # not in [19.5, 20.5)
    assert _at(x, y, 21) == 5.0                    # in [20.5, 21.5)


def test_pure_noise_fine_grid_yields_no_confident_detection():
    """On a fine grid the window-max of n noise samples sits at ~sqrt(2·ln n)·σ, so
    a bare 2σ cut kept 5-7 'present' gases from PURE NOISE. With the look-elsewhere
    correction no gas's band may exclude zero."""
    x = np.linspace(1.0, 50.0, 49 * 8 + 1)
    gases = get_gases(["N2", "H2O", "O2", "CO2", "He", "CH4"])
    for seed in range(4):
        y = np.random.default_rng(seed).normal(0.0, 1.0, x.shape)
        amounts, err, _r, _p = deconvolve_mc(x, y, gases, runs=32,
                                             sigma=1.0, seed=1)
        for n, v in amounts.items():
            s_lo = err[n][0]
            if s_lo == s_lo:                       # finite → band bottom must touch 0
                assert v - s_lo <= 1e-9, (seed, n, v, err[n])


def test_noise_floor_survives_a_quantized_flat_baseline():
    """A constant nonzero baseline (ADC-quantized underrange) made np.std return
    float-ULP garbage instead of 0, bypassing the dynamic-range floor → absurdly
    tight bands. Sub-1e-9-of-peak estimates are artifacts, not physics."""
    x, y = _spectrum({"N2": 1e-8})
    y = np.where(y == 0.0, 2e-11, y)               # flat quantized baseline
    assert _noise_sigma(y) >= 1e-12                # ≥ the 1e-3 dynamic-range floor
    with np.errstate(over="ignore"):               # np.std overflows internally —
        s = _noise_sigma(np.full(50, 1e300))       # the inf must be caught
    assert np.isfinite(s)
