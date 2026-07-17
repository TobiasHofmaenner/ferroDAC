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


def test_curve_and_band_break_at_a_recorded_data_gap(qapp):
    """A historic re-stream reads full-res (read_raw) with NO gap marker, so two samples
    straddling a recording gap are adjacent → a line would be drawn across the gap. The
    chart must insert a NaN break from the coverage provider (display-only), and the σ
    band must break at the SAME x so the fill's subpaths stay paired."""
    p = _chart(qapp)
    p.apply_config({"logy": False, "show_sigma": True})       # linear → raw values
    p.set_sigma_provider(lambda key, t, v: np.full(len(v), 0.1))
    # coverage: data over 1000–1100 and 1300–1400, a real 200 s gap between them
    p.set_gap_provider(lambda key: [(1000.0, 1100.0), (1300.0, 1400.0)])
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=1.0, status=0, t=1000.0),
            types.SimpleNamespace(key="g/p", value=1.0, status=0, t=1100.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=1300.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=1400.0)])

    dx, dy = p._curves["g/p"].getData()
    assert np.isnan(dy).any()                                 # the line breaks (no line across)
    nan_x = dx[np.isnan(dy)]
    assert nan_x.size == 1 and 1100.0 < nan_x[0] < 1300.0     # exactly one break, inside the gap
    assert not np.isnan(dy[0]) and not np.isnan(dy[-1])       # covered stretches stay intact

    lo, hi, _f = p._bands["g/p"]                              # the band breaks at the same x
    lx, ly = lo.getData()
    _hx, hy = hi.getData()
    assert np.isnan(ly).any() and np.isnan(hy).any()
    np.testing.assert_allclose(lx[np.isnan(ly)], nan_x)


def test_no_gap_break_without_a_provider_or_when_contiguous(qapp):
    """No provider → never break (live charts don't have coverage wired); and a provider
    reporting a single contiguous interval inserts no NaN even across a config-epoch roll."""
    p = _chart(qapp)
    p.apply_config({"logy": False})
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=1.0, status=0, t=1000.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=1100.0)])
    assert not np.isnan(p._curves["g/p"].getData()[1]).any()  # no provider → no break
    p.set_gap_provider(lambda key: [(1000.0, 1100.0)])        # one contiguous interval
    p.feed([types.SimpleNamespace(key="g/p", value=3.0, status=0, t=1050.0)])
    assert not np.isnan(p._curves["g/p"].getData()[1]).any()  # contiguous → still no break


def test_parked_curve_drawn_from_query_envelope_ignores_restream_feed(qapp):
    """Phase 2 (reduce once): a parked curve is drawn from a pixel-budgeted store-query
    min/max envelope via set_window_curve; while windowed the full-res re-stream feed() is
    IGNORED for that key (the query owns it — no second, fixed-cap decimation); going live
    (clear_history) releases the key so the live tail feed drives it again."""
    p = _chart(qapp)
    p.apply_config({"logy": False})               # linear → assert raw values
    p.add_source("g/p", _src("P", "mbar"))

    p.set_query_owned(["g/p"])                     # mark before the async result lands
    assert "g/p" in p._query_owned
    p.feed([types.SimpleNamespace(key="g/p", value=999.0, status=0, t=1.0)])
    dx0 = p._curves["g/p"].getData()[0]
    assert dx0 is None or len(dx0) == 0            # re-stream feed ignored while windowed

    x = np.array([1000.0, 1000.0, 1001.0, 1001.0])   # a min/max envelope polyline
    y = np.array([1.0, 3.0, 2.0, 4.0])
    p.set_window_curve("g/p", x, y)
    dx, dy = p._curves["g/p"].getData()
    assert len(dx) == 4 and dy.max() == 4.0 and 999.0 not in dy   # envelope, not the fed value

    p.clear_history()                             # go live → feed drives again
    assert not p._query_owned
    p.feed([types.SimpleNamespace(key="g/p", value=7.0, status=0, t=2.0)])
    assert p._curves["g/p"].getData()[1][-1] == 7.0


def test_manual_zoom_fires_on_zoom_with_the_view_range(qapp):
    """Phase 3 (Fix B): a settled manual zoom reports the VISIBLE x-range via on_zoom, so
    the app can re-query that sub-window at pixel resolution. (The debounce timer's timeout
    calls _fire_zoom; we invoke it directly — the view range is what matters.)"""
    p = _chart(qapp)
    p.add_source("g/p", _src("P", "mbar"))
    seen = []
    p.on_zoom = lambda t0, t1: seen.append((t0, t1))
    p.plot.setXRange(1000.0, 2000.0, padding=0)
    p._fire_zoom()
    assert seen and abs(seen[-1][0] - 1000.0) < 50 and abs(seen[-1][1] - 2000.0) < 50


def test_release_of_query_ownership_clears_so_feed_rebuilds_monotonically(qapp):
    """Play releases the window (ChartFeed.reconcile assigns an empty owned set on the
    PARKED→PLAYING transition), which CLEARS the envelope buffer — the query
    envelope spans the whole parked window, so the resuming re-stream (which re-experiences that
    span from its start) must rebuild the buffer, not append onto the envelope's later points and
    make it step BACKWARD in time (which drew a diagonal to an out-of-order point)."""
    p = _chart(qapp)
    p.apply_config({"logy": False})
    p.add_source("g/p", _src("P", "mbar"))
    p.set_query_owned(["g/p"])
    p.set_window_curve("g/p", np.array([1000.0, 1001.0, 1002.0]), np.array([1.0, 2.0, 3.0]))
    assert len(p._curves["g/p"].getData()[0]) == 3
    p.set_query_owned([])
    assert not p._query_owned and len(p._buf["g/p"]) == 0         # buffer cleared on release
    p.feed([types.SimpleNamespace(key="g/p", value=1.0, status=0, t=1000.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=1001.0)])   # play re-streams
    assert list(p._curves["g/p"].getData()[0]) == [1000.0, 1001.0]  # rebuilt in order, no diagonal


def test_extend_back_while_live_shows_history_and_keeps_appending(qapp):
    """Grow-mode: dragging the tail back while LIVE draws the historic envelope with the
    key simply NOT query-owned, so feed() keeps appending the live tail. The redundant re-stream of the same
    span (≤ the envelope's last time) is dropped by the monotonic guard; forward live is kept.
    Regression: this path returned early / had its history dropped → the chart showed nothing."""
    p = _chart(qapp)
    p.apply_config({"logy": False})
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=1.0, status=0, t=100.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=101.0)])   # live tail
    p.clear_history()                                                          # on_reset
    p.set_window_curve("g/p", np.array([10.0, 50.0, 101.0]),
                       np.array([5.0, 6.0, 2.0]))                              # historic, un-owned
    assert "g/p" not in p._query_owned and p._buf["g/p"].x[0] == 10.0
    p.feed([types.SimpleNamespace(key="g/p", value=9.0, status=0, t=50.0)])    # redundant re-stream
    p._flush_dirty_curves()                       # redraws are throttled (~10 Hz)
    assert 9.0 not in list(p._curves["g/p"].getData()[1])                      # dropped (≤ last)
    p.feed([types.SimpleNamespace(key="g/p", value=3.0, status=0, t=102.0)])   # live continues
    p._flush_dirty_curves()
    dx = list(p._curves["g/p"].getData()[0])
    assert dx[0] == 10.0 and dx[-1] == 102.0        # history kept AND live tail appended


def test_feed_drops_backward_out_of_order_points(qapp):
    """A live time-series display must never step back in time: a stray older reading (a device
    clock correction, a leftover envelope) is dropped so connect='finite' can't draw a diagonal
    to an out-of-order point. Forward points are still accepted."""
    p = _chart(qapp)
    p.apply_config({"logy": False})
    p.add_source("g/p", _src("P", "mbar"))
    p.feed([types.SimpleNamespace(key="g/p", value=1.0, status=0, t=1000.0),
            types.SimpleNamespace(key="g/p", value=2.0, status=0, t=1001.0)])
    p.feed([types.SimpleNamespace(key="g/p", value=9.0, status=0, t=1000.5)])   # stray OLDER point
    p._flush_dirty_curves()                       # redraws are throttled (~10 Hz)
    dx, dy = p._curves["g/p"].getData()
    assert list(dx) == [1000.0, 1001.0] and 9.0 not in list(dy)  # backward point dropped
    p.feed([types.SimpleNamespace(key="g/p", value=3.0, status=0, t=1002.0)])   # forward → kept
    p._flush_dirty_curves()
    assert list(p._curves["g/p"].getData()[0]) == [1000.0, 1001.0, 1002.0]


def test_clear_history_cancels_pending_zoom_requery(qapp):
    """Review #4: a pending zoom-debounce timer must be stopped on a window reset, else it
    fires _fire_zoom on the STALE viewRange and paints the old window (and its shared query
    key kills the fresh park query)."""
    p = _chart(qapp)
    p.add_source("g/p", _src("P", "mbar"))
    p.plot.setXRange(1000, 2000, padding=0)
    p._zoom_timer.start()                             # a zoom re-query is pending
    assert p._zoom_timer.isActive()
    p.clear_history()
    assert not p._zoom_timer.isActive() and p._last_zoom_x is None


def test_y_only_zoom_does_not_refire_on_zoom(qapp):
    """Review #6: an unchanged X range (a Y-axis-only zoom still emits sigRangeChangedManually
    in pyqtgraph 0.14) must not re-fire on_zoom — it would re-query the store and redraw every
    windowed chart for zero visual change."""
    p = _chart(qapp)
    p.add_source("g/p", _src("P", "mbar"))
    fired = []
    p.on_zoom = lambda t0, t1: fired.append((t0, t1))
    p.plot.setXRange(1000, 2000, padding=0)
    p._fire_zoom()
    p._fire_zoom()                                    # same X (Y-only) → must be a no-op
    assert len(fired) == 1


def test_remove_source_clears_query_owned_so_reroute_draws(qapp):
    """Review C6: remove_source must purge the key from _query_owned, else re-routing the same
    source onto a still-parked chart leaves feed() ignoring it → a permanently blank curve."""
    p = _chart(qapp)
    p.apply_config({"logy": False})
    p.add_source("g/p", _src("P", "mbar"))
    p.set_query_owned(["g/p"])
    assert "g/p" in p._query_owned
    p.remove_source("g/p")
    assert "g/p" not in p._query_owned
    p.add_source("g/p", _src("P", "mbar"))            # re-route while still parked
    p.feed([types.SimpleNamespace(key="g/p", value=5.0, status=0, t=1.0)])
    assert p._curves["g/p"].getData()[1][-1] == 5.0   # drew (feed no longer suppressed)


def test_set_x_range_stamps_last_zoom_x(qapp):
    """Review C5: the X-link's programmatic set_x_range stamps _last_zoom_x, so a later Y-only
    zoom on the followed panel is still recognised as X-unchanged (no wasted re-query storm)."""
    p = _chart(qapp)
    p.add_source("g/p", _src("P", "mbar"))
    p.set_x_range(1000.0, 2000.0)
    assert p._last_zoom_x == (1000.0, 2000.0)
    fired = []
    p.on_zoom = lambda t0, t1: fired.append((t0, t1))
    p._fire_zoom()                                    # X came from the link, unchanged → no fire
    assert not fired


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


@pytest.mark.ui
def test_sigma_band_never_drives_the_autorange(qapp):
    """Regression (2026-07-13): with a large σ model (σ ≳ y — real gauges near
    range edges), the band's FillBetweenItem drove the Y autorange decades past
    the data, collapsing the curve to a sliver — 'enabling uncertainties hides
    my data'. Bands are context: the view must range on the DATA, whatever σ is."""
    import time as _time
    spans = {}
    for sigma_scale in (0.1, 30.0):
        p = _chart(qapp)
        p.add_source("g/p", _src("P", "mbar"))
        p.set_sigma_provider(
            lambda k, x, y, s=sigma_scale: np.abs(np.asarray(y)) * s)
        p.apply_config({"show_sigma": True, "logy": True})
        p.resize(800, 500)
        p.show()
        base = _time.time()
        p.feed([types.SimpleNamespace(key="g/p", value=1e-6 * (1 + 0.05 * i),
                                      status=0, t=base + i, sigma=None)
                for i in range(20)])
        qapp.processEvents()
        vb = p.plot.getPlotItem().getViewBox()
        vb.autoRange()
        (_, _), (y0, y1) = vb.viewRange()          # log units = decades
        spans[sigma_scale] = y1 - y0
        p.close()
    assert spans[30.0] < 0.8, f"huge σ dragged the view to {spans[30.0]:.1f} decades"
    assert abs(spans[30.0] - spans[0.1]) < 0.1     # σ magnitude must not matter
