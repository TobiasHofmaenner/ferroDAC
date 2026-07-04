"""Process-global serial-port arbitration (DESIGN §21.1 — device threading).

Every serial driver used to keep its OWN ``_active_ports`` set + class lock, so a
port held open by a TPG was invisible to the Keithley's ``discover()`` — the audit's
"cross-driver serial-port arbitration is inexpressible" gap: two drivers could both
try to open ``/dev/ttyUSB0``. This is the ONE shared registry they now point at, so a
claimed port is off-limits to every driver, not just its own class.

Qt-free (plain threading). A serial driver uses it by class-referencing the shared
lock + set, e.g.::

    from ..core.serial_arbiter import SERIAL_LOCK, PORTS_IN_USE
    class MyDevice(BaseDevice):
        _cls_lock = SERIAL_LOCK
        _active_ports = PORTS_IN_USE
"""

from __future__ import annotations

import threading

# One re-entrant lock + one set shared by ALL serial drivers. RLock so a driver
# that already holds it (discovery) can call a helper that re-acquires it.
SERIAL_LOCK = threading.RLock()
PORTS_IN_USE: set = set()


def held_ports() -> set:
    """A snapshot of every physical port currently held open (any driver)."""
    with SERIAL_LOCK:
        return set(PORTS_IN_USE)
