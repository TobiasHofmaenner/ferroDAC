"""Add a processor onto a source — the generic creator.

Any registered processor (built-in or from a plugin) can be instantiated on a
compatible source here; its output ports then appear as virtual sources you can route
to a Bars panel, a chart, etc. This is what makes a plugin processor (e.g. the
window-integral example) usable, and what lets gas analysis be "add Gas → route its
outputs" rather than a bespoke panel.

v1 instantiates with the processor's default parameters; per-processor parameter
editing can layer on later (a generic form from its state()).
"""
from qtpy.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                            QLabel)

from ..analysis import PROCESSOR_TYPES


class AddProcessorDialog(QDialog):
    def __init__(self, dashboard, parent=None):
        super().__init__(parent)
        self.dash = dashboard
        self.setWindowTitle("Add processor")
        self.setMinimumWidth(360)
        form = QFormLayout(self)

        self._kind = QComboBox()
        for kind, cls in sorted(PROCESSOR_TYPES.items(),
                                key=lambda kc: getattr(kc[1], "label", kc[0]).lower()):
            self._kind.addItem(getattr(cls, "label", kind), kind)
        self._kind.currentIndexChanged.connect(self._refilter)
        form.addRow("Processor", self._kind)

        self._src = QComboBox()
        form.addRow("Input source", self._src)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#8b95a4; font-size:11px;")
        form.addRow("", self._hint)

        self._bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._bb.accepted.connect(self.accept)
        self._bb.rejected.connect(self.reject)
        form.addRow(self._bb)
        self._refilter()

    def _refilter(self, *_):
        """List only the sources whose dtype the selected processor accepts."""
        kind = self._kind.currentData()
        cls = PROCESSOR_TYPES.get(kind)
        accepts = getattr(cls, "accepts", "trace")
        self._src.clear()
        for key, sp in sorted(self.dash._sources.items()):
            if getattr(sp, "dtype", None) == accepts:
                self._src.addItem(f"{getattr(sp, 'label', None) or sp.name}  ({key})", key)
        ok = self._src.count() > 0
        self._hint.setText("" if ok else
                           f"No '{accepts}' sources available to feed this processor. "
                           "Connect a device or add one that produces that data first.")
        self._src.setEnabled(ok)
        self._bb.button(QDialogButtonBox.Ok).setEnabled(ok)

    def result(self):
        """(kind, input_source_key) — or (None, None) if nothing valid is selected."""
        if self._src.count() == 0:
            return None, None
        return self._kind.currentData(), self._src.currentData()
