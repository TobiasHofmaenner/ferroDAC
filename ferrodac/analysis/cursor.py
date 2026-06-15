"""CursorProcessor — a trend cursor as a Processor (trace → one scalar).

Extracts a scalar (peak / value-at / area) from a trace source at a given m/z,
turning e.g. "the H2O peak (m/z 18)" into a routable, recordable scalar. This is
the reference Processor and the template for the gas-composition analyzer.
"""

from __future__ import annotations

from ..core.trace import extract
from .processor import Port, Processor, register


@register
class CursorProcessor(Processor):
    kind = "cursor"
    label = "Trend cursor"
    accepts = "trace"
    id_prefix = "cur"            # ids stay "cur1"/"cur2"; output key "cur/cur1"

    def __init__(self, pid: str, input_key: str, mz: float = 0.0,
                 name: str = None, mode: str = "peak", width: float = 1.0,
                 unit: str = ""):
        super().__init__(pid, input_key)
        self.mz = float(mz)
        self.name = name or f"m/z {self.mz:g}"
        self.mode = mode
        self.width = float(width)
        self.unit = unit
        self.last_value = float("nan")

    # back-compat alias: callers/serialisation refer to a cursor's trace as
    # `source_key`; for processors it is the generic `input_key`.
    @property
    def source_key(self) -> str:
        return self.input_key

    def outputs(self) -> list[Port]:
        return [Port(f"cur/{self.id}", self.name, "float", self.unit)]

    def process(self, value) -> dict:
        self.last_value = extract(value, self.mz, self.width, self.mode)
        if not self.unit and getattr(value, "y_unit", ""):
            self.unit = value.y_unit        # adopt the trace's intensity unit
        return {f"cur/{self.id}": self.last_value}

    def state(self) -> dict:
        return {"mz": self.mz, "name": self.name,
                "mode": self.mode, "width": self.width}
