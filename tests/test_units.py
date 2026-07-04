"""core/units.py — the pint edge adapter. These pin the CONTRACT the rest of the
system relies on (validate / canonical / dimensional compatibility / convert), not
pint's internals. Qt-free: runs in the data-plane CI job."""
import numpy as np
import pytest

from ferrodac.core import units


# -- validation & canonical spelling ---------------------------------------- #

@pytest.mark.parametrize("s", ["mbar", "Pa", "Torr", "atm", "V", "A", "K", "degC",
                               "percent", "%", "°C", "% RH", "a.u.", ""])
def test_known_and_messy_strings_validate(s):
    assert units.is_valid(s)


def test_garbage_is_invalid_and_never_raises():
    assert not units.is_valid("zorp")
    assert units.parse("zorp") is None
    assert units.convert(1.0, "zorp", "mbar") is None


def test_canonical_folds_spellings_but_keeps_unknowns():
    assert units.canonical("°C") == units.canonical("degC")      # both → same symbol
    assert units.canonical("zorp") == "zorp"                     # unknown preserved


# -- dimensional compatibility (the routing gate) --------------------------- #

def test_pressure_units_are_mutually_compatible():
    assert units.compatible("mbar", "Pa")
    assert units.compatible("Torr", "atm")
    assert units.compatible("mbar", "Torr")


def test_different_dimensions_are_incompatible():
    assert not units.compatible("mbar", "V")     # pressure vs voltage
    assert not units.compatible("mbar", "K")     # pressure vs temperature
    assert not units.compatible("V", "A")


def test_dimensionless_family_interoperates():
    for a in ("percent", "a.u.", "", "%"):
        assert units.compatible(a, "a.u."), a


def test_unparseable_units_only_match_their_own_label():
    assert units.compatible("zorp", "zorp")      # same historic label still routes
    assert not units.compatible("zorp", "mbar")  # but not against a real unit


# -- conversion (the display / export edge) --------------------------------- #

def test_pressure_conversions_have_known_factors():
    assert units.convert(1.0, "mbar", "Pa") == pytest.approx(100.0)
    assert units.convert(1.0, "Torr", "Pa") == pytest.approx(101325.0 / 760.0)
    assert units.convert(1.0, "atm", "mbar") == pytest.approx(1013.25)


def test_convert_is_vectorised():
    out = units.convert(np.array([1.0, 2.0, 3.0]), "mbar", "Pa")
    assert isinstance(out, np.ndarray)
    np.testing.assert_allclose(out, [100.0, 200.0, 300.0])


def test_affine_temperature_uses_offset_not_scale():
    # 0 °C = 273.15 K, 25 °C = 298.15 K — an offset, not a multiply.
    np.testing.assert_allclose(
        units.convert(np.array([0.0, 25.0, 100.0]), "degC", "K"),
        [273.15, 298.15, 373.15])


def test_incompatible_conversion_returns_none():
    assert units.convert(1.0, "mbar", "V") is None


# -- convert_factor: multiplicative fast path, None for affine --------------- #

def test_convert_factor_multiplicative():
    assert units.convert_factor("mbar", "Pa") == pytest.approx(100.0)


def test_convert_factor_none_for_affine_temperature():
    assert units.convert_factor("degC", "K") is None       # offset ⇒ use convert()


def test_convert_factor_none_for_incompatible():
    assert units.convert_factor("mbar", "V") is None


# -- one shared registry ----------------------------------------------------- #

def test_registry_is_a_singleton():
    assert units.registry() is units.registry()
