"""A Detector — an OCR-backed virtual source reading a ROI of a frame.

It crops its region of interest, recognises text, parses it to the configured
type, and applies a failure policy when parsing fails. The Dashboard registers
each Detector as a normal SourcePort, so its value routes like any other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ocr import preprocess, recognize

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

# parse_as -> routing dtype (what compatible-sink filtering uses)
_PARSE_DTYPE = {"float": "float", "int": "float", "bool": "bool", "text": "string"}

PARSE_LABELS = [("float", "Float"), ("int", "Integer"),
                ("bool", "Boolean"), ("text", "Text")]
FAIL_LABELS = [("nan", "Not-a-number (gap)"), ("zero", "Zero"),
               ("hold", "Hold last good")]
WHITELIST_PRESETS = {
    "float": "0123456789.-",
    "int": "0123456789-",
    "bool": "",
    "text": "",
}


@dataclass
class Detector:
    id: str
    name: str
    sink_key: str                  # the image display sink it reads from
    roi: tuple                     # (x, y, w, h) in image pixels
    parse_as: str = "float"        # float | int | bool | text
    on_fail: str = "nan"           # nan | zero | hold
    whitelist: str = "0123456789.-"
    invert: bool = False
    threshold: bool = False
    scale: int = 3
    psm: int = 7
    # runtime state
    panel: object = None
    last_text: str = ""
    last_value: object = None
    _last_good: object = None

    @property
    def dtype(self) -> str:
        return _PARSE_DTYPE.get(self.parse_as, "string")

    # -- processing ----------------------------------------------------------
    def crop(self, rgb):
        x, y, w, h = self.roi
        H, W = rgb.shape[:2]
        x = max(0, min(int(x), W - 1))
        y = max(0, min(int(y), H - 1))
        w = max(1, min(int(w), W - x))
        h = max(1, min(int(h), H - y))
        return rgb[y:y + h, x:x + w]

    def read(self, rgb):
        """OCR the ROI of a full RGB frame → (value, raw_text, status)."""
        gray = preprocess(self.crop(rgb), self.invert, self.threshold, self.scale)
        text = recognize(gray, self.whitelist, self.psm)
        self.last_text = text
        value, status = self._parse(text)
        self.last_value = value
        return value, text, status

    def _parse(self, text: str):
        if self.parse_as == "text":
            return text, (0 if text else 1)
        if self.parse_as == "bool":
            t = text.strip().lower()
            if t in ("1", "on", "true", "yes", "hi", "high"):
                self._last_good = True
                return True, 0
            if t in ("0", "off", "false", "no", "lo", "low"):
                self._last_good = False
                return False, 0
            return self._fail(False)
        m = _NUM_RE.search(text.replace(" ", ""))
        if m:
            try:
                v = float(m.group())
                if self.parse_as == "int":
                    v = float(round(v))
                self._last_good = v
                return v, 0
            except ValueError:
                pass
        return self._fail(float("nan"))

    def _fail(self, nan_value):
        if self.on_fail == "zero":
            return (False if self.parse_as == "bool" else 0.0), 0
        if self.on_fail == "hold" and self._last_good is not None:
            return self._last_good, 0
        return nan_value, 1
