"""The display-widget contract.

`Widget` is the STABLE, versioned surface a ferroDAC display widget implements —
the one thing third-party widget plugins subclass (via `from ferrodac.plugin import
Widget`). The built-in `Panel` is `Panel(Widget)`, so internal panels can grow extra
conveniences without those leaking into the plugin contract. Keep every method here
stable; bump the plugin `api` version if it changes.
"""
from qtpy.QtWidgets import QWidget

# The widget registry: kind -> (menu label, class). Built-ins populate it (see
# panels.PANEL_TYPES, which IS this dict); plugin widgets add themselves with the
# @register_widget decorator below, so they appear in the Add menu like any built-in.
WIDGET_TYPES: dict = {}


def register_widget(label=None):
    """Class decorator registering a Widget subclass by its ``kind`` for the Add menu.
    ``label`` defaults to the class's ``label`` attribute (or its ``kind``)::

        @register_widget("Polar plot")
        class PolarPlot(Widget): ...
    """
    def deco(cls):
        WIDGET_TYPES[cls.kind] = (label or getattr(cls, "label", cls.kind), cls)
        return cls
    return deco


class Widget(QWidget):
    """A widget that displays a routed set of Sources (and/or emits control inputs)."""

    kind = "widget"            # registry key (unique across loaded plugins)
    is_input = False           # True → a control INPUT (slider/button), not a data sink
    routable = True            # False → carries no data port (e.g. a document view)
    accepts = frozenset()      # input source dtypes it can display, e.g. {"float", "bool"}
    single_bind = False        # True → at most one routed source

    def __init__(self, parent=None):
        super().__init__(parent)
        self.panel_id = ""
        self.title = ""
        self._unsub = None
        self.export_spec = None    # per-widget render-export override {width,height,dpi}; None=project default

    # -- data lifecycle (the Dashboard + replay drive these) -----------------
    def add_source(self, key: str, source) -> None: ...

    def remove_source(self, key: str) -> None: ...

    def feed(self, batch: list) -> None: ...

    def clear_history(self) -> None:
        """Drop accumulated display data so the widget can re-experience a new slice
        from scratch — called by the replay reset when the head jumps (park / scrub /
        return to live). Default: nothing to clear."""

    def trim_to(self, x_min: float) -> None:
        """Drop accumulated data older than x_min (relative-time coords) so the live
        window slides instead of growing. Time-axis widgets override."""

    def set_window(self, t0: float, t1: float) -> None:
        """The shared time window [t0,t1] moved (live growth / scrub). Time-axis
        widgets (waterfall) override to map their Y range to it. Default: ignore."""

    def zoom_time(self, t0: float, t1: float) -> None:
        """Frame the view on the time window [t0,t1] (Zoom-to-recording / jump-to-
        tag). Each widget knows which axis is time — charts set X, waterfalls set Y.
        Default: no-op (widgets without a time axis, e.g. a spectrum, ignore it)."""

    # -- persistence ---------------------------------------------------------
    def state(self) -> dict:
        """Per-widget state to persist in a saved session (override as needed)."""
        return {}

    def set_state(self, state: dict) -> None:
        """Restore per-widget state from a saved session."""

    # -- image export --------------------------------------------------------
    def export_item(self):
        """The pyqtgraph GraphicsItem to hand to ImageExporter for a plot-image
        export, or None for a widget with nothing to render (numeric / button /
        camera / doc). Default: the single plot's item; multi-plot widgets override
        to return their whole layout."""
        plot = getattr(self, "plot", None)
        if plot is None:
            return None
        if hasattr(plot, "plotItem"):
            return plot.plotItem
        if hasattr(plot, "getPlotItem"):
            return plot.getPlotItem()
        return None

    # -- configuration (⚙) ---------------------------------------------------
    def config_fields(self) -> list:
        """Editable settings as ``[(key, label, kind, value, opts)]`` where kind is
        text / int / float / bool / choice. Every widget has a display name."""
        return [("name", "Display name", "text", self.title, {})]

    def apply_config(self, values: dict) -> None:
        if values.get("name"):
            self.set_display_name(values["name"])

    def set_display_name(self, name: str) -> None:
        """Set the widget's name (dock title + patch-bay). Plot widgets override to
        also set the plot title so it appears on exported plots."""
        self.title = name
