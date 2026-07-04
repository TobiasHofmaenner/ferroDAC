"""core/uncertainty.py — declarative σ MODELS (DESIGN §19.0, first-class uncertainty).

A source declares an uncertainty *model* — one small serialisable value object — and the
framework reconstructs σ from it over a bounded window on demand (§19.0's "derived lens":
never a stored 2× channel unless an instrument genuinely *measures* σ). A model never
computes on the hot path; ``sigma(values)`` is a vectorised pure function evaluated only
where a view asks for it.

**Semantics: every model returns a STANDARD uncertainty (1σ).** Datasheet accuracy figures
are usually *bounds* (worst-case, or k=2 / rectangular) — convert to 1σ (divide by the
coverage factor) when you declare the model. Independent contributions combine in
quadrature (RSS), the GUM rule.

The five types:
  * ``Abs(sigma_abs)``     — constant absolute σ (an ADC's ½-LSB floor).
  * ``Rel(rel)``           — σ = rel·|x| (a gauge's "±0.15 % of reading" → rel=0.0015).
  * ``FloorRel(floor,rel)``— σ = hypot(floor, rel·|x|) (the common "abs + rel" spec).
  * ``Measured(channel)``  — σ is measured per-sample and STORED as a companion channel;
                             it is not a function of the value, so ``sigma()`` is undefined.
  * ``Spec(random, systematic)`` — a source's full uncertainty split into an independent
                             RANDOM part (enters propagation per-point, averages down) and a
                             SYSTEMATIC part (correlated across all points, propagated
                             separately). The split must travel from the source (§19.2).

Qt-free, numpy-only — runs in the data-plane job and server-side.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional

import numpy as np


def _arr(values):
    return np.asarray(values, dtype=float)


class Uncertainty:
    """Base σ model. Subclasses implement ``sigma(values) -> ndarray`` (standard
    uncertainty, vectorised) and ``to_dict``/``_from_dict`` for the provenance
    change-log + export manifest."""

    TYPE: ClassVar[str] = ""

    def sigma(self, values):
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Uncertainty"]:
        """Reconstruct a model (or None) from its serialised form; dispatch on ``type``."""
        if not d:
            return None
        cls = _TYPES.get(d.get("type"))
        if cls is None:
            raise ValueError(f"unknown uncertainty model: {d.get('type')!r}")
        return cls._from_dict(d)


@dataclass(frozen=True)
class Abs(Uncertainty):
    """Constant absolute standard uncertainty, independent of the value."""
    sigma_abs: float
    TYPE: ClassVar[str] = "abs"

    def sigma(self, values):
        return np.full(np.shape(values), abs(float(self.sigma_abs)), dtype=float)

    def to_dict(self):
        return {"type": self.TYPE, "sigma_abs": float(self.sigma_abs)}

    @classmethod
    def _from_dict(cls, d):
        return cls(float(d["sigma_abs"]))


@dataclass(frozen=True)
class Rel(Uncertainty):
    """Relative standard uncertainty: σ = rel·|x| (rel as a fraction: 0.15 % → 0.0015)."""
    rel: float
    TYPE: ClassVar[str] = "rel"

    def sigma(self, values):
        return abs(float(self.rel)) * np.abs(_arr(values))

    def to_dict(self):
        return {"type": self.TYPE, "rel": float(self.rel)}

    @classmethod
    def _from_dict(cls, d):
        return cls(float(d["rel"]))


@dataclass(frozen=True)
class FloorRel(Uncertainty):
    """The common "absolute floor + relative" spec, combined in quadrature:
    σ = hypot(floor, rel·|x|). At x→0 it is ``floor``; at large |x| it is ``rel·|x|``."""
    floor: float
    rel: float
    TYPE: ClassVar[str] = "floor_rel"

    def sigma(self, values):
        return np.hypot(abs(float(self.floor)), abs(float(self.rel)) * np.abs(_arr(values)))

    def to_dict(self):
        return {"type": self.TYPE, "floor": float(self.floor), "rel": float(self.rel)}

    @classmethod
    def _from_dict(cls, d):
        return cls(float(d["floor"]), float(d["rel"]))


@dataclass(frozen=True)
class Measured(Uncertainty):
    """σ is measured per-sample and stored as its own channel — not a function of the
    value. The engine reads that companion σ array; ``sigma()`` is undefined here."""
    channel: str = ""
    TYPE: ClassVar[str] = "measured"

    def sigma(self, values):
        raise TypeError("Measured σ comes from a stored channel, not a value model")

    def to_dict(self):
        return {"type": self.TYPE, "channel": self.channel}

    @classmethod
    def _from_dict(cls, d):
        return cls(d.get("channel", ""))


@dataclass(frozen=True)
class Spec(Uncertainty):
    """A source's full uncertainty = an independent RANDOM part ⊕ a SYSTEMATIC part.
    ``sigma()`` returns the combined 1σ (both in quadrature) for a single-point view; the
    engine reads ``.random`` and ``.systematic`` separately to propagate the systematic
    (calibration) as a correlated term rather than a per-point weight."""
    random: Optional[Uncertainty] = None
    systematic: Optional[Uncertainty] = None
    TYPE: ClassVar[str] = "spec"

    def sigma(self, values):
        v = _arr(values)
        acc = np.zeros(np.shape(v), dtype=float)
        for part in (self.random, self.systematic):
            if part is not None:
                s = np.asarray(part.sigma(v), dtype=float)
                acc = acc + s * s
        return np.sqrt(acc)

    def to_dict(self):
        return {"type": self.TYPE,
                "random": self.random.to_dict() if self.random else None,
                "systematic": self.systematic.to_dict() if self.systematic else None}

    @classmethod
    def _from_dict(cls, d):
        return cls(Uncertainty.from_dict(d.get("random")),
                   Uncertainty.from_dict(d.get("systematic")))


_TYPES = {c.TYPE: c for c in (Abs, Rel, FloorRel, Measured, Spec)}
