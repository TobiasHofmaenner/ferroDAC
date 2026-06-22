"""Plugin SDK facade + manifest parser (Phase 1 of the extension platform).

Most of these are Qt-free on purpose — a processor/driver author should be able to
build against `ferrodac.plugin` without Qt. The reference repo under examples/ doubles
as the contract fixture.
"""
import os
import types

import numpy as np
import pytest

EX = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                  "examples", "ferrodac-ext-example"))


def test_plugin_facade_exports():
    import ferrodac.plugin as P
    assert P.API_VERSION == 1
    assert (P.FLOAT, P.BOOL, P.TRACE) == ("float", "bool", "trace")
    assert P.DTYPES == frozenset({"float", "bool", "trace"})
    for name in ("Processor", "Port", "Device", "Trace"):    # Qt-free contract bases
        assert getattr(P, name).__name__ == name


def test_manifest_parse_example():
    from ferrodac.extensions import load_manifest
    m = load_manifest(EX)
    assert m.name == "ferrodac-ext-example" and m.api == 1 and m.is_compatible()
    assert len(m.processors) == 1 and not m.drivers and not m.widgets
    p = m.processors[0]
    assert p.role == "processor" and p.entry.endswith(":WindowIntegral")
    wp = m.whitepaper_path(p)
    assert wp and wp.endswith("integrate.md") and os.path.exists(wp)


def test_manifest_errors(tmp_path):
    from ferrodac.extensions import load_manifest
    from ferrodac.extensions.manifest import ManifestError
    with pytest.raises(ManifestError):                       # no manifest at all
        load_manifest(str(tmp_path))
    mf = tmp_path / "ferrodac-extension.toml"
    mf.write_text('[extension]\nname = "x"\n')               # missing api
    with pytest.raises(ManifestError):
        load_manifest(str(tmp_path))
    mf.write_text('[extension]\nname = "x"\napi = 1\n[[processors]]\nentry = "noColon"\n')
    with pytest.raises(ManifestError):                       # entry not module:Class
        load_manifest(str(tmp_path))


def test_manifest_api_incompatible(tmp_path):
    from ferrodac.extensions import load_manifest
    (tmp_path / "ferrodac-extension.toml").write_text('[extension]\nname="x"\napi=999\n')
    assert not load_manifest(str(tmp_path)).is_compatible()


def test_example_processor_against_facade():
    """A real plugin (the example) implements the SDK contract end-to-end — Qt-free."""
    import sys
    if EX not in sys.path:
        sys.path.insert(0, EX)
    from ferrodac.plugin import FLOAT, Processor
    from ferrodac_ext_example.processors.integrate import WindowIntegral
    assert issubclass(WindowIntegral, Processor)
    p = WindowIntegral("wint1", "src", lo=10.0, hi=20.0, name="peakA")
    out = p.process(types.SimpleNamespace(x=np.linspace(0, 50, 51), y=np.ones(51)))
    assert abs(out["wint/wint1/peakA"] - 10.0) < 0.5        # unit band over [10,20] ≈ 10
    assert p.outputs()[0].dtype == FLOAT
    assert p.state()["hi"] == 20.0


def test_trace_xarray_roundtrip():
    xr = pytest.importorskip("xarray")
    from ferrodac.core.trace import Trace
    t = Trace(x=np.linspace(1, 10, 10), y=np.arange(10.0), x_label="mz", x_unit="amu",
              y_label="Intensity", y_unit="A")
    da = t.to_xarray()
    assert da.dims == ("mz",) and da.attrs["units"] == "A"
    t2 = Trace.from_xarray(da)
    assert np.allclose(t2.x, t.x) and np.allclose(t2.y, t.y)
    assert t2.x_unit == "amu" and t2.y_unit == "A"


@pytest.mark.ui
def test_panel_subclasses_widget(qapp):
    from ferrodac.plugin import Widget
    from ferrodac.ui.panels import ChartPanel, Panel
    assert issubclass(Panel, Widget) and issubclass(ChartPanel, Widget)
    c = ChartPanel()                                         # the contract surface is intact
    try:
        for meth in ("add_source", "feed", "state", "set_state", "export_item",
                     "config_fields", "apply_config", "set_window", "zoom_time"):
            assert callable(getattr(c, meth))
    finally:
        c.deleteLater()
