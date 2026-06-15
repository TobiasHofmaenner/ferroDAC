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

# points/amu of the fine axis a reconstructed (Gaussian) spectrum is drawn on
_RECON_PPA = 32


@register
class GasAnalyzer(Processor):
    kind = "gas"
    label = "Gas composition"
    accepts = "trace"
    id_prefix = "gas"

    def __init__(self, pid: str, input_key: str, gases=None,
                 sparsity: float = 0.0, mc: int = 0, peak_fwhm: float = 0.7,
                 unit: str = ""):
        super().__init__(pid, input_key)
        self.gas_names = list(gases) if gases else list(DEFAULT_GASES)
        self._gases = get_gases(self.gas_names)
        self.sparsity = float(sparsity)
        self.mc = int(mc)                       # 0/1 = single fit; >1 = MC runs
        self.peak_fwhm = float(peak_fwhm)       # reconstructed peak width (amu)
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
        trace (route the latter onto the spectrum to see the fit). Plus a total
        Model trace and a Residual trace (measured - model: its leftover peaks
        are the species not being accounted for)."""
        ports = []
        for n in self.gas_names:
            ports.append(Port(f"gas/{self.id}/{n}", n, "float", self.unit))
            ports.append(Port(f"fit/{self.id}/{n}", f"{n} fit", "trace", self.unit))
        ports.append(Port(f"model/{self.id}", "Model fit", "trace", self.unit))
        ports.append(Port(f"residual/{self.id}", "Residual", "trace", self.unit))
        return ports

    @staticmethod
    def _recon_floor(y) -> float:
        """A baseline floor for the reconstruction, derived from the measured
        spectrum's noise (robust MAD) so Gaussian tails sit on a sensible
        baseline instead of plunging to ~1e-227 on a log overlay."""
        yv = np.asarray(y, float)
        yv = yv[np.isfinite(yv)]
        if yv.size == 0:
            return 0.0
        sigma = float(1.4826 * np.median(np.abs(yv - np.median(yv))))
        return max(sigma, float(np.max(yv)) * 1e-5)   # noise, or 5 decades down

    def _trace(self, x, y, lo, hi) -> Trace:
        return Trace(np.asarray(x, float), y, x_label="m/z", y_label="Intensity",
                     y_unit=self.unit, x_lo=lo, x_hi=hi)

    def _gaussian(self, fine, name, amount) -> np.ndarray:
        """The analog spectrum this gas alone would produce at its fitted amount:
        each fragment a Gaussian (peak_fwhm wide) at its m/z, so it looks like a
        real RGA scan and overlays the measured peaks."""
        y = np.zeros(len(fine))
        g = LIBRARY.get(name)
        if g is not None and amount > 0:
            contrib = amount * (g.rsf or 1.0)        # un-sensitivity-corrected
            sigma = max(self.peak_fwhm, 1e-3) / 2.3548
            for m, frac in g.norm_pattern.items():
                y += contrib * frac * np.exp(-0.5 * ((fine - m) / sigma) ** 2)
        return y

    def _stick_model(self, x) -> np.ndarray:
        """The fitted intensity at each measured mass — sum of every gas's
        fragment contributions there — for the residual (measured - model)."""
        x = np.asarray(x, float)
        model = np.zeros(len(x))
        for n in self.gas_names:
            amt = self.last_amounts.get(n, 0.0)
            g = LIBRARY.get(n)
            if g is None or amt <= 0:
                continue
            contrib = amt * (g.rsf or 1.0)
            for m, frac in g.norm_pattern.items():
                model[np.abs(x - m) <= 0.5] += contrib * frac
        return model

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
        floor = self._recon_floor(value.y)      # baseline from the measured noise
        lo, hi = float(value.x[0]), float(value.x[-1])
        fine = np.linspace(lo, hi, max(2, int(round((hi - lo) * _RECON_PPA)) + 1))
        clamp = (lambda y: np.maximum(y, floor)) if floor > 0 else (lambda y: y)
        out = {}
        model = np.zeros(len(fine))
        for n in self.gas_names:
            amt = self.last_amounts.get(n, 0.0)
            gy = self._gaussian(fine, n, amt)
            model += gy
            out[f"gas/{self.id}/{n}"] = amt
            out[f"fit/{self.id}/{n}"] = self._trace(fine, clamp(gy), lo, hi)
        out[f"model/{self.id}"] = self._trace(fine, clamp(model), lo, hi)
        # residual on the measured axis: leftover peaks = unaccounted species
        resid = np.asarray(value.y, float) - self._stick_model(value.x)
        out[f"residual/{self.id}"] = self._trace(value.x, resid, lo, hi)
        return out

    def state(self) -> dict:
        return {"gases": self.gas_names, "sparsity": self.sparsity, "mc": self.mc,
                "peak_fwhm": self.peak_fwhm}
