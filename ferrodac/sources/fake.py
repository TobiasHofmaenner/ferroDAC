"""Fake, hardware-free sources for developing & testing the v1 plumbing.

They exercise the full ``discover -> describe -> connect -> status`` loop with no
real hardware, and (being multi-channel) make the nested source/channel cards
meaningful. Real device drivers (TPG-256A, Modbus) drop in alongside these.
"""

from __future__ import annotations

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

    def _connect(self) -> None:
        time.sleep(0.4)            # simulate a handshake (shows CONNECTING)
        self._firmware = "SIMv1.0"


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
