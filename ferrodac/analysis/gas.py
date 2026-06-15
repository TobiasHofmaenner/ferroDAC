"""GasAnalyzer — gas composition as a Processor (trace -> one source per gas).

Deconvolves a mass spectrum against a cracking-pattern library and publishes a
partial-pressure source per candidate gas (`gas/<id>/<name>`), so each gas's
pressure charts, routes and records like any scalar. Pair with the RGA's
total-pressure normalisation for partial pressures in real units (mbar).
"""

from __future__ import annotations

from .deconvolve import deconvolve, deconvolve_mc
from .library import DEFAULT_GASES, get_gases
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
        return [Port(f"gas/{self.id}/{n}", n, "float", self.unit)
                for n in self.gas_names]

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
        return {f"gas/{self.id}/{n}": self.last_amounts.get(n, 0.0)
                for n in self.gas_names}

    def state(self) -> dict:
        return {"gases": self.gas_names, "sparsity": self.sparsity, "mc": self.mc}
