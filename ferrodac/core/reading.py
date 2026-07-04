"""Reading — one sample on a Source, pushed through the data plane."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    device: str        # device instance_id
    source: str        # source id within the device
    t: float           # wall-clock timestamp (seconds)
    value: float
    status: int = 0    # 0 = ok
    partial: bool = False  # preview frame (e.g. a partially-scanned spectrum):
    #                        live displays render it, but recorders / waterfall /
    #                        cursor extraction ignore it and wait for the complete.
    sigma: "float | tuple[float, float] | None" = None
    #                        optional inline 1σ (DESIGN §19.0), for uncertainty a
    #                        transient PROCESSOR output CREATES (e.g. a fit's bootstrap
    #                        σ) — free to carry since derived outputs aren't persisted.
    #                        A scalar is symmetric ±σ; a (σ_lo, σ_hi) pair is asymmetric
    #                        (a fit against a physical bound folds — §19.7). Device
    #                        channels leave this None (their σ is a model, §19.3).

    @property
    def key(self) -> str:
        """Global source key: 'device_instance/source'."""
        return f"{self.device}/{self.source}"
