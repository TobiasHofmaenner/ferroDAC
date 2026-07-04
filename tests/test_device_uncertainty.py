"""Device σ declarations (DESIGN §19.0 / X2a): the centralised spec models and the
built-in drivers that carry them. Qt-free — runs in the data-plane job."""
import pytest

from ferrodac.core.uncertainty import Abs, FloorRel, Rel, Spec
from ferrodac.devices.uncertainty_specs import (K_RECT, SHELLY_HUMIDITY, SHELLY_TEMP,
                                                 gauge_uncertainty,
                                                 keithley_current_uncertainty, rel_bound)


# -- gauges: type → % of reading -------------------------------------------- #

def test_gauge_map_by_type():
    assert gauge_uncertainty("Pirani") == Spec(systematic=rel_bound(10.0))
    assert gauge_uncertainty("FullRange") == Spec(systematic=rel_bound(30.0))
    assert gauge_uncertainty("Bayard-Alpert") == Spec(systematic=rel_bound(15.0))
    assert gauge_uncertainty("CMR capacitance") == Spec(systematic=rel_bound(0.2))
    assert gauge_uncertainty("weird") == Spec(systematic=rel_bound(15.0))   # fallback


def test_gauge_sigma_is_percent_of_reading_as_1sigma():
    # ±10 % bound → 1σ = (10 %/√3) of the reading
    assert gauge_uncertainty("Pirani").sigma(1e-6) == pytest.approx(0.10 / K_RECT * 1e-6)


# -- Keithley 6221: range-resolved, random/systematic split ----------------- #

def test_keithley_picks_range_and_splits():
    m = keithley_current_uncertainty(1e-3)                 # → 2 mA range
    assert m.random == Abs(40e-9)                          # RMS noise (the random part)
    assert m.systematic == FloorRel(1e-6 / K_RECT, 0.0005 / K_RECT)   # ±(0.05 %+1 µA)/√3
    hi = keithley_current_uncertainty(50e-3)               # → 100 mA range
    assert hi.random == Abs(2e-6)
    assert hi.systematic == FloorRel(50e-6 / K_RECT, 0.001 / K_RECT)


def test_keithley_zero_and_over_range_dont_crash():
    assert keithley_current_uncertainty(0.0).random == Abs(80e-15)   # smallest (2 nA)
    assert keithley_current_uncertainty(1.0).random == Abs(2e-6)     # clamps to 100 mA


# -- Shelly ----------------------------------------------------------------- #

def test_shelly_constants():
    assert SHELLY_TEMP == Spec(systematic=Abs(0.5 / K_RECT))
    assert SHELLY_HUMIDITY == Spec(systematic=Abs(5.0 / K_RECT))


# -- the built-in sim drivers carry the models ------------------------------ #

def test_sim_gauge_sources_declare_gauge_type_models():
    from ferrodac.devices import fake
    g = fake.FakeGaugeController.discover()[0]
    assert g._sources[0].uncertainty == gauge_uncertainty("Pirani")
    assert g._sources[1].uncertainty == gauge_uncertainty("FullRange")


def test_sim_psu_and_thermometer_declare_models():
    from ferrodac.devices import fake
    psu = {s.id: s for s in fake.FakePowerSupply.discover()[0]._sources}
    assert psu["voltage"].uncertainty == FloorRel(0.01, 0.001)
    assert psu["power"].uncertainty == Rel(0.02)
    temp = fake.FakeThermometer.discover()[0]._sources[0]
    assert temp.uncertainty == Spec(random=Abs(0.06), systematic=Abs(0.2))


def test_sim_rga_has_no_sigma():
    from ferrodac.devices import fake
    assert fake.FakeRGA.discover()[0]._sources[0].uncertainty is None
