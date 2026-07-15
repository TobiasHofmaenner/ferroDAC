# Writing a device driver

A ferroDAC driver turns an instrument into `Source`s (data out) and `Sink`s (control in).
This page is the contract a driver must honour — the conventions that, until now, lived
only in `ferrodac/devices/lsa31.py` by example. Read `lsa31.py` (a mature serial driver)
and `lsc.py` (a self-describing one) alongside this.

## The two-layer pattern

Split every driver in two, so the instrument logic is reusable outside ferroDAC (cal
stations, flashing scripts) and unit-testable against a fake link:

1. **A dependency-free controller** (`LSA31`, `LSC`) — plain synchronous methods over the
   transport (pyserial, VISA, a socket). No ferroDAC imports, no Qt. This is where the
   wire protocol lives.
2. **A `BaseDevice` wrapper** (`LSA31Device`, `LSCDevice`) — the thin ferroDAC adapter that
   maps the controller onto `Source`/`Sink` and implements the hooks below.

Register by subclassing `BaseDevice` with a `driver = "<name>"` class attribute; discoverable
drivers are auto-found. Set `discoverable = False` for minted-only devices (e.g. Python devices).

## The hooks you implement

| hook | contract |
|------|----------|
| `discover(cls) -> list` | class method; return live device instances. For serial, use the shared arbiter (below). |
| `_connect(self)` | open the link, verify identity, seed sink state from the **real** instrument. Raise on failure. |
| `_disconnect(self)` | close the link. **Leave physical outputs as the operator set them** — an implicit change on disconnect is an unsafe surprise. |
| `_read(self, source) -> (float, int)` | one channel's value. See the read contract. |
| `_write(self, sink, value)` | apply a control write. Raise on refusal — a refused command must never *look* accepted. |

### The `_read` contract

`_read(source)` returns **`(float value, int status)`**:

- `value` is **always a float**, whatever the source's declared dtype — a bool is `0.0/1.0`,
  an enum is its **option index**. The data plane (`Reading.value`) carries a float.
- `status` is `0` for a good reading, **non-zero for "no reading this cycle"** (with
  `value = NaN`). Return `(math.nan, 1)` for an invalid/unavailable/out-of-range sample —
  never a bogus number.
- `_read` runs on the device's poll thread under the platform's per-device lock (`_guard()`).
  Keep one acquisition instant shared across a cycle's sources (query the instrument once,
  cache for at most half a poll period — see `LSCDevice._fresh_meas`).

### `Source.dtype` vocabulary

The legal tokens (the router in `net/convert.py` is the authority) are: `float`, `bool`,
`trace`, `string`, `enum`, `image`, `video`, `waveform`. Because `_read` always emits a
float, scalar and categorical channels declare **`float`** (or `bool`) — declaring `"int"`
or `"str"` (which are *not* tokens) makes the router fall back and **drops the channel from
curation, CSV export, charts, and control readback**. Today `float`/`bool`/`trace`/`image`
render fully; `string`/`enum`/`waveform` are recognised by the wire but not yet first-class
in charts. (The processor pipeline's `plugin.DTYPES` — `float`/`bool`/`trace` — is a separate,
narrower vocabulary for source→processor→widget ports.)

## Emitting tags (alarms / events) — `emit_tag`

A device raises an event (a leak, a gas-detected crossing, a FIB timeout) by building a
device-origin `Marker` and calling `self.emit_tag(marker)` (DESIGN §7.3). The platform
injects the sink when the device goes active (`DeviceManager` → `device_tag` → the TagStore);
until then it is a safe no-op. **Do not invent your own tag callback** — `set_tag_sink` is a
platform hook, not a driver one. Build the `Marker` with `origin_kind=ORIGIN_DEVICE`,
`origin_id=self.data_id`, a `severity`, and `immutable=True` (an emitted fact); see
`LSCDevice._event_to_tag`.

## Serial specifics

- **Discovery** uses a shared arbiter so two drivers never open the same port at once:
  `PORTS_IN_USE` / `SERIAL_LOCK` from `core.serial_arbiter`, plus a per-class `_cache`.
  Copy the `discover()` shape from `lsa31.py`/`lsc.py` (a `BaseSerialDevice` helper to own
  this is a planned cleanup — `AUDIT-2026-07`).
- **Probe closes the port** — `discover` must not hold it open.
- **Know your hardware's hazards.** Example: on a SAM3X/Arduino-Due, opening the port at
  **1200 baud triggers the bootloader** and resets the instrument — the `LSC`/`LSA31`
  constructors reject 1200 outright (defence in depth). Encode such rules in the controller.
- **Resync after a timeout.** A timed-out reply can leave the stream offset by one; flag it
  and flush the input buffer before the next transaction (`LSC._desynced`) so a command can
  never read the *previous* command's reply.

## Rate & lifecycle

Declare a `RateControl(mode=RateMode.SETTABLE|FIXED, native_hz, default_hz, min_hz, max_hz)`;
the platform polls at `_rate_hz`. `primary_source` names the channel shown by default. The
platform owns start/stop/poll and the io-lock — you never spawn the poll thread yourself.

## Checklist

- [ ] Two layers; the controller has no ferroDAC/Qt imports.
- [ ] `_read` returns `(float, status)`, `(nan, 1)` for no-reading; `dtype` is a legal token.
- [ ] `_write` raises on refusal.
- [ ] `_disconnect` leaves outputs untouched.
- [ ] Events go through `self.emit_tag(...)`.
- [ ] Serial: shared arbiter, probe closes the port, hardware hazards encoded.
- [ ] Tests against a fake link cover the decode, error, and interleave paths (see `tests/test_lsc.py`).
