"""Shared serial open + identify handshake (DESIGN §21.1 — device robustness, issue #9).

A serial driver's FIRST bytes on the wire after opening the port are its identify query
(``*IDN?``), sent single-shot with no warm-up and no retry — so the first connect is
fragile and a single corrupted ``*IDN?`` fails the whole activation (``remove + re-add``
then works). Two things corrupt that first command, and a plain ``reset_input_buffer()``
handles neither because it clears only the HOST rx queue, not the instrument's line parser:

  1. a first-write-after-open FTDI/CDC transient mangles the first bytes → ``unknown command``;
  2. a PARTIAL LINE left in the DEVICE's parser by a prior/aborted session gets glued onto
     the fresh ``*IDN?`` → the instrument sees ``<junk>*IDN?`` → ``unknown command``.

``open_and_identify`` folds a warm-up (a lone terminator that flushes the device parser) plus
a bounded retry into ONE helper both serial drivers use from ``probe_port`` AND ``_connect``,
so discovery and activation are equally hardened. Qt-free.
"""

from __future__ import annotations

import sys
import time

TERM = b"\n"
IDENTIFY_ATTEMPTS = 3        # up to N identify tries; the first may be sacrificial


def posix_exclusive_kwargs() -> dict:
    """``serial.Serial`` kwargs that make a POSIX open EXCLUSIVE (TIOCEXCL), so a stray
    reader / screen / cat on the port can't silently corrupt a live link (issue #6). Empty
    off POSIX — a Windows serial port is exclusive by default. One definition both serial
    drivers' ``open()`` share."""
    if sys.platform.startswith(("linux", "darwin")):
        return {"exclusive": True}
    return {}


def _flush_parser(conn, settle: float) -> None:
    """Write a lone line terminator so the INSTRUMENT's parser terminates + discards any
    partial line left from a prior session (``reset_input_buffer`` can't — it's host-side
    only), let the device consume it, then drop the host rx buffer (its echo / ERR to that
    blank line). Best-effort — the retry loop is the real safety net. Only a terminator is
    ever written, never a side-effecting command."""
    try:
        conn._ser.write(TERM)
        conn._ser.flush()
        time.sleep(settle)                 # let the device consume the terminator
        conn._ser.reset_input_buffer()
    except Exception:
        pass


def open_and_identify(conn, validate, *, attempts: int = IDENTIFY_ATTEMPTS,
                      settle: float = 0.03):
    """Open a pyserial-backed controller and run the warm-up + RETRIED identify handshake
    (issue #9). ``conn`` is a duck-typed LSA31 / LSC controller exposing ``.open()`` (opens
    EXCLUSIVE on POSIX, settles, host-side ``reset_input_buffer``; returns conn or raises),
    ``._ser`` (the open ``serial.Serial``, for the parser flush), ``.idn()`` (issues the
    identify query → the raw reply, may raise on an instrument ERR), and ``.close()``.
    ``validate`` is the driver's module-level ``_parse_idn`` (returns the parsed identity or
    ``None``), so the helper needs no driver-specific identity knowledge.

    Returns the parsed identity from the FIRST accepted reply, or ``None`` if every attempt
    failed. ``open()`` failures PROPAGATE (a busy / absent port is never masked as a
    wrong-device ``None``). The caller owns cleanup (``close``) on both success and ``None`` —
    exactly as the hand-rolled ``open() + first *IDN?`` path it replaces."""
    conn.open()                            # exclusive open + settle + host reset (may raise)
    _flush_parser(conn, settle)            # clear a device-side partial-line leftover
    for _ in range(max(1, int(attempts))):
        try:
            conn._ser.reset_input_buffer() # a FRESH host buffer before THIS attempt — drops a
        except Exception:                  # late flush-ERR or a prior attempt's stale reply
            pass
        try:
            parsed = validate(conn.idn())
        except Exception:                  # instrument ERR / I/O blip → this try was sacrificial
            parsed = None
        if parsed is not None:
            return parsed
    return None
