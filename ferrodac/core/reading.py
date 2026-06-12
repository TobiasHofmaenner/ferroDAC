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

    @property
    def key(self) -> str:
        """Global source key: 'device_instance/source'."""
        return f"{self.device}/{self.source}"
