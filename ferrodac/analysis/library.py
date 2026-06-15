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


# --------------------------------------------------------------------------- #
#  Extensible store: import EI spectra (NIST/MoNA MSP) into the live library
# --------------------------------------------------------------------------- #
import json
import os
import re
import urllib.request

_CURATED_NAMES = set(LIBRARY)            # the bundled defaults (never cached out)


def _cache_dir() -> str:
    d = os.path.join(os.path.expanduser("~"), "Documents", "ferroDAC", "library")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_file() -> str:
    return os.path.join(_cache_dir(), "compounds.json")


def add_compounds(gases) -> None:
    """Merge gases into the live library (overwriting same-name entries)."""
    for g in gases:
        LIBRARY[g.name] = g


def search(query: str, limit: int = 300) -> list:
    """Library entries whose name or formula contains `query` (case-insensitive)."""
    q = (query or "").lower().strip()
    if not q:
        return list(LIBRARY.values())[:limit]
    hit = [g for g in LIBRARY.values()
           if q in g.name.lower() or q in g.formula.lower()]
    hit.sort(key=lambda g: (not g.name.lower().startswith(q), g.name.lower()))
    return hit[:limit]


def parse_msp(text: str, max_mw: float = 250.0) -> list:
    """Parse an MSP file (NIST/MoNA EI export) into Gas patterns, keeping
    low-mass (RGA-relevant) compounds and normalising to base peak = 100."""
    out, by_name = [], {}
    for block in re.split(r"\n[ \t]*\n", text):
        if not block.strip():
            continue
        name = formula = None
        mw = None
        peaks: dict = {}
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            kv = re.match(r"([A-Za-z][\w ]*?)\s*[:=]\s*(.*)", line)
            if kv and not re.match(r"^[\d.+-]", line):
                key = kv.group(1).lower().strip()
                val = kv.group(2).strip()
                if key == "name" and name is None:
                    name = val
                elif key == "formula":
                    formula = val
                elif key in ("mw", "molecular weight") and mw is None:
                    try:
                        mw = float(val)
                    except ValueError:
                        pass
                elif key in ("exactmass", "exact mass") and mw is None:
                    try:
                        mw = float(re.split(r"[ /]", val)[0])
                    except ValueError:
                        pass
                continue
            for a, b in re.findall(r"(\d+\.?\d*)[\s,;:]+(\d+\.?\d*)", line):
                m = int(round(float(a)))
                peaks[m] = peaks.get(m, 0.0) + float(b)
        if not (name and peaks):
            continue
        if mw is not None and mw > max_mw:
            continue
        mx = max(peaks.values()) or 1.0
        pat = {k: round(v / mx * 100.0, 2) for k, v in peaks.items()
               if v / mx * 100.0 >= 0.5}            # drop noise < 0.5 % of base
        if not pat:
            continue
        g = Gas(name, formula or "", pat, rsf=1.0)   # generic sensitivity factor
        if name not in by_name:                      # de-dupe by name (keep first)
            by_name[name] = g
            out.append(g)
    return out


def import_msp(path: str, max_mw: float = 250.0) -> int:
    """Import an MSP file into the library and persist it; returns #compounds."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        gases = parse_msp(fh.read(), max_mw)
    add_compounds(gases)
    _save_cache()
    return len(gases)


# Best-effort bulk URL (MoNA GC-MS MSP, CC BY 4.0). The download page generates
# links dynamically, so this can rot — Import-from-file is the reliable path.
MONA_GCMS_MSP = ("https://mona.fiehnlab.ucdavis.edu/rest/downloads/retrieve/"
                 "MoNA-export-GC-MS_Spectra.msp")


def download_library(url: str = MONA_GCMS_MSP, max_mw: float = 250.0) -> int:
    """Best-effort: fetch an MSP bulk export and import it. May fail if the URL
    has changed — callers should fall back to import_msp()."""
    dest = os.path.join(_cache_dir(), "download.msp")
    urllib.request.urlretrieve(url, dest)
    return import_msp(dest, max_mw)


def _save_cache() -> None:
    data = [{"name": g.name, "formula": g.formula, "rsf": g.rsf,
             "pattern": {str(k): v for k, v in g.pattern.items()}}
            for g in LIBRARY.values() if g.name not in _CURATED_NAMES]
    try:
        with open(_cache_file(), "w") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _load_cache() -> None:
    try:
        with open(_cache_file()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    add_compounds([Gas(d["name"], d.get("formula", ""),
                       {int(k): v for k, v in d["pattern"].items()},
                       d.get("rsf", 1.0)) for d in data])


_load_cache()                                        # restore imports on startup
