"""Spectrum — the value of an array-valued (waveform) Reading.

A mass spectrum (RGA) is the first array source: an ``intensity`` array over an
``mass`` (m/z) axis, rather than a scalar. It rides in ``Reading.value`` and
routes (datatype ``"spectrum"``) to a spectrum panel, not a time-series chart.

``eq=False`` so the dataclass doesn't synthesise an ``__eq__`` that would do an
ambiguous element-wise NumPy comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(eq=False)
class Spectrum:
    mass: np.ndarray          # m/z axis
    intensity: np.ndarray     # intensity (partial pressure / ion current)
    unit: str = ""            # intensity unit

    def __len__(self) -> int:
        return len(self.mass)

    @property
    def peak(self) -> float:
        return float(self.intensity.max()) if len(self.intensity) else float("nan")
