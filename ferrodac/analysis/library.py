"""Cracking-pattern library for residual-gas analysis.

Each gas has its 70 eV electron-ionization fragmentation pattern (m/z -> relative
intensity, base peak = 100) and a relative ionization sensitivity factor (vs.
N2 = 1.0) used for quantitative partial pressures.

Values are the standard EI patterns from the **NIST Chemistry WebBook** (SRD 69,
public domain, 15 U.S.C. 105), rounded; sensitivity factors are typical RGA/gauge
values and are instrument-dependent. This is the curated default set — the full
searchable library (NIST/MoNA download) is a later phase. Patterns of common
gases are reliable; solvent/oil patterns are approximate catch-alls.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Gas:
    name: str
    formula: str
    pattern: dict          # {m/z(int): relative intensity, base peak = 100}
    rsf: float = 1.0       # relative ionization sensitivity (N2 = 1.0)

    @property
    def norm_pattern(self) -> dict:
        """Pattern as fractions summing to 1 — a column of the fit matrix."""
        tot = float(sum(self.pattern.values())) or 1.0
        return {m: v / tot for m, v in self.pattern.items()}


# --- the curated residual-gas library (NIST 70 eV EI) --------------------- #
_GASES = [
    Gas("H2",  "H2",  {2: 100, 1: 2},                                  rsf=0.44),
    Gas("He",  "He",  {4: 100},                                        rsf=0.14),
    Gas("CH4", "CH4", {16: 100, 15: 86, 14: 16, 13: 8, 12: 3, 1: 4},   rsf=1.6),
    Gas("H2O", "H2O", {18: 100, 17: 23, 16: 1, 1: 1},                  rsf=1.0),
    Gas("NH3", "NH3", {17: 100, 16: 80, 15: 8, 14: 2},                 rsf=1.3),
    Gas("Ne",  "Ne",  {20: 100, 22: 10, 21: 0.3},                      rsf=0.23),
    Gas("N2",  "N2",  {28: 100, 14: 7, 29: 0.7},                       rsf=1.0),
    Gas("CO",  "CO",  {28: 100, 12: 5, 16: 2, 29: 1},                  rsf=1.05),
    Gas("NO",  "NO",  {30: 100, 14: 8, 15: 2, 16: 1.5},               rsf=1.2),
    Gas("O2",  "O2",  {32: 100, 16: 11, 34: 0.4},                      rsf=1.0),
    Gas("Ar",  "Ar",  {40: 100, 20: 15, 36: 0.3},                      rsf=1.2),
    Gas("CO2", "CO2", {44: 100, 28: 11, 16: 9, 12: 6, 45: 1, 22: 2},   rsf=1.4),
    Gas("NO2", "NO2", {30: 100, 46: 37, 16: 22, 14: 10},               rsf=1.5),
    Gas("Methanol",  "CH3OH",  {31: 100, 32: 67, 29: 46, 28: 7, 15: 13, 30: 8},
        rsf=1.8),
    Gas("Ethanol",   "C2H5OH", {31: 100, 45: 50, 29: 30, 27: 23, 46: 22, 43: 8,
                                15: 7}, rsf=2.9),
    Gas("Acetone",   "C3H6O",  {43: 100, 58: 24, 15: 18, 42: 8, 27: 5, 14: 3},
        rsf=3.6),
    Gas("IPA",       "C3H8O",  {45: 100, 43: 17, 27: 16, 41: 8, 29: 10, 39: 6,
                                59: 3}, rsf=3.5),
    Gas("Hydrocarbons", "CnHm", {43: 100, 57: 90, 41: 75, 55: 60, 29: 50, 71: 45,
                                 69: 40, 27: 40, 85: 30, 39: 30, 83: 20}, rsf=3.0),
]

LIBRARY: dict[str, Gas] = {g.name: g for g in _GASES}

# Sensible default candidate set for vacuum residual-gas analysis.
DEFAULT_GASES = ["H2", "H2O", "N2", "O2", "Ar", "CO", "CO2", "CH4", "He", "Ne"]


def get_gases(names) -> list[Gas]:
    """Resolve gas names to Gas objects, skipping unknown ones."""
    return [LIBRARY[n] for n in names if n in LIBRARY]


def all_names() -> list[str]:
    return list(LIBRARY)
