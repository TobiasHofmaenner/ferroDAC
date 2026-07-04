"""Device uncertainty specs — the σ MODELS for ferroDAC's built-in drivers, in ONE
place so the numbers (and where they came from) are auditable (DESIGN §19.0).

Datasheet accuracy figures are BOUNDS ("guaranteed within ±…"). With no stated
distribution, GUM treats a bound as the half-width of a RECTANGULAR distribution →
standard uncertainty σ = bound/√3. That divisor is applied here so every model returned
is a 1σ standard uncertainty, consistent with :mod:`ferrodac.core.uncertainty`.

Sources:
- Keithley 6221: Reference Manual 622x-901-01 (Jun 2005), "SOURCE SPECIFICATIONS".
- Pfeiffer compact gauges (TPG 256 A): manufacturer typical "% of reading" for N2.
- Shelly H&T: Shelly publishes no accuracy → typical SHT3x-class sensor figures.
"""
import math

from ..core.uncertainty import Abs, FloorRel, Rel, Spec

K_RECT = math.sqrt(3.0)      # rectangular bound → 1σ (GUM default, no stated distribution)


def rel_bound(pct: float) -> Rel:
    """A ±pct-%-of-reading accuracy BOUND as a 1σ relative model."""
    return Rel((pct / 100.0) / K_RECT)


# --------------------------------------------------------------------------- #
#  Vacuum gauges (Pfeiffer compact gauges on the TPG 256 A)
# --------------------------------------------------------------------------- #
# Accuracy is gauge-TYPE dependent, quoted as % of reading. Figures are manufacturer
# typicals for N2, treated as bounds. TODO(verify): confirm against the actual gauges.
_GAUGE_PCT = [
    (("capacitance", "cmr", "ccr"), 0.2),                # capacitance diaphragm — precise
    (("pirani", "tpr", "pcr"), 10.0),                    # Pirani — ±10 % of reading
    (("bayard", "alpert", "b-a", "hot cathode"), 15.0),  # hot-cathode ion gauge — ±15 %
    (("fullrange", "full range", "pkr", "mpt"), 30.0),   # Pirani + cold-cathode combo
    (("cold", "ikr", "penning"), 30.0),                  # cold-cathode — ±30 %
]


def gauge_uncertainty(label: str) -> Spec:
    """σ model for a vacuum gauge from its type label (a SYSTEMATIC % of reading).
    Falls back to a generic compact-gauge ±15 % when the type is unrecognised."""
    s = (label or "").casefold()
    for keys, pct in _GAUGE_PCT:
        if any(k in s for k in keys):
            return Spec(systematic=rel_bound(pct))
    return Spec(systematic=rel_bound(15.0))              # generic compact gauge


# --------------------------------------------------------------------------- #
#  Keithley 6221 DC current source
# --------------------------------------------------------------------------- #
# Ref. manual "SOURCE SPECIFICATIONS": Accuracy (1 Year, 23°C±5°C) = ±(%rdg + offset)
# per range (a SYSTEMATIC gain/offset bound), plus the RMS-noise column (the RANDOM
# part, already 1σ). Range-dependent → re-declared per config-epoch when the range or
# level changes (X2b). Row = (max_amps, rdg_%, offset_A, rms_noise_A).
_KEITHLEY_RANGES = [
    (2e-9,   0.4,  2e-12,   80e-15),
    (20e-9,  0.3,  10e-12,  0.8e-12),
    (200e-9, 0.3,  100e-12, 4e-12),
    (2e-6,   0.1,  1e-9,    40e-12),
    (20e-6,  0.05, 10e-9,   0.4e-9),
    (200e-6, 0.05, 100e-9,  4e-9),
    (2e-3,   0.05, 1e-6,    40e-9),
    (20e-3,  0.05, 10e-6,   0.4e-6),
    (100e-3, 0.1,  50e-6,   2e-6),
]


def keithley_current_uncertainty(amps: float) -> Spec:
    """σ model for the 6221 output current at level ``amps``: the range that holds
    |amps| (+5 % over-range) → Spec(random = RMS noise, systematic = the ±(%rdg+offset)
    accuracy bound → 1σ). Re-evaluate whenever the level/range changes."""
    a = abs(float(amps))
    for max_a, pct, offset, rms in _KEITHLEY_RANGES:
        if a <= max_a * 1.05:
            break
    else:
        max_a, pct, offset, rms = _KEITHLEY_RANGES[-1]
    return Spec(random=Abs(rms),
                systematic=FloorRel(offset / K_RECT, (pct / 100.0) / K_RECT))


# --------------------------------------------------------------------------- #
#  Shelly H&T (SHT3x-class sensor; Shelly publishes no accuracy)
# --------------------------------------------------------------------------- #
# TODO(verify): typical SHT3x-class consumer-grade figures, as bounds → 1σ.
SHELLY_TEMP = Spec(systematic=Abs(0.5 / K_RECT))         # ±0.5 °C
SHELLY_HUMIDITY = Spec(systematic=Abs(5.0 / K_RECT))     # ±5 %RH
