# QMS 200 (Pfeiffer Prisma) RGA driver — protocol notes & refactor plan

Driver-specific design note for `ferrodac/devices/qms200.py`. Captures the
**protocol facts** (interop analysis — facts aren't copyrightable; reimplemented
in our own code, never copied) and the planned **stability refactor**. Written
2026-06-16 before the rewrite.

## The instrument

Pfeiffer **Prisma 80 / QMS 200** quadrupole RGA (legacy Balzers QMG family),
RS-232 at 19 200 baud, CR terminator. Authoritative wire spec is *BG 805 203 BE
Communication Protocol* — **not freely available**; these facts were reconstructed
from the firmware data model in the **HiQuad QMG 700 Communication Protocol**
(BG 5401 BE, OPC variant) and confirmed against the **PyExpLabSys `pfeiffer_qmg422`**
driver, which speaks the *same* ASCII protocol. The operating manual
(BG 805 204 BE) has no wire protocol. See *Sources* below.

## Transport — ASCII ACK/ENQ

    HOST → "MNEMONIC[,param]\r"   →   device → <ACK 0x06>
    HOST → <ENQ 0x05>            →   device → "data\r\n"

- A mnemonic **with** a parameter is a **write**; the **same mnemonic with no
  parameter is a read** (returns the current value via the ENQ step). This is the
  key to the read-back principle below — and the driver already relies on it for
  `FIE`/`SEM`/`SDT`/`SHV`.
- `CMO ,1` selects ASCII computer control. (A binary transfer mode very likely
  exists — the firmware data is binary 8-byte tuples — but its framing is in the
  unavailable BG 805 203; ASCII is the supported, documented-enough path. If we
  ever want it, sniff Quadstar/TalkStar on the wire.)

## Mass-scan protocol

**Configure** (idempotent, sent on connect and on any range/speed/resolution
change): `CMO ,1` · `CYM ,0` (single channel) · `SMC ,0` · `MMO ,0` (mass-scan) ·
`MRE ,1` (resolve peak) · `MST ,<res>` · `MSD ,<speed>` · `MFM ,<first>` ·
`MWI ,<width>` · `AMO ,1` + `ARL ,-11` (auto-range, lower limit).

**Run one sweep:** `CRU ,2` to start, then read the measurement buffer.

**Read the buffer:** poll `MBH` (measurement buffer header), then pull that many
points with `MDB` (one value per `MDB` — there is **no bulk read** in ASCII; the
reference loops `MDB` per point too).

### `MBH` returns **5 comma-separated fields** — the load-bearing fact

| field | meaning |
|---|---|
| **[0]** | **running state — `0` = measurement STOPPED (sweep complete), non-zero = running** |
| [1], [2] | (unconfirmed) |
| **[3]** | **number of samples waiting in the buffer** (the only field the current driver reads) |
| [4] | a count echo (NOT a mass) |

### `MDB` carries **intensity only**

One value per call, e.g. `+1.00300E-11` (Amps). **The mass is not on the wire** —
it is defined entirely by `MFM` (first mass) + `MWI` (width) + `MST` (resolution →
points per amu). So the m/z axis is **deterministic from the scan parameters**,
not something to infer from the data.

## Root causes of the current instability

1. **We ignore `MBH` field [0]** (the running flag) and instead *guess* sweep
   completion with a timing heuristic (`scan_time elapsed AND idle ≥ 8`). When the
   guess is wrong the sweep is cut short → the "terminates too early / truncated"
   failures. **The instrument signals completion and we don't read it.**
2. **Rate-learning is fed by that broken completion.** Because we *learn*
   points-per-amu from the (sometimes truncated) sweep length instead of computing
   it, a bad completion corrupts the mass axis (peaks drift).
3. **Per-point framing fragility + destructive resync** (`reset_input_buffer`
   chops in-flight bytes), and **control writes interleaved into the point drain**.
4. **Coarse recovery:** any `ProtocolError` nukes the link and stalls 3 s, so
   transient one-byte slips throw away whole sweeps.

## Refactor plan

Four layers, each independently testable; the processing pipeline (smooth →
grid-resample → rolling-average → peak-reduce → normalise) is already good and
stays — it just stops being braided through the I/O.

### 1. Deterministic completion (delete the timer)

Drive the sweep off `MBH` field [0]:

```
CRU ,2                                  # start
seen_running = False
loop:
    running, count = MBH[0], MBH[3]
    seen_running |= (running != 0)      # start-up guard: don't finish before it begins
    read `count` points via MDB, append
    if seen_running and running == 0 and count == 0:
        break                           # sweep complete — deterministic, no timing
```

No idle counter, no `scan_time` guess. A generous wall-clock deadline stays only
as a safety backstop, not as the normal end condition.

### 2. Read-back the actual scan parameters — never trust our settings

After configuring, **query the parameters back from the instrument** and build the
axis from the device's truth, not from what we sent:

```
actual_first = int(query("MFM"))
actual_width = int(query("MWI"))
actual_res   = int(query("MST"))        # → points-per-amu via the confirmed map
mass(i) = actual_first + offset + i / ppa(actual_res)
```

If a read-back disagrees with what we sent (a command was clamped or didn't take),
the **device value wins** and we log it. This kills an entire class of "axis
doesn't match reality" bugs, and it **removes the rate-learning subsystem**
(`_steps_per_amu`/`_avg`-relearn) entirely — the axis is authoritative from
config, the point count just says how many points to place on it.

### 3. Hardened transport

`_Link` becomes the only thing that touches the port: a proper ACK/ENQ codec with
**per-operation timeouts**, **typed errors** (timeout vs NAK vs framing), and a
**bounded, non-destructive** resync (don't flush mid-value). Retry an individual
`MDB`/`MBH` op a few times before declaring a real desync.

### 4. Clean integration

Control writes (`FIE`/`SEM`/`SDT`/`SHV`) applied **only at sweep boundaries**, not
interleaved into the point drain. Transient glitches recover by **retrying the
operation**, not nuking the link; only a genuine link loss triggers a reopen.
Keep the dedicated acquisition path (continuous sweep, partial frames) — it suits
a continuously-sweeping instrument.

## Open items — confirm on the rig (one `FERRODAC_QMS_DEBUG=1` capture of one full scan)

- **`MST` → points-per-amu mapping.** Our table says `0=1, 1=8, 2=64`/amu; the
  QMG 700 binary doc implied a different encoding. The actual point count for a
  known range + `MST` settles it (and cross-checks the axis).
- **`MBH` field [0] behaviour** — verify it goes `1 → 0` at the sweep boundary.
- **Read-back formats** of `MFM`/`MWI`/`MST` (int vs float, extra fields).

## Sources (interop references; facts only)

- Pfeiffer **HiQuad QMG 700 Communication Protocol** (BG 5401 BE) — firmware data
  model (ring buffer, packet header `[channel, data-type, status, count]`, 8-byte
  `intensity+mass+status` tuples, status codes channel/cycle/measurement-end).
- **PyExpLabSys `pfeiffer_qmg422`** (CINF, GPL) — confirms the same ASCII protocol:
  `MBH` 5 fields with field [0] = running, field [3] = sample count; `MDB` =
  intensity per call; mass from `MFM`/`MWI`. *Read for protocol facts only.*
- Prisma QMS 200 operating manual (BG 805 204 BE) — instrument data, no protocol.
- *BG 805 203 BE Communication Protocol* — the authoritative ASCII spec, not
  obtainable; would also confirm whether a binary RS-232 mode exists.

## Status: implemented — 2026-06-16 (v0.41.0)

Rewritten and rig-confirmed via a full `FERRODAC_QMS_DEBUG=1` capture:

- **Completion signal confirmed: `MBH` field [0]** (`0`=measuring, `1`=complete).
  The old timing heuristic truncated wildly — the *same* config gave 66 / 1568 /
  8 / 336 points across runs. Now: read points per `MBH`, conclude only when
  `field[0]==1` and the buffer has drained. (`field [2]` is a constant `7`, not a
  status; my earlier guess was wrong.)
- **Resolution confirmed:** `MST=1` → 32 points/amu (1568 pts over 1–50), so
  `PPA_FROM_MST = {0:64, 1:32, 2:16, 3:8}` (RES_OPTS relabelled; default now 2).
- **Read-back vindicated:** sent `MST ,0`, device reports `1` (clamped). Axis is
  `linspace(MFM_readback, MFM+MWI_readback, N)` — rate-learning deleted.
- **Read-back formats:** `MFM`→`'1.00'` (float), `MWI`→`'+49'` (int).
- **`SEM` bug fixed:** reads `'1,1248'` (on,HV) — parse field [0].
- Verified headless with a mock link replaying the captured `MBH`/`MDB` pattern
  (incl. the empty-buffer-mid-sweep header that used to truncate).

Known: a full high-res sweep is inherently ~60 s over the per-point ASCII link;
lower resolution = faster. Stray `3,4,…` frames during 1000-pt bursts are skipped.
