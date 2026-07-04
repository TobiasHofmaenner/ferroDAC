"""Processor.units_out() — the unit-algebra contract (DESIGN §19.0). Default is
identity (out-unit = in-unit, the pass-through/cursor case); dimension-changing
processors override. Qt-free — runs in the data-plane job."""
from ferrodac.analysis.processor import Processor


def test_default_units_out_is_identity():
    p = Processor("p1", "in")
    assert p.units_out("mbar") == "mbar"
    assert p.units_out("") == ""


def test_dimension_changing_processors_override():
    class Ratio(Processor):
        def units_out(self, input_unit):
            return ""                        # dimensionless

    class Integral(Processor):
        def units_out(self, input_unit):
            return f"{input_unit}·s"         # ×(x-unit)

    assert Ratio("r", "in").units_out("mbar") == ""
    assert Integral("i", "in").units_out("mbar") == "mbar·s"
