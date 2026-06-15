"""Trace — the value of an array-valued Reading.

A generic 1-D array over a swept axis: a mass spectrum (m/z vs intensity), an
RF/audio spectrum (frequency vs power), an optical spectrum (wavelength vs
intensity), a scope capture (time vs voltage)… The value carries its own axis
labels/units so a panel can self-label regardless of the source.

Datatype string is ``"trace"``; it routes to a trace panel (spectrum line or
waterfall), not a time-series chart. ``eq=False`` avoids a synthesised
``__eq__`` that would do an ambiguous element-wise NumPy comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(eq=False)
class Trace:
    x: np.ndarray                 # the swept axis (m/z, frequency, wavelength…)
    y: np.ndarray                 # values (intensity, power, amplitude…)
    x_label: str = "x"
    x_unit: str = ""
    y_label: str = "Intensity"
    y_unit: str = ""
    x_lo: float = None            # declared full-axis range (so a partial fill or
    x_hi: float = None            # stale view doesn't dictate the plotted extent)
    sigma: np.ndarray = None      # optional per-point measured noise (1 sigma),
    #                               e.g. the sweep-to-sweep std of an average

    def __len__(self) -> int:
        return len(self.x)

    @property
    def peak(self) -> float:
        return float(self.y.max()) if len(self.y) else float("nan")


def extract(trace: "Trace", center: float, width: float = 1.0,
            mode: str = "peak") -> float:
    """A scalar from a trace at ``center`` (± width/2): peak / value-at / area.

    Powers trend cursors — turn "the H2O peak (m/z 18)" of an RGA spectrum into a
    routable scalar that charts and Record over time.
    """
    x, y = trace.x, trace.y
    if len(x) == 0:
        return float("nan")
    if mode == "value":
        return float(y[int(np.abs(x - center).argmin())])
    lo, hi = center - width / 2.0, center + width / 2.0
    sel = (x >= lo) & (x <= hi)
    if not sel.any():
        return float(y[int(np.abs(x - center).argmin())])
    if mode == "area":
        return float(np.trapz(y[sel], x[sel]))
    return float(y[sel].max())          # peak (default)
