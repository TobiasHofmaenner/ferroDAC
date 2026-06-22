"""Processor — a data-plane transform that the Dashboard hosts uniformly.

A processor is bound to one input source (`input_key`) of a given `accepts`
datatype, and produces one or more derived output sources (`outputs()`), each
with a stable key. On every input value the Dashboard calls `process(value)`,
which returns ``{output_key: value}`` to publish back into the data plane.

This generalises the previously bespoke trend-cursor / CV-detector transforms
into an extensible, registered collection: a new analyzer (FFT, peak-finder,
gas composition…) is just a `Processor` subclass added to `PROCESSOR_TYPES`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Port:
    """One source a processor produces."""
    key: str                 # global source key, e.g. "cur/cur1", "gas/H2O"
    name: str
    dtype: str = "float"
    unit: str = ""


class Processor:
    kind: str = "processor"      # registry key
    label: str = "Processor"     # human label
    accepts: str = "trace"       # input source datatype
    id_prefix: str = "proc"      # id/key prefix (keeps ids stable per kind)

    def __init__(self, pid: str, input_key: str = None):
        self.id = pid
        self.input_key = input_key       # None = unbound; set by routing a source to its input

    def bind_input(self, input_key) -> None:
        """Bind (or clear, with None) the source feeding this processor. Called when a
        source is routed to the processor's input port. Override to react to a re-bind."""
        self.input_key = input_key

    def outputs(self) -> list[Port]:
        """The source ports this processor publishes (may depend on config)."""
        raise NotImplementedError

    def process(self, value) -> dict:
        """Map one input value to ``{output_key: value}`` to publish."""
        raise NotImplementedError

    def update(self, **fields) -> None:
        """Apply config changes (from the UI)."""
        for k, v in fields.items():
            setattr(self, k, v)

    def state(self) -> dict:
        """Serialisable config for layout save/restore."""
        return {}


PROCESSOR_TYPES: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator: add a Processor subclass to the registry by its `kind`."""
    PROCESSOR_TYPES[cls.kind] = cls
    return cls
