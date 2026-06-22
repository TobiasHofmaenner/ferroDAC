"""A reference ferroDAC processor: integrate a spectrum over an m/z window.

Intentionally tiny and dependency-light (numpy only) — this is the template authors
copy. It turns a ``trace`` (a 1-D spectrum) into one scalar ``float`` source: the
trapezoidal area under the curve between ``[lo, hi]``. Route that scalar onto a Bars
panel (or a Chart) to track, e.g., a partial-pressure proxy for one species.

The whole contract a processor implements is shown below; see ``papers/integrate.md``
for the (short) white paper.

NOTE: ``ferrodac.plugin`` is ferroDAC's stable, versioned plugin SDK (Phase 1 of the
plugin platform). Until it exists in your ferroDAC, this file is the *spec* of the API
authors will code against.
"""
from __future__ import annotations

import numpy as np

from ferrodac.plugin import FLOAT, Port, Processor, register_processor

# numpy 2.x renamed trapz → trapezoid; support both.
_trapz = getattr(np, "trapezoid", None) or np.trapz


@register_processor
class WindowIntegral(Processor):
    kind = "window_integral"          # registry key (must be unique across loaded plugins)
    label = "Window integral"         # human label (shown in the UI / /proc)
    accepts = "trace"                 # input source datatype it consumes
    id_prefix = "wint"                # output key prefix → "wint/<id>/<name>"

    def __init__(self, pid: str, input_key: str, lo: float = 1.0, hi: float = 50.0,
                 name: str = "area", unit: str = ""):
        super().__init__(pid, input_key)
        self.lo, self.hi = float(lo), float(hi)
        self.name, self.unit = name, unit

    def outputs(self) -> list[Port]:
        """The source(s) this processor publishes — here one scalar."""
        return [Port(f"{self.id_prefix}/{self.id}/{self.name}", self.name, FLOAT, self.unit)]

    def process(self, value) -> dict:
        """Transform one input value → {output_key: value}. ``value`` is a Trace."""
        x, y = np.asarray(value.x), np.asarray(value.y)
        m = (x >= self.lo) & (x <= self.hi)
        area = float(_trapz(y[m], x[m])) if int(m.sum()) > 1 else 0.0
        return {f"{self.id_prefix}/{self.id}/{self.name}": area}

    def state(self) -> dict:
        """Serializable config (round-trips through a saved layout)."""
        return {"lo": self.lo, "hi": self.hi, "name": self.name, "unit": self.unit}
