"""Offscreen render of a panel to PNG — visual QA without a display.

Lets a developer (or an agent that can read images) *see* a panel's actual
rendering with representative data, instead of reasoning about pyqtgraph blind.
Uses Qt's offscreen platform + QWidget.grab(), so no display is needed.

    python tools/render.py <scenario> [out.png]

Scenarios: waterfall · waterfall-gap · specwf · chart · spectrum  (or `all`).
Default output: /tmp/ferrodac_<scenario>.png
"""

from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from qtpy.QtWidgets import QApplication

from ferrodac.core.trace import Trace

_BASE = 1_000_000.0
_MZ = np.linspace(1, 50, 128)


def _spectrum(noise=0.1):
    y = sum(np.exp(-((_MZ - mz) ** 2) / 0.3) * a
            for mz, a in [(18, 1.0), (28, 0.6), (32, 0.45), (44, 0.3)])
    return (y + 1e-3) * (1 + noise * np.random.rand(len(_MZ)))


def _scan(t, y=None):
    return types.SimpleNamespace(
        key="k", t=t, partial=False,
        value=Trace(x=_MZ, y=_spectrum() if y is None else y, x_label="m/z", x_unit=""))


def _feed_trace(panel, cadence=40.0, n=15, gap_after=None):
    """Feed n scans `cadence` s apart; `gap_after` injects a long outage."""
    panel.add_source("k", types.SimpleNamespace(name="Mass spectrum", unit="mbar",
                                                 dtype="trace"))
    panel._src_key = "k"
    t = _BASE
    span_end = _BASE
    for i in range(n):
        panel.feed([_scan(t)])
        span_end = t
        t += cadence
        if gap_after is not None and i == gap_after:
            t += cadence * 30                      # device offline ~30× cadence
    panel.set_window(_BASE, span_end + cadence)


def _waterfall(gap=False):
    from ferrodac.ui.panels import WaterfallPanel
    p = WaterfallPanel()
    p.resize(560, 480)
    _feed_trace(p, gap_after=6 if gap else None)
    return p


def _specwf():
    from ferrodac.ui.panels import SpectrumWaterfallPanel
    p = SpectrumWaterfallPanel()
    p.resize(560, 520)
    _feed_trace(p)
    return p


def _chart():
    from ferrodac.ui.panels import ChartPanel
    p = ChartPanel()
    p.resize(620, 360)
    p.add_source("g", types.SimpleNamespace(name="Pirani", unit="mbar", dtype="float"))
    t = _BASE + np.arange(600) * 0.5
    for i in range(600):
        p.feed([types.SimpleNamespace(key="g", t=t[i], status=0,
                                      value=1e-6 * (1 + 0.5 * np.sin(t[i] / 30)))])
    return p


def _spectrum_panel():
    from ferrodac.ui.panels import SpectrumPanel
    p = SpectrumPanel()
    p.resize(620, 360)
    p.add_source("k", types.SimpleNamespace(name="Mass spectrum", unit="mbar",
                                            dtype="trace"))
    p._src_key = "k"
    p.feed([_scan(_BASE)])
    return p


SCENARIOS = {
    "waterfall": lambda: _waterfall(gap=False),
    "waterfall-gap": lambda: _waterfall(gap=True),
    "specwf": _specwf,
    "chart": _chart,
    "spectrum": _spectrum_panel,
}


def render(widget, path):
    app = QApplication.instance()
    widget.show()
    for _ in range(3):
        app.processEvents()
    ok = widget.grab().save(path)
    widget.close()
    return ok


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    which = argv[0] if argv else "all"
    names = list(SCENARIOS) if which == "all" else [which]
    app = QApplication.instance() or QApplication([])   # held for the whole run
    for name in names:
        if name not in SCENARIOS:
            print(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)} or 'all'")
            return 2
        out = argv[1] if len(argv) > 1 and which != "all" else f"/tmp/ferrodac_{name}.png"
        ok = render(SCENARIOS[name](), out)
        print(f"{'✓' if ok else '✗'} {name} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
