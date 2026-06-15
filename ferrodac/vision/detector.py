"""A Detector — an OCR-backed virtual source reading a ROI of a frame.

It crops its region of interest, recognises text, and turns it into a trustworthy
routable value:

    OCR text → parse → gain·x + offset → accept-window → stability filter

with a failure policy (NaN gap / zero / hold-last) whenever a step rejects the
read. The Dashboard registers each Detector as a normal SourcePort.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

from .ocr import get_engine, preprocess

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
_TRUE = ("1", "on", "true", "yes", "hi", "high")
_FALSE = ("0", "off", "false", "no", "lo", "low")

# parse_as -> routing dtype (what compatible-sink filtering uses)
_PARSE_DTYPE = {"float": "float", "int": "float", "bool": "bool", "text": "string"}

PARSE_LABELS = [("float", "Float"), ("int", "Integer"),
                ("bool", "Boolean"), ("text", "Text")]
FAIL_LABELS = [("nan", "Not-a-number (gap)"), ("zero", "Zero / empty"),
               ("hold", "Hold last good")]
WHITELIST_PRESETS = {
    "float": "0123456789.-", "int": "0123456789-", "bool": "", "text": "",
}

# Configurable fields that are serialized with a saved session.
CONFIG_FIELDS = (
    "engine", "parse_as", "on_fail", "whitelist", "unit",
    "scale", "invert", "threshold", "adaptive", "thresh_value", "denoise", "rotate",
    "gain", "offset", "vmin", "vmax", "smooth", "rate_hz",
)


@dataclass
class Detector:
    id: str
    name: str
    sink_key: str                  # the image display sink it reads from
    roi: tuple                     # (x, y, w, h) in image pixels
    engine: str = "general"        # general (DNN) | tesseract
    parse_as: str = "float"        # float | int | bool | text
    on_fail: str = "nan"           # nan | zero | hold
    whitelist: str = "0123456789.-"
    unit: str = ""
    psm: int = 7
    # preprocessing
    scale: int = 3
    invert: bool = False
    threshold: bool = False
    adaptive: bool = False         # adaptive vs Otsu/manual when threshold is on
    thresh_value: int = 0          # >0 = manual threshold, else Otsu
    denoise: bool = False
    rotate: float = 0.0            # degrees (deskew)
    # value pipeline
    gain: float = 1.0
    offset: float = 0.0
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    smooth: int = 1                # stability window (median/mode of last N)
    rate_hz: float = 5.0
    # runtime state
    panel: object = None
    last_text: str = ""
    last_value: object = None
    _good: object = field(default=None, repr=False)        # rolling window
    _last_good: object = field(default=None, repr=False)

    @property
    def dtype(self) -> str:
        return _PARSE_DTYPE.get(self.parse_as, "string")

    @property
    def numeric(self) -> bool:
        return self.parse_as in ("float", "int")

    # -- processing ----------------------------------------------------------
    def crop(self, rgb):
        x, y, w, h = self.roi
        H, W = rgb.shape[:2]
        x = max(0, min(int(x), W - 1))
        y = max(0, min(int(y), H - 1))
        w = max(1, min(int(w), W - x))
        h = max(1, min(int(h), H - y))
        return rgb[y:y + h, x:x + w]

    def preprocessed(self, rgb):
        return preprocess(self.crop(rgb), self.invert, self.threshold, self.scale,
                          self.adaptive, self.denoise, self.rotate, self.thresh_value)

    def read(self, rgb):
        """OCR + parse + transform + range + stability → (value, raw_text, status)."""
        text, _dbg = get_engine(self.engine).read(self.crop(rgb), self)
        self.last_text = text
        value, status = self._finalize(*self._parse_raw(text))
        self.last_value = value
        return value, text, status

    # -- pipeline stages -----------------------------------------------------
    def _parse_raw(self, text: str):
        """OCR text → (raw_value, ok) — no transform / failure policy yet."""
        if self.parse_as == "text":
            return text, bool(text)
        if self.parse_as == "bool":
            t = text.strip().lower()
            if t in _TRUE:
                return True, True
            if t in _FALSE:
                return False, True
            return None, False
        m = _NUM_RE.search(text.replace(" ", ""))
        if m:
            try:
                v = float(m.group())
                return (float(round(v)) if self.parse_as == "int" else v), True
            except ValueError:
                pass
        return None, False

    def _finalize(self, raw, ok):
        if ok and self.numeric:
            raw = self.gain * raw + self.offset
            if (self.vmin is not None and raw < self.vmin) or \
               (self.vmax is not None and raw > self.vmax):
                ok = False                      # outside the accept-window
        if ok:
            self._push(raw)
            return self._smoothed(), 0
        return self._on_fail()

    def _push(self, v):
        n = max(1, int(self.smooth))
        if self._good is None or self._good.maxlen != n:
            self._good = deque(self._good or (), maxlen=n)
        self._good.append(v)
        self._last_good = v

    def _smoothed(self):
        w = list(self._good or ())
        if not w:
            return self._last_good
        if self.numeric:
            s = sorted(w)
            med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
            return float(round(med)) if self.parse_as == "int" else med
        return Counter(w).most_common(1)[0][0]      # mode for bool/text

    def _on_fail(self):
        empty = False if self.parse_as == "bool" else ("" if self.parse_as == "text" else 0.0)
        if self.on_fail == "zero":
            return empty, 0
        if self.on_fail == "hold" and self._last_good is not None:
            return self._last_good, 0
        nan = float("nan") if self.numeric else empty
        return nan, 1
