"""core/uncertainty.py — the declarative σ models (DESIGN §19.0). Qt-free / numpy-only,
so this runs in the data-plane job. Pins the sigma() contract, the random/systematic
split, and serialisation round-trips against the canonical cases."""
import numpy as np
import pytest

from ferrodac.core.uncertainty import (Abs, FloorRel, Measured, Rel, Spec,
                                        Uncertainty)


# -- Abs / Rel / FloorRel --------------------------------------------------- #

def test_abs_is_constant_and_broadcasts():
    m = Abs(2e-9)
    assert m.sigma(1.3e-6) == pytest.approx(2e-9)
    out = m.sigma(np.array([1.0, 5.0, 9.0]))
    np.testing.assert_allclose(out, [2e-9, 2e-9, 2e-9])


def test_rel_is_fraction_of_magnitude():
    # a gauge: ±0.15 % of reading at 1.3e-6 mbar
    assert Rel(0.0015).sigma(1.3e-6) == pytest.approx(1.95e-9)
    assert Rel(0.1).sigma(-5.0) == pytest.approx(0.5)          # uses |x|


def test_floorrel_is_quadrature():
    m = FloorRel(1e-9, 0.001)
    assert m.sigma(0.0) == pytest.approx(1e-9)                 # floor dominates near 0
    assert m.sigma(1.0) == pytest.approx(np.hypot(1e-9, 0.001))
    # large signal → the relative term dominates
    assert m.sigma(1e6) == pytest.approx(0.001 * 1e6, rel=1e-6)


def test_floorrel_vectorised():
    m = FloorRel(0.5, 0.01)
    out = m.sigma(np.array([0.0, 100.0]))
    np.testing.assert_allclose(out, [0.5, np.hypot(0.5, 1.0)])


# -- Measured --------------------------------------------------------------- #

def test_measured_has_no_value_model():
    with pytest.raises(TypeError):
        Measured("dev/sigma").sigma(1.0)


# -- Spec: the random/systematic split (the Keithley case) ------------------ #

def test_spec_combines_in_quadrature():
    m = Spec(random=Abs(3.0), systematic=Abs(4.0))
    assert m.sigma(0.0) == pytest.approx(5.0)                  # 3–4–5


def test_spec_exposes_the_parts_separately():
    # SMU-like: random read noise + a systematic gain/offset spec
    m = Spec(random=Abs(1e-4), systematic=FloorRel(6e-4, 0.00012))
    assert m.random.sigma(0.0) == pytest.approx(1e-4)
    assert m.systematic.sigma(1.0) == pytest.approx(np.hypot(6e-4, 0.00012))


def test_spec_with_one_part_only():
    assert Spec(random=Rel(0.1)).sigma(2.0) == pytest.approx(0.2)
    assert Spec().sigma(3.0) == pytest.approx(0.0)             # no model → σ = 0


# -- serialisation (provenance change-log + export manifest) ---------------- #

@pytest.mark.parametrize("m", [
    Abs(2e-9),
    Rel(0.0015),
    FloorRel(6e-4, 0.00012),
    Measured("dev/sigma"),
    Spec(random=Abs(1e-4), systematic=FloorRel(6e-4, 0.00012)),
    Spec(random=Rel(0.1)),
])
def test_round_trips_through_dict(m):
    assert Uncertainty.from_dict(m.to_dict()) == m


def test_from_dict_none_and_unknown():
    assert Uncertainty.from_dict(None) is None
    with pytest.raises(ValueError):
        Uncertainty.from_dict({"type": "bogus"})
