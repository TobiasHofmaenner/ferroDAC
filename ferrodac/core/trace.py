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

    def __len__(self) -> int:
        return len(self.x)

    @property
    def peak(self) -> float:
        return float(self.y.max()) if len(self.y) else float("nan")
