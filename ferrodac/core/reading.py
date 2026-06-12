"""Reading — one sample on a channel, pushed through the data plane.

This is the unit of the *stream* path (push), distinct from the *snapshot* path
(`describe()` → SourceDescriptor, pull). Sources emit Readings; the engine fans
them out to sinks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    source: str        # source instance_id
    channel: str       # channel id within the source
    t: float           # wall-clock timestamp (seconds)
    value: float       # scalar value (NaN if unavailable)
    status: int = 0    # 0 = ok, non-zero = error/underrange/etc.

    @property
    def key(self) -> str:
        """Global channel key: 'source_instance/channel'."""
        return f"{self.source}/{self.channel}"
