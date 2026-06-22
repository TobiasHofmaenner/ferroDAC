"""The plugin's OWN tests (run in the plugin repo's env, where ferrodac.plugin
exists). ferroDAC's main suite does not collect examples/ (testpaths=["tests"])."""
import types

import numpy as np

from ferrodac_ext_example.processors.integrate import WindowIntegral


def test_window_integral_area():
    p = WindowIntegral("wint1", "src", lo=10.0, hi=20.0, name="peakA")
    trace = types.SimpleNamespace(x=np.linspace(0, 50, 51), y=np.ones(51))
    out = p.process(trace)
    # unit-height band integrated over [10, 20] → area ≈ 10
    assert abs(out["wint/wint1/peakA"] - 10.0) < 0.5
    assert p.outputs()[0].dtype == "float"
    assert p.state()["lo"] == 10.0
