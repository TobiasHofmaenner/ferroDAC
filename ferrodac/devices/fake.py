"""Fake, hardware-free devices for developing & testing.

They exercise discover → describe → connect → stream → write with no real
hardware: two multi-source instruments and a simulated bench power supply.
"""

from __future__ import annotations

import math
import random
import time

from ..core.base import BaseDevice
from ..core.device import (
    Interface,
    Modality,
    Param,
    RateControl,
    RateMode,
    Sink,
    SinkKind,
    Source,
)


class FakeGaugeController(BaseDevice):
    """A simulated multi-gauge controller (à la the TPG-256A)."""

    driver = "fake_gauge"
    discoverable = True

    _UNITS = ["A", "B"]
    _BASES = {"ch1": 8e2, "ch2": 9e-1, "ch3": 5e-8}

    @classmethod
    def discover(cls):
        out = []
        for tag in cls._UNITS:
            sources = [
                Source(id=f"ch{i}", name=name, unit="mbar",
                       modality=Modality.SCALAR, prefer_log=True)
                for i, name in enumerate(["Pirani", "FullRange", "Bayard-Alpert"], 1)
            ]
            sinks = [
                Sink(id="zero", name="Zero", kind=SinkKind.ACTION),
                Sink(id="setpoint", name="Setpoint", kind=SinkKind.SETPOINT,
                     params=(Param("threshold", "float", "mbar",
                                   minimum=1e-9, maximum=1000.0),),
                     value=1e-5),
                Sink(id="filter", name="Filter", kind=SinkKind.ENUM,
                     params=(Param("mode", "str",
                                   options=("fast", "standard", "slow")),),
                     value="standard"),
                Sink(id="emission", name="Emission", kind=SinkKind.TOGGLE, value=False),
            ]
            out.append(cls(
                instance_id=f"sim:gauge:{tag}",
                name=f"Sim Gauge Ctrl {tag}",
                interface=Interface(kind="sim", params={"port": f"SIM{tag}"}),
                sources=sources,
                sinks=sinks,
                rate=RateControl(mode=RateMode.SETTABLE, native_hz=4.0,
                                 default_hz=1.0, min_hz=0.1, max_hz=4.0),
                primary_source="ch1",
                hardware_id=f"SIM-GAUGE-{tag}",
                model="SimGauge 6",
            ))
        return out

    def _connect(self) -> None:
        time.sleep(0.4)
        self._firmware = "SIMv1.0"
        self._t0 = time.time()

    def _read(self, source):
        t = time.time() - getattr(self, "_t0", time.time())
        i = list(self._BASES).index(source.id) if source.id in self._BASES else 0
        base = self._BASES.get(source.id, 1.0)
        val = base * (1 + 0.3 * math.sin(t / (3 + i))) * math.exp(-t / 180) + base * 1e-3
        val *= 1 + 0.02 * random.uniform(-1, 1)
        return val, 0


class FakeThermometer(BaseDevice):
    """A simulated single-source temperature module."""

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
                sources=[Source(id="temp", name="Temperature", unit="°C")],
                rate=RateControl(mode=RateMode.FIXED, native_hz=1.0),
                hardware_id=f"SIM-TEMP-{tag}",
                model="SimTherm",
            ))
        return out

    def _connect(self) -> None:
        time.sleep(0.3)
        self._firmware = "T1"
        self._t0 = time.time()

    def _read(self, source):
        t = time.time() - getattr(self, "_t0", time.time())
        return 25.0 + 3.0 * math.sin(t / 10.0) + random.uniform(-0.1, 0.1), 0


class FakePowerSupply(BaseDevice):
    """A simulated bench power supply driving a 100 Ω load.

    Sinks (control): enable on/off, set-voltage, current-limit.
    Sources (data):  measured voltage, current, power.
    CV until the current limit is hit, then CC (clamped), with noise.
    """

    driver = "fake_psu"
    discoverable = True
    _R = 100.0   # load resistance (ohms)

    @classmethod
    def discover(cls):
        sources = [
            Source(id="voltage", name="Voltage", unit="V"),
            Source(id="current", name="Current", unit="A"),
            Source(id="power", name="Power", unit="W"),
        ]
        sinks = [
            Sink(id="enable", name="Enable", kind=SinkKind.TOGGLE, value=False),
            Sink(id="voltage", name="Set Voltage", kind=SinkKind.SETPOINT,
                 params=(Param("v", "float", "V", minimum=0.0, maximum=30.0),),
                 value=5.0),
            Sink(id="current_limit", name="Current Limit", kind=SinkKind.SETPOINT,
                 params=(Param("i", "float", "A", minimum=0.0, maximum=5.0),),
                 value=1.0),
        ]
        return [cls(
            instance_id="sim:psu:1",
            name="Sim Power Supply",
            interface=Interface(kind="sim", params={"addr": "PSU1"}),
            sources=sources,
            sinks=sinks,
            rate=RateControl(mode=RateMode.SETTABLE, native_hz=10.0,
                             default_hz=5.0, min_hz=0.5, max_hz=10.0),
            primary_source="voltage",
            hardware_id="SIM-PSU-0001",
            model="SimPSU 30-5",
        )]

    def _connect(self) -> None:
        time.sleep(0.3)
        self._firmware = "PSU1.2"

    def _read(self, source):
        on = bool(self._sink_values.get("enable", False))
        vset = float(self._sink_values.get("voltage", 0.0))
        ilim = float(self._sink_values.get("current_limit", 1.0))
        if not on:
            v = i = 0.0
        else:
            i_ideal = vset / self._R
            if i_ideal > ilim:
                i, v = ilim, ilim * self._R
            else:
                v, i = vset, i_ideal
            v *= 1 + 0.005 * random.uniform(-1, 1)
            i *= 1 + 0.01 * random.uniform(-1, 1)
        value = {"voltage": v, "current": i, "power": v * i}[source.id]
        return value, 0
