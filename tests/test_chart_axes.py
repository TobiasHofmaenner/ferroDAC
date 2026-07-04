"""ChartPanel unit-aware multi-axis (DESIGN §19.0 / U2). One Y axis per physical
DIMENSION: same-dimension sources share an axis (converted to a common display unit);
a new dimension allocates a new right-side axis. UI-marked (needs a QApplication)."""
import types

import pytest

pytest.importorskip("qtpy")
pytestmark = pytest.mark.ui


def _src(name, unit):
    return types.SimpleNamespace(name=name, label=name, unit=unit, dtype="float")


def _chart(qapp):
    from ferrodac.ui.panels import ChartPanel
    return ChartPanel()


def _primary(p):
    return next(s for s in p._axes.values() if s.primary)


def _extras(p):
    return [s for s in p._axes.values() if not s.primary]


def test_first_source_labels_the_left_axis_from_its_unit(qapp):
    p = _chart(qapp)
    p.add_source("g/p", _src("pressure", "mbar"))
    assert len(p._axes) == 1
    prim = _primary(p)
    assert prim.display_unit == "mbar"
    assert p.plot.getAxis("left").labelText == "[mbar]"


def test_same_dimension_shares_one_axis_with_conversion(qapp):
    p = _chart(qapp)
    p.add_source("g/mbar", _src("A", "mbar"))
    p.add_source("g/torr", _src("B", "Torr"))
    # Both are pressure → ONE axis, displayed in the first-seen unit (mbar).
    assert len(p._axes) == 1
    assert _primary(p).display_unit == "mbar"
    # The Torr curve carries a conversion to mbar: 1 Torr ≈ 1.33322 mbar.
    conv = p._meta["g/torr"][2]
    assert conv is not None and conv(1.0) == pytest.approx(1.33322, rel=1e-4)
    # The mbar curve needs no conversion (identity).
    assert p._meta["g/mbar"][2] is None


def test_new_dimension_allocates_a_second_axis(qapp):
    p = _chart(qapp)
    p.add_source("g/p", _src("pressure", "mbar"))
    p.add_source("s/t", _src("temp", "°C"))
    assert len(p._axes) == 2
    extra = _extras(p)
    assert len(extra) == 1
    assert extra[0].display_unit == "°C"
    assert extra[0].ax.labelText == "[°C]"
    # The temperature curve lives in its own ViewBox, not the main one.
    assert p._curves["s/t"] not in p._pi.vb.addedItems
    assert p._curves["s/t"] in extra[0].vb.addedItems


def test_dimensionless_sources_share_a_single_axis(qapp):
    p = _chart(qapp)
    p.add_source("a/x", _src("X", ""))       # arbitrary / unitless
    p.add_source("b/y", _src("Y", "a.u."))
    assert len(p._axes) == 1                  # dimensionless family → one axis
    assert p.plot.getAxis("left").labelText == ""    # no meaningless [a_u] label


def test_removing_the_last_source_hides_its_extra_axis(qapp):
    p = _chart(qapp)
    p.add_source("g/p", _src("pressure", "mbar"))
    p.add_source("s/t", _src("temp", "°C"))
    extra = _extras(p)[0]
    p.remove_source("s/t")
    assert not extra.keys                      # slot emptied
    assert not extra.ax.isVisible()            # axis hidden (slot kept for reuse)
    assert _primary(p).keys == {"g/p"}         # primary untouched


def test_feed_converts_and_never_raises(qapp):
    p = _chart(qapp)
    p.add_source("g/mbar", _src("A", "mbar"))
    p.add_source("g/torr", _src("B", "Torr"))
    batch = [types.SimpleNamespace(key="g/torr", value=1.0, status=0, t=1000.0),
             types.SimpleNamespace(key="g/mbar", value=2.0, status=0, t=1000.0)]
    p.feed(batch)                              # must not raise
    # raw magnitude is buffered in the SOURCE unit; conversion is applied at draw time
    assert list(p._buf["g/torr"].y) == [1.0]
