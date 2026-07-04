"""CompositionPanel redraws from the FIT RESULTS — the derived gas readings, whose
value and inline σ travel together (race-free) — not from the raw spectrum, whose
fit is still on the offload worker at that moment (§19.7: bars lagged one scan and
the last scan of a session never displayed). UI-marked (needs a QApplication)."""
import numpy as np
import pytest

pytest.importorskip("qtpy")
pytestmark = pytest.mark.ui


class _Analyzer:
    gas_names = ["N2", "CO2"]
    last_amounts = {"N2": 1.0, "CO2": 0.0}     # stale (previous scan) on purpose
    last_sd = {}
    last_residual = 0.05
    last_degenerate = []
    unit = "mbar"


def _panel(analyzer):
    from ferrodac.ui.panels import CompositionPanel
    p = CompositionPanel()
    p._proc_id = "g1"
    p._get = lambda pid: analyzer
    p._src_key = "qms/spec"
    return p


def test_bars_redraw_from_derived_readings_not_the_raw_trace(qapp):
    from ferrodac.core.reading import Reading
    from ferrodac.core.trace import Trace
    p = _panel(_Analyzer())
    calls = []
    p._view.set_bars = lambda labels, heights, errors=None, **k: \
        calls.append((list(labels), list(heights), errors))
    # The raw spectrum arriving must NOT redraw: the fit for it hasn't run yet,
    # so the analyzer attributes still describe the previous scan.
    x = np.arange(1.0, 50.0)
    p.feed([Reading("qms", "spec", 1.0, Trace(x, np.zeros_like(x)))])
    assert not calls
    # The fit results arriving DO redraw — heights and whiskers from the readings.
    p.feed([Reading("gas", "g1/N2", 2.0, 10.0, sigma=(1.0, 2.0)),
            Reading("gas", "g1/CO2", 2.0, 0.0, sigma=(0.0, 0.3))])
    assert len(calls) == 1
    labels, heights, errors = calls[0]
    assert labels == ["N2", "CO2"]
    assert heights == [10.0, 0.0]
    lo, hi = errors
    assert lo == [1.0, 0.0] and hi == [2.0, 0.3]   # asymmetric whiskers, per gas
