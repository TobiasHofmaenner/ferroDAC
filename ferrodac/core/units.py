"""core/units.py — the unit AUTHORITY (DESIGN §19.1; first-class, 2026-07-04).

A **thin edge adapter over `pint`**, not a units system of its own. pint owns all the
real logic — the registry, dimensional algebra, affine temperature (°C↔K is an
*offset*, not a scale), SI prefixes, and conversions. This module adds only the three
things pint can't know about our codebase:

  1. ONE shared ``UnitRegistry`` (Quantities from different registries don't
     interoperate — there must be exactly one authority everyone routes through);
  2. normalisation of the messy real-world unit STRINGS we already carry
     (``°C``, ``% RH``, ``a.u.``) into something pint parses;
  3. small ``validate/canonical/compatible/convert`` wrappers, each a 1–3 line call
     into pint — no dimensional arithmetic is written by hand here.

Values stay plain ``float64`` on the wire and in the store (§6); a ``Quantity`` is
materialised only at the EDGES this module serves — display conversion, dimensional
routing checks, export. Qt-free and import-light, so drivers, the store, and headless
tests all use it.
"""
from __future__ import annotations

import functools
import logging

import numpy as np
import pint

log = logging.getLogger(__name__)

# One registry for the whole process. pint interns units per-registry, so this MUST
# be the single shared instance — never construct another.
_UREG = pint.UnitRegistry()
# 'a.u.' / '' (arbitrary units) → a dimensionless placeholder. Kept distinct from a
# true ``dimensionless`` only in name; dimensionally it IS dimensionless, so an a.u.
# channel routes freely against other dimensionless quantities (ratios, %).
_UREG.define("arbitrary_unit = [] = a_u")

# Real-world strings ferroDAC already carries that pint won't parse verbatim. Matched
# on the stripped string (exact, then case-folded) BEFORE handing off to pint.
_ALIASES = {
    "°c": "degC", "degc": "degC", "c": "degC", "celsius": "degC",
    "°f": "degF", "degf": "degF", "fahrenheit": "degF",
    "k": "kelvin",
    "%": "percent", "%rh": "percent", "% rh": "percent", "pct": "percent",
    "rh": "arbitrary_unit",
    "": "arbitrary_unit", "a.u.": "arbitrary_unit", "au": "arbitrary_unit",
    "arb": "arbitrary_unit", "arbitrary": "arbitrary_unit",
}


def registry() -> pint.UnitRegistry:
    """The single shared registry. Use this to build Quantities elsewhere so they
    interoperate with everything this module produces."""
    return _UREG


def _normalise(unit: str) -> str:
    s = (unit or "").strip()
    return _ALIASES.get(s, _ALIASES.get(s.casefold(), s))


@functools.lru_cache(maxsize=4096)
def parse(unit: str):
    """The pint ``Unit`` for a (possibly messy) string, or ``None`` if unparseable.
    The caller decides the fallback (usually ``arbitrary_unit``) — this never raises."""
    norm = _normalise(unit)
    try:
        return _UREG.parse_units(norm)      # exact casing wins (Pa ≠ pa)
    except Exception:
        pass
    try:
        return _UREG.parse_units(norm, case_sensitive=False)   # Torr → torr
    except Exception:                       # UndefinedUnit / Dimensionality / syntax
        log.debug("unparseable unit %r (normalised %r)", unit, norm)
        return None


def is_valid(unit: str) -> bool:
    return parse(unit) is not None


@functools.lru_cache(maxsize=4096)
def canonical(unit: str) -> str:
    """The canonical short/symbol form pint prints (e.g. ``mbar`` → ``mbar``), or the
    original string unchanged when unparseable — so a validated unit is stored in one
    consistent spelling without ever losing an unknown label."""
    u = parse(unit)
    return f"{u:~}" if u is not None else (unit or "")


@functools.lru_cache(maxsize=4096)
def dimensionality(unit: str):
    """pint's dimensionality container (e.g. pressure → ``[mass]/[length]/[time]**2``),
    or ``None`` if unparseable. Two units are interchangeable on an axis iff these match."""
    u = parse(unit)
    return u.dimensionality if u is not None else None


@functools.lru_cache(maxsize=8192)
def compatible(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` share a dimensionality (so one converts to the other).
    Unparseable units are treated as incompatible with everything except an equal
    string (fail closed for routing, but don't block same-label historic channels)."""
    da, db = dimensionality(a), dimensionality(b)
    if da is None or db is None:
        return _normalise(a) == _normalise(b)
    return da == db


def convert(values, src: str, dst: str):
    """Convert magnitudes ``src`` → ``dst`` (scalar or ndarray), handling affine units
    (°C↔K) correctly via pint. Returns a float/ndarray, or ``None`` if the units are
    unparseable or dimensionally incompatible. This is the display/export edge."""
    su, du = parse(src), parse(dst)
    if su is None or du is None:
        return None
    try:
        q = _UREG.Quantity(np.asarray(values, dtype=float), su)
        out = q.to(du).magnitude
    except Exception:                       # DimensionalityError etc.
        return None
    return out if out.ndim else float(out)


def convert_factor(src: str, dst: str):
    """The pure multiplicative factor ``src`` → ``dst`` (``mbar→Pa`` = 100.0), for the
    common offset-free case where a hot loop wants one scalar. Returns ``None`` for
    affine pairs (temperature) — use :func:`convert` there — or incompatible units."""
    zero = convert(0.0, src, dst)
    one = convert(1.0, src, dst)
    if zero is None or one is None:
        return None
    if abs(zero) > 1e-12:                    # non-zero image of 0 ⇒ affine (offset)
        return None
    return one - zero
