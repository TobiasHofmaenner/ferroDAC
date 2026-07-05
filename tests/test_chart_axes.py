"""ChartPanel single-axis + dimensional routing gate (Option B, docs/AXIS-DECISION-2026-07):
ONE physical dimension per chart — the first real-unit source claims the Y axis, same-
dimension sources share it (converted, mbar+Torr), a dimensionally-incompatible source is
REFUSED (add_source→False so the Dashboard drops the route), and a source that bound with an
unknown unit adopts the axis when its real unit arrives (the reload case). UI-marked."""
import types

import numpy as np
import pytest

pytest.importorskip("qtpy")
pytestmark = pytest.mark.ui


def _src(name, unit):
    return types.SimpleNamespace(name=name, label=name, unit=unit, dtype="float")


def _chart(qapp):
    from ferrodac.ui.panels import ChartPanel
    return ChartPanel()


def test_first_source_labels_the_axis_from_its_unit(qapp):
    p = _chart(qapp)
    assert p.add_source("g/p", _src("pressure", "mbar")) is True
    assert p.display_unit == "mbar"
    assert p.plot.getAxis("left").labelText == "[mbar]"


def test_same_dimension_shares_the_axis_with_conversion(qapp):
    p = _chart(qapp)
    p.add_source("g/mbar", _src("A", "mbar"))
    assert p.add_source("g/torr", _src("B", "Torr")) is True    # pressure → accepted, converted
    assert p.display_unit == "mbar"                             # first-seen unit displays
    # the Torr curve carries a conversion to mbar (1 Torr ≈ 1.33322 mbar); mbar is identity
    conv = p._conv_of["g/torr"]
    assert conv is not None and conv(1.0) == pytest.approx(1.33322, rel=1e-4)
    assert p._conv_of["g/mbar"] is None


def test_incompatible_dimension_is_refused(qapp):
    """The gate: once a chart holds pressure, routing a temperature returns False (the
    Dashboard drops the route) — no second axis, no leftover state."""
    p = _chart(qapp)
    p.add_source("g/p", _src("pressure", "mbar"))
    assert p.add_source("s/t", _src("temp", "°C")) is False
    assert "s/t" not in p._curves and p.display_unit == "mbar"
    assert p.accepts_unit("Torr") is True and p.accepts_unit("°C") is False


def test_dimensionless_sources_share_the_axis(qapp):
    p = _chart(qapp)
    assert p.add_source("a/x", _src("X", "")) is True        # arbitrary / unitless
    assert p.add_source("b/y", _src("Y", "a.u.")) is True    # dimensionless never conflicts
    assert p.plot.getAxis("left").labelText == ""            # no meaningless [a_u] label
    assert p.add_source("g/p", _src("P", "mbar")) is True    # a real dim can still be adopted


def test_removing_all_sources_resets_the_dimension(qapp):
    """Empty the chart → it forgets its dimension so it can adopt a new one (and the
    Dashboard frees the sink's unit gate)."""
    changed = []
    p = _chart(qapp)
    p.on_dim_changed = changed.append
    p.add_source("g/p", _src("pressure", "mbar"))
    p.remove_source("g/p")
    assert p._dim is None and p.display_unit == ""
    assert changed[-1] == ""                                  # notified the Dashboard
    assert p.add_source("s/t", _src("temp", "°C")) is True    # now free to adopt temperature
    assert p.display_unit == "°C"


def test_late_unit_adopts_the_axis_in_place(qapp):
    """The reload case (bug #1): a source binds with unit='' (a historic port before the
    device reconnects), then its real unit arrives on a re-add → the chart adopts the
    dimension in place, no viewbox surgery, buffered data preserved."""
    p = _chart(qapp)
    p.add_source("s/t", _src("temp", ""))                    # unknown unit first
    p.feed([types.SimpleNamespace(key="s/t", value=22.0, status=0, t=1.0)])
    assert p.add_source("s/t", _src("temp", "°C")) is True   # real unit arrives
    assert p.display_unit == "°C"
    assert p.plot.getAxis("left").labelText == "[°C]"
    assert len(p._buf["s/t"]) == 1                           # data preserved


def test_temperature_defaults_to_linear_and_keeps_its_unit_label(qapp):
    """A temperature chart must default to a LINEAR Y axis — on log, ~20 °C data sits
    inside one decade so pyqtgraph shows no ticks and the axis (with its °C label)
    collapses. Pressure stays log (vacuum spans decades)."""
    t = _chart(qapp)
    t.add_source("s/t", _src("temp", "°C"))
    assert t._logy is False
    assert t.plot.getAxis("left").labelText == "[°C]"

    p = _chart(qapp)
    p.add_source("g/p", _src("P", "mbar"))
    assert p._logy is True                     # pressure keeps the log default


def test_explicit_log_choice_overrides_and_persists(qapp):
    """A user's explicit Log-Y toggle wins over the per-dimension default and is the
    only case a `logy` is saved (so a fresh auto chart restores to its default)."""
    t = _chart(qapp)
    t.apply_config({"logy": True})             # force log on a temp chart
    t.add_source("s/t", _src("temp", "°C"))
    assert t._logy is True
    assert "logy" in t.state()                 # explicit → persisted

    auto = _chart(qapp)
    auto.add_source("s/t", _src("temp", "°C"))
    assert "logy" not in auto.state()          # auto default → not persisted


def test_uncertainty_bands_toggle_convert_and_scale(qapp):
    p = _chart(qapp)
    p.apply_config({"logy": False})           # linear axis → assert raw values directly
    p.set_sigma_provider(lambda key, t, v: 0.1 * np.abs(np.asarray(v, float)))  # σ = 10 %
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=10.0, status=0, t=1000.0),
            types.SimpleNamespace(key="g/p", value=20.0, status=0, t=1001.0)])

    assert "g/p" not in p._bands              # off by default
    p.apply_config({"show_sigma": True})
    assert "g/p" in p._bands                  # band created on toggle
    lo, hi, _fill = p._bands["g/p"]
    np.testing.assert_allclose(lo.getData()[1], [9.0, 18.0])    # value − 1σ
    np.testing.assert_allclose(hi.getData()[1], [11.0, 22.0])   # value + 1σ

    p.apply_config({"sigma_2": True})         # k = 2 → band doubles
    lo2, hi2, _f = p._bands["g/p"]
    np.testing.assert_allclose(lo2.getData()[1], [8.0, 16.0])
    np.testing.assert_allclose(hi2.getData()[1], [12.0, 24.0])

    p.apply_config({"show_sigma": False})     # toggle off → removed
    assert "g/p" not in p._bands


def test_clear_history_clears_stale_bands(qapp):
    """clear_history (a window change / re-stream) must empty the σ bands, not just the
    curves — else a source with no data in the new window keeps its old band drawn as a
    horizontal span across the window (the range-select artifact)."""
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True})
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=10.0, status=0, t=1.0, sigma=0.5),
            types.SimpleNamespace(key="g/p", value=11.0, status=0, t=2.0, sigma=0.5)])
    lo, hi, _f = p._bands["g/p"]
    assert lo.getData()[1] is not None and len(lo.getData()[1]) == 2
    p.clear_history()
    assert len(lo.getData()[1] or []) == 0 and len(hi.getData()[1] or []) == 0


def test_band_uses_inline_sigma_from_reading(qapp):
    """A processor output that CREATES uncertainty carries σ inline on the Reading; the
    chart draws the band from it — no provider needed (that's the gas-fit path)."""
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True})   # NO sigma_provider set
    p.add_source("gas/g1/H2O", _src("H2O", "mbar"))
    p.feed([types.SimpleNamespace(key="gas/g1/H2O", value=10.0, status=0, t=1.0,
                                  sigma=0.5)])
    lo, hi, _f = p._bands["gas/g1/H2O"]
    np.testing.assert_allclose(lo.getData()[1], [9.5])    # value − inline σ
    np.testing.assert_allclose(hi.getData()[1], [10.5])   # value + inline σ


def test_band_uses_asymmetric_inline_sigma(qapp):
    """A fit folded against a physical bound carries (σ_lo, σ_hi) — the band hugs
    the bound from below while keeping the full upside (§19.7)."""
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True})
    p.add_source("gas/g1/CO2", _src("CO2", "mbar"))
    p.feed([types.SimpleNamespace(key="gas/g1/CO2", value=1.0, status=0, t=1.0,
                                  sigma=(1.0, 3.0))])
    lo, hi, _f = p._bands["gas/g1/CO2"]
    np.testing.assert_allclose(lo.getData()[1], [0.0])    # value − σ_lo → the floor
    np.testing.assert_allclose(hi.getData()[1], [4.0])    # value + σ_hi (full upside)


def test_folded_band_survives_a_log_axis(qapp):
    """On a log axis the folded lower edge (value − σ_lo == 0) must be clamped to a
    positive floor, NOT NaN-ed: NaN-ing only the lower curve desynchronises the
    fill's subpath pairing and the band vanishes (§19.7). Both curves must carry
    the same finite samples."""
    p = _chart(qapp)
    p.apply_config({"logy": True, "show_sigma": True})
    p.add_source("gas/g1/N2", _src("N2", "mbar"))
    p.feed([types.SimpleNamespace(key="gas/g1/N2", value=v, status=0, t=1000.0 + i,
                                  sigma=(v, 0.5))          # folded: σ_lo == value
            for i, v in enumerate([5.0, 4.0, 1.0])])
    lo, hi, _f = p._bands["gas/g1/N2"]
    lo_y, hi_y = lo.getData()[1], hi.getData()[1]
    assert len(lo_y) == 3 and len(hi_y) == 3
    assert np.isfinite(lo_y).all() and np.isfinite(hi_y).all()   # band still drawn
    assert (np.isfinite(lo_y) == np.isfinite(hi_y)).all()        # curves in sync


def test_2sigma_never_pushes_a_folded_band_below_zero(qapp):
    """k=2 must not scale a bound-folded σ_lo past the physical x≥0 floor (§19.7)."""
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True, "sigma_2": True})
    p.add_source("gas/g1/O2", _src("O2", "mbar"))
    p.feed([types.SimpleNamespace(key="gas/g1/O2", value=v, status=0, t=1000.0 + i,
                                  sigma=(0.9 * v, 0.5))
            for i, v in enumerate([5.0, 4.0, 0.5])])
    lo, _hi, _f = p._bands["gas/g1/O2"]
    assert (lo.getData()[1] >= 0.0).all()


def test_band_converts_to_the_axis_display_unit(qapp):
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True})
    p.set_sigma_provider(lambda key, t, v: 0.0 * np.asarray(v, float) + 1.0)  # σ = 1 Torr
    p.add_source("g/mbar", _src("A", "mbar"))
    p.add_source("g/torr", _src("B", "Torr"))     # shares the pressure axis, converted
    p.feed([types.SimpleNamespace(key="g/torr", value=10.0, status=0, t=1.0)])
    lo, hi, _f = p._bands["g/torr"]
    # σ=1 Torr around 10 Torr → [9,11] Torr, displayed in mbar (×1.33322)
    np.testing.assert_allclose(hi.getData()[1], [11.0 * 1.33322], rtol=1e-4)
    np.testing.assert_allclose(lo.getData()[1], [9.0 * 1.33322], rtol=1e-4)


def test_feed_converts_and_never_raises(qapp):
    p = _chart(qapp)
    p.add_source("g/mbar", _src("A", "mbar"))
    p.add_source("g/torr", _src("B", "Torr"))
    batch = [types.SimpleNamespace(key="g/torr", value=1.0, status=0, t=1000.0),
             types.SimpleNamespace(key="g/mbar", value=2.0, status=0, t=1000.0)]
    p.feed(batch)                              # must not raise
    # raw magnitude is buffered in the SOURCE unit; conversion is applied at draw time
    assert list(p._buf["g/torr"].y) == [1.0]
