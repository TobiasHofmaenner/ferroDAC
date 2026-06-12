"""Fake, hardware-free sources for developing & testing the v1 plumbing.

They exercise the full ``discover -> describe -> connect -> status`` loop with no
real hardware, and (being multi-channel) make the nested source/channel cards
meaningful. Real device drivers (TPG-256A, Modbus) drop in alongside these.
"""

from __future__ import annotations

import math
import random
import time

from ..core.base import BaseSource
from ..core.source import (
    Channel,
    Control,
    ControlKind,
    Interface,
    Modality,
    Param,
    RateControl,
    RateMode,
)


class FakeGaugeController(BaseSource):
    """A simulated multi-gauge controller (à la the TPG-256A): several pressure
    channels, a settable poll rate, and a couple of declared (not-yet-wired)
    controls."""

    driver = "fake_gauge"
    discoverable = True

    _UNITS = ["A", "B"]  # two simulated controllers on two "ports"

    @classmethod
    def discover(cls):
        out = []
        for tag in cls._UNITS:
            channels = [
                Channel(id=f"ch{i}", name=name, unit="mbar",
                        modality=Modality.SCALAR, prefer_log=True)
                for i, name in enumerate(["Pirani", "FullRange", "Bayard-Alpert"], 1)
            ]
            controls = [
                Control(id="zero", name="Zero", kind=ControlKind.ACTION),
                Control(id="setpoint", name="Setpoint", kind=ControlKind.SETPOINT,
                        params=(Param("threshold", "float64", "mbar",
                                      minimum=1e-9, maximum=1000.0),),
                        value=1e-5),
                Control(id="filter", name="Filter", kind=ControlKind.ENUM,
                        params=(Param("mode", "str",
                                      options=("fast", "standard", "slow")),),
                        value="standard"),
                Control(id="emission", name="Emission", kind=ControlKind.TOGGLE,
                        value=False),
            ]
            out.append(cls(
                instance_id=f"sim:gauge:{tag}",
                name=f"Sim Gauge Ctrl {tag}",
                interface=Interface(kind="sim", params={"port": f"SIM{tag}"}),
                channels=channels,
                controls=controls,
                rate=RateControl(mode=RateMode.SETTABLE, native_hz=4.0,
                                 default_hz=1.0, min_hz=0.1, max_hz=4.0),
                primary_channel="ch1",
                hardware_id=f"SIM-GAUGE-{tag}",
                model="SimGauge 6",
            ))
        return out

    _BASES = {"ch1": 8e2, "ch2": 9e-1, "ch3": 5e-8}   # pressure scale per channel

    def _connect(self) -> None:
        time.sleep(0.4)            # simulate a handshake (shows CONNECTING)
        self._firmware = "SIMv1.0"
        self._t0 = time.time()

    def _read(self, channel):
        t = time.time() - getattr(self, "_t0", time.time())
        i = list(self._BASES).index(channel.id) if channel.id in self._BASES else 0
        base = self._BASES.get(channel.id, 1.0)
        val = base * (1 + 0.3 * math.sin(t / (3 + i))) * math.exp(-t / 180) + base * 1e-3
        val *= 1 + 0.02 * random.uniform(-1, 1)
        return val, 0


class FakeThermometer(BaseSource):
    """A simulated single-channel temperature module — its one channel is the
    automatic primary (no explicit pointer needed)."""

    driver = "fake_temp"
    discoverable = True

    _UNITS = ["1", "2"]

    @classmethod
    def discover(cls):
        out = []
        for tag in cls._UNITS:
            out.append(cls(
                instance_id=f"sim:temp:{tag}",
                name=f"Sim Thermometer {tag}",
                interface=Interface(kind="sim", params={"slave": tag}),
                channels=[Channel(id="temp", name="Temperature", unit="°C")],
                rate=RateControl(mode=RateMode.FIXED, native_hz=1.0),
                hardware_id=f"SIM-TEMP-{tag}",
                model="SimTherm",
            ))
        return out

    def _connect(self) -> None:
        time.sleep(0.3)
        self._firmware = "T1"
        self._t0 = time.time()

    def _read(self, channel):
        t = time.time() - getattr(self, "_t0", time.time())
        return 25.0 + 3.0 * math.sin(t / 10.0) + random.uniform(-0.1, 0.1), 0


class FakePowerSupply(BaseSource):
    """A simulated bench power supply driving a 100 Ω load.

    Controls (setters): output on/off, set-voltage, current-limit.
    Channels (getters):  measured voltage, current, power.
    Behaves like a real PSU: constant-voltage until the current limit is hit,
    then constant-current (clamped), with a little measurement noise.
    """

    driver = "fake_psu"
    discoverable = True
    _R = 100.0   # load resistance (ohms)

    @classmethod
    def discover(cls):
        channels = [
            Channel(id="voltage", name="Voltage", unit="V"),
            Channel(id="current", name="Current", unit="A"),
            Channel(id="power", name="Power", unit="W"),
        ]
        controls = [
            Control(id="output", name="Output", kind=ControlKind.TOGGLE, value=False),
            Control(id="voltage", name="Set Voltage", kind=ControlKind.SETPOINT,
                    params=(Param("v", "float64", "V", minimum=0.0, maximum=30.0),),
                    value=5.0),
            Control(id="current_limit", name="Current Limit",
                    kind=ControlKind.SETPOINT,
                    params=(Param("i", "float64", "A", minimum=0.0, maximum=5.0),),
                    value=1.0),
        ]
        return [cls(
            instance_id="sim:psu:1",
            name="Sim Power Supply",
            interface=Interface(kind="sim", params={"addr": "PSU1"}),
            channels=channels,
            controls=controls,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=10.0,
                             default_hz=5.0, min_hz=0.5, max_hz=10.0),
            primary_channel="voltage",
            hardware_id="SIM-PSU-0001",
            model="SimPSU 30-5",
        )]

    def _connect(self) -> None:
        time.sleep(0.3)
        self._firmware = "PSU1.2"

    def _read(self, channel):
        on = bool(self._control_values.get("output", False))
        vset = float(self._control_values.get("voltage", 0.0))
        ilim = float(self._control_values.get("current_limit", 1.0))
        if not on:
            v = i = 0.0
        else:
            i_ideal = vset / self._R
            if i_ideal > ilim:                # current-limited (CC)
                i, v = ilim, ilim * self._R
            else:                             # constant voltage (CV)
                v, i = vset, i_ideal
            v *= 1 + 0.005 * random.uniform(-1, 1)
            i *= 1 + 0.01 * random.uniform(-1, 1)
        value = {"voltage": v, "current": i, "power": v * i}[channel.id]
        return value, 0
