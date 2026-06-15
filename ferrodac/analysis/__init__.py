"""Analysis — spectrum/data transforms (processors) with no UI or hardware deps.

A `Processor` consumes one source's values and produces one or more derived
sources. Trend cursors (trace→scalar) live here; the gas-composition analyzer
(trace→one partial-pressure source per gas) will join as another registered
type. Kept Qt-free so it is unit-testable headless and could run server-side.
"""

from .processor import Port, Processor, PROCESSOR_TYPES, register  # noqa: F401
from . import cursor  # noqa: F401  registers CursorProcessor
