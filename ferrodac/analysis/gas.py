"""GasAnalyzer — gas composition as a Processor (trace -> one source per gas).

Deconvolves a mass spectrum against a cracking-pattern library and publishes a
partial-pressure source per candidate gas (`gas/<id>/<name>`), so each gas's
pressure charts, routes and records like any scalar. Pair with the RGA's
total-pressure normalisation for partial pressures in real units (mbar).
"""

from __future__ import annotations

import numpy as np

from ..core.trace import Trace
from .deconvolve import deconvolve, deconvolve_mc
from .library import DEFAULT_GASES, LIBRARY, get_gases
from .processor import Port, Processor, register


@register
class GasAnalyzer(Processor):
    kind = "gas"
    label = "Gas composition"
    accepts = "trace"
    id_prefix = "gas"

    def __init__(self, pid: str, input_key: str, gases=None,
                 sparsity: float = 0.0, mc: int = 0, unit: str = ""):
        super().__init__(pid, input_key)
        self.gas_names = list(gases) if gases else list(DEFAULT_GASES)
        self._gases = get_gases(self.gas_names)
        self.sparsity = float(sparsity)
        self.mc = int(mc)                       # 0/1 = single fit; >1 = MC runs
        self.unit = unit
        # latest results, for the composition panel
        self.last_amounts: dict = {}
        self.last_sd: dict = {}                 # 1-sigma uncertainty (MC only)
        self.last_residual = float("nan")
        self.last_degenerate: list = []         # unresolvable (a, b, corr) pairs

    def update(self, **fields) -> None:
        super().update(**fields)
        if "gas_names" in fields or "gases" in fields:
            self.gas_names = list(fields.get("gas_names", fields.get("gases")))
            self._gases = get_gases(self.gas_names)

    def outputs(self) -> list[Port]:
        """Per gas: a scalar partial-pressure source and a reconstructed-spectrum
        trace source (route the latter back onto the spectrum to see the fit)."""
        ports = []
        for n in self.gas_names:
            ports.append(Port(f"gas/{self.id}/{n}", n, "float", self.unit))
            ports.append(Port(f"fit/{self.id}/{n}", f"{n} fit", "trace", self.unit))
        return ports

    def _reconstruct(self, x, name, amount) -> Trace:
        """The spectrum this gas alone would produce at its fitted amount, on the
        measured mass axis — its fragments placed at their m/z (so it overlays)."""
        g = LIBRARY.get(name)
        y = np.zeros(len(x))
        if g is not None and amount > 0:
            contrib = amount * (g.rsf or 1.0)        # un-sensitivity-corrected
            for m, frac in g.norm_pattern.items():
                y[np.abs(x - m) <= 0.5] = contrib * frac
        return Trace(np.asarray(x, float), y, x_label="m/z", y_label="Intensity",
                     y_unit=self.unit, x_lo=float(x[0]), x_hi=float(x[-1]))

    def process(self, value) -> dict:
        sigma = getattr(value, "sigma", None)   # measured per-mass noise, if any
        if self.mc > 1:
            med, sd, resid, pairs = deconvolve_mc(
                value.x, value.y, self._gases, runs=self.mc,
                sparsity=self.sparsity, sigma=sigma)
            self.last_amounts, self.last_sd = med, sd
            self.last_residual, self.last_degenerate = resid, pairs
        else:
            amounts, resid = deconvolve(value.x, value.y, self._gases,
                                        sparsity=self.sparsity, sigma=sigma)
            self.last_amounts, self.last_sd = amounts, {}
            self.last_residual, self.last_degenerate = resid, []
        if not self.unit and getattr(value, "y_unit", ""):
            self.unit = value.y_unit
        out = {}
        for n in self.gas_names:
            amt = self.last_amounts.get(n, 0.0)
            out[f"gas/{self.id}/{n}"] = amt
            out[f"fit/{self.id}/{n}"] = self._reconstruct(value.x, n, amt)
        return out

    def state(self) -> dict:
        return {"gases": self.gas_names, "sparsity": self.sparsity, "mc": self.mc}
