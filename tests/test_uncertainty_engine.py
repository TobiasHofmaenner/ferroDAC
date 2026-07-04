"""store/uncertainty.py — the windowed σ reconstruction engine (DESIGN §19.0 / X3).
Qt-free — data-plane job. Uses a real ZarrStore so the change-log path is exercised."""
import os
import tempfile

import numpy as np
import pytest

from ferrodac.core.sourceid import uncertainty_at
from ferrodac.core.uncertainty import Measured, Rel
from ferrodac.devices.uncertainty_specs import (gauge_uncertainty,
                                                 keithley_current_uncertainty)
from ferrodac.store.uncertainty import band, model_timeline, reconstruct
from ferrodac.store.zarrstore import ZarrStore


def _store(tmp):
    return ZarrStore(os.path.join(tmp, "s.zarr"))


def _log(store, did, channel, events):
    """Seed a device with a σ-model change-log: events = [(t, model), ...]."""
    if events:
        store.put_device(did, {f"uncertainty:{channel}": events[-1][1].to_dict()})
    for t, m in events:
        store.emit_device_meta(did, t, f"uncertainty:{channel}", m.to_dict())


# -- single static model ----------------------------------------------------- #

def test_single_model_is_percent_of_each_value():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        m = gauge_uncertainty("Pirani")                 # Rel(0.10/√3) systematic
        _log(store, "tpg", "ch1", [(0.0, m)])
        vals = np.array([1e-6, 2e-6, 5e-6])
        sig = reconstruct(store, "tpg/ch1", np.array([10.0, 11.0, 12.0]), vals)
        np.testing.assert_allclose(sig, m.sigma(vals))


def test_no_model_or_no_store_is_all_nan():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        out = reconstruct(store, "nope/ch", np.array([1.0]), np.array([2.0]))
        assert np.isnan(out).all()
    assert np.isnan(reconstruct(None, "x/y", [1.0], [2.0])).all()


def test_empty_window():
    assert reconstruct(None, "x/y", [], []).shape == (0,)


# -- multi-epoch: the Keithley range change mid-window ----------------------- #

def test_model_changes_across_the_window():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        m1 = keithley_current_uncertainty(1e-6)         # low range
        m2 = keithley_current_uncertainty(50e-3)        # 100 mA range
        _log(store, "k", "iout", [(100.0, m1), (200.0, m2)])
        assert m1 != m2
        times = np.array([50.0, 150.0, 250.0])          # before / between / after
        vals = np.array([1e-6, 1e-6, 50e-3])
        sig = reconstruct(store, "k/iout", times, vals)
        # before the first event and between → m1; after the 2nd event → m2
        assert sig[0] == pytest.approx(m1.sigma(1e-6))
        assert sig[1] == pytest.approx(m1.sigma(1e-6))
        assert sig[2] == pytest.approx(m2.sigma(50e-3))


def test_timeline_reads_sorted_epochs():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        m1, m2 = Rel(0.01), Rel(0.02)
        store.put_device("k", {"uncertainty:iout": m1.to_dict()})   # create the group
        # emit out of order → timeline must come back sorted by time
        store.emit_device_meta("k", 200.0, "uncertainty:iout", m2.to_dict())
        store.emit_device_meta("k", 100.0, "uncertainty:iout", m1.to_dict())
        tl = model_timeline(store, "k/iout")
        assert [t for t, _ in tl] == [100.0, 200.0]
        assert [m for _, m in tl] == [m1, m2]


# -- Measured σ can't be reconstructed from the value ------------------------ #

def test_measured_model_yields_nan():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        _log(store, "dev", "ch", [(0.0, Measured("dev/sigma"))])
        out = reconstruct(store, "dev/ch", np.array([1.0, 2.0]), np.array([3.0, 4.0]))
        assert np.isnan(out).all()


# -- band helper ------------------------------------------------------------- #

def test_band_is_value_plus_minus_k_sigma():
    vals = np.array([10.0, 20.0])
    sig = np.array([1.0, 2.0])
    lo, hi = band(vals, sig, k=2.0)
    np.testing.assert_allclose(lo, [8.0, 16.0])
    np.testing.assert_allclose(hi, [12.0, 24.0])


def test_band_nan_sigma_gives_nan_bounds():
    lo, hi = band(np.array([5.0]), np.array([np.nan]))
    assert np.isnan(lo).all() and np.isnan(hi).all()


# -- adversarial-verification regressions (X3 review) ------------------------ #

def test_reconstruct_agrees_with_uncertainty_at_even_out_of_order():
    """The point-in-time resolver (cursor/tooltip) and the windowed band must match,
    including a change-log emitted OUT OF TIME ORDER and samples before the first event."""
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        m1, m2 = Rel(0.01), Rel(0.02)
        store.put_device("k", {"uncertainty:iout": m2.to_dict()})     # current = latest
        store.emit_device_meta("k", 200.0, "uncertainty:iout", m2.to_dict())
        store.emit_device_meta("k", 100.0, "uncertainty:iout", m1.to_dict())  # out of order
        key = "k/iout"
        for t in (50.0, 100.0, 150.0, 200.0, 250.0):
            model = uncertainty_at(store, key, t)
            pt = reconstruct(store, key, np.array([t]), np.array([1.0]))[0]
            assert pt == pytest.approx(model.sigma(1.0)), t
        assert uncertainty_at(store, key, 50.0) == m1     # pre-first-event → FIRST model


def test_mismatched_or_empty_arrays_never_crash():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        _log(store, "k", "iout", [(0.0, Rel(0.01))])
        out = reconstruct(store, "k/iout", np.array([1., 2., 3.]),
                          np.array([1., 2., 3., 4., 5.]))          # length mismatch
        assert out.shape == (5,) and np.isnan(out).all()
        out2 = reconstruct(store, "k/iout", np.array([]), np.array([1e-6]))   # empty times
        assert out2.shape == (1,) and np.isnan(out2).all()


def test_epoch_that_unsets_the_model_becomes_nan():
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        store.put_device("k", {"uncertainty:iout": Rel(0.01).to_dict()})
        store.emit_device_meta("k", 100.0, "uncertainty:iout", Rel(0.01).to_dict())
        store.emit_device_meta("k", 200.0, "uncertainty:iout", {"type": "bogus"})  # unknown
        out = reconstruct(store, "k/iout", np.array([150.0, 250.0]), np.array([1.0, 1.0]))
        assert out[0] == pytest.approx(Rel(0.01).sigma(1.0))   # before the change
        assert np.isnan(out[1])                                # after → no valid model
