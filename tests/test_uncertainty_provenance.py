"""X2b — runtime σ re-declaration + provenance time-resolution (DESIGN §19.0).

set_uncertainty() updates a device's live model (reflected in its descriptor and
flagged for a re-push); the store's device change-log then time-resolves which model
was in effect at each moment, so a historic window recovers the right σ (the Keithley
range-dependent case). Qt-free — data-plane job."""
import os
import tempfile

from ferrodac.core.sourceid import uncertainty_at
from ferrodac.core.uncertainty import Abs, Rel, Spec
from ferrodac.devices import fake
from ferrodac.devices.uncertainty_specs import keithley_current_uncertainty
from ferrodac.store.zarrstore import ZarrStore


# -- device side: set_uncertainty → descriptor + dirty flag ------------------ #

def test_set_uncertainty_reflects_in_descriptor_and_flags_dirty():
    dev = fake.FakeThermometer.discover()[0]
    seeded = next(s for s in dev.describe().sources if s.id == "temp")
    assert seeded.uncertainty == Spec(random=Abs(0.06), systematic=Abs(0.2))
    assert dev.take_provenance_dirty() is False           # nothing changed yet

    dev.set_uncertainty("temp", Rel(0.01))
    assert dev.take_provenance_dirty() is True             # flagged for re-push
    assert dev.take_provenance_dirty() is False            # ...and consumed once
    reflected = next(s for s in dev.describe().sources if s.id == "temp")
    assert reflected.uncertainty == Rel(0.01)


def test_setting_the_same_model_is_not_dirty():
    dev = fake.FakeThermometer.discover()[0]
    dev.take_provenance_dirty()                             # clear the seed state
    same = next(s for s in dev.describe().sources if s.id == "temp").uncertainty
    dev.set_uncertainty("temp", same)
    assert dev.take_provenance_dirty() is False


# -- store side: the change-log time-resolves the model ---------------------- #

def test_uncertainty_time_resolves_via_change_log():
    with tempfile.TemporaryDirectory() as d:
        store = ZarrStore(os.path.join(d, "s.zarr"))
        did = "k6221:COM3"
        m1 = keithley_current_uncertainty(1e-6)            # a low range
        m2 = keithley_current_uncertainty(50e-3)           # the 100 mA range
        store.put_device(did, {"uncertainty:iout": m2.to_dict()})
        store.emit_device_meta(did, 100.0, "uncertainty:iout", m1.to_dict())
        store.emit_device_meta(did, 200.0, "uncertainty:iout", m2.to_dict())
        key = f"{did}/iout"
        assert uncertainty_at(store, key, 150.0) == m1     # between the two range changes
        assert uncertainty_at(store, key, 250.0) == m2     # after the second
        assert m1 != m2                                    # the ranges really do differ


def test_uncertainty_at_missing_is_none():
    with tempfile.TemporaryDirectory() as d:
        store = ZarrStore(os.path.join(d, "s.zarr"))
        assert uncertainty_at(None, "x/y", 1.0) is None            # no store
        assert uncertainty_at(store, "unknown/iout", 1.0) is None  # unknown device
        store.put_device("dev", {"name": "D"})                     # device w/o a σ field
        assert uncertainty_at(store, "dev/ch", 1.0) is None
