"""OCR backends + frame helpers.

The recogniser is **pluggable** — a detector picks an engine by key:

  - ``general``   — a DNN OCR (PP-OCR via ONNX Runtime / RapidOCR). Far more
                    robust across printed / LCD / seven-segment / scene text;
                    works on the raw colour crop. The default when available.
  - ``tesseract`` — fast and light, great on clean printed digits with a
                    character whitelist; weak on segmented / styled displays.

Each engine takes the cropped ROI and the detector (for its preprocessing /
whitelist prefs) and returns ``(text, debug_image)``.
"""

from __future__ import annotations

import shutil
import subprocess

import numpy as np

from .. import _qtbinding  # noqa: F401  selects QT_API before qtpy import
from qtpy.QtGui import QImage

try:
    import cv2
    _HAVE_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    _HAVE_CV2 = False

_TESSERACT = shutil.which("tesseract")


def have_ocr() -> bool:
    return _HAVE_CV2 and bool(available_engines())


# --------------------------------------------------------------------------- #
#  Frame conversion
# --------------------------------------------------------------------------- #
def qimage_to_rgb(qimg: QImage) -> np.ndarray:
    """QImage -> contiguous (H, W, 3) uint8 RGB array."""
    img = qimg.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = bytes(img.constBits())
    arr = np.frombuffer(buf, np.uint8).reshape((h, bpl))
    return np.ascontiguousarray(arr[:, : w * 3].reshape((h, w, 3)))


# --------------------------------------------------------------------------- #
#  Preprocess + recognise
# --------------------------------------------------------------------------- #
def preprocess(rgb: np.ndarray, invert: bool = False, threshold: bool = False,
               scale: int = 3, adaptive: bool = False, denoise: bool = False,
               rotate: float = 0.0, thresh_value: int = 0) -> np.ndarray:
    """Grayscale → deskew → upscale → denoise → invert → binarize.

    Binarization (when ``threshold``): adaptive Gaussian, manual (``thresh_value``
    > 0), or Otsu (default). Built general for mixed displays.
    """
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    if rotate:
        h, w = gray.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), rotate, 1.0)
        gray = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    if scale and scale != 1:
        gray = cv2.resize(gray, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)
    if denoise:
        gray = cv2.medianBlur(gray, 3)
    if invert:
        gray = cv2.bitwise_not(gray)
    if threshold:
        if adaptive:
            gray = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                         cv2.THRESH_BINARY, 31, 10)
        elif thresh_value and thresh_value > 0:
            gray = cv2.threshold(gray, thresh_value, 255, cv2.THRESH_BINARY)[1]
        else:
            gray = cv2.threshold(gray, 0, 255,
                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return gray


def recognize(gray: np.ndarray, whitelist: str = "", psm: int = 7) -> str:
    """Run Tesseract on a (preprocessed) single-channel image, return raw text."""
    if _TESSERACT is None or not _HAVE_CV2:
        return ""
    ok, png = cv2.imencode(".png", gray)
    if not ok:
        return ""
    cmd = [_TESSERACT, "stdin", "stdout", "--psm", str(psm), "-l", "eng"]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    try:
        out = subprocess.run(cmd, input=png.tobytes(),
                             capture_output=True, timeout=5)
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _whitelist_filter(text: str, whitelist: str) -> str:
    if not whitelist:
        return text.strip()
    allowed = set(whitelist) | {" "}
    return "".join(c for c in text if c in allowed).strip()


def _rotate(gray, deg):
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)


# --------------------------------------------------------------------------- #
#  Engines
# --------------------------------------------------------------------------- #
class _Engine:
    key = ""
    label = ""

    def available(self) -> bool:
        return False

    def read(self, rgb, det):                 # -> (text, debug_gray)
        return "", None


class TesseractEngine(_Engine):
    key = "tesseract"
    label = "Tesseract — fast, clean printed digits"

    def available(self) -> bool:
        return _TESSERACT is not None and _HAVE_CV2

    def read(self, rgb, det):
        gray = preprocess(rgb, det.invert, det.threshold, det.scale, det.adaptive,
                          det.denoise, det.rotate, det.thresh_value)
        return recognize(gray, det.whitelist, det.psm), gray


class GeneralEngine(_Engine):
    """PP-OCR (RapidOCR / ONNX Runtime) — a DNN that reads the *raw colour* crop."""

    key = "general"
    label = "General — DNN (PP-OCR), most robust"
    _ocr = None
    _ok = None

    def available(self) -> bool:
        if GeneralEngine._ok is None:
            try:
                import rapidocr_onnxruntime  # noqa: F401
                GeneralEngine._ok = _HAVE_CV2
            except Exception:
                GeneralEngine._ok = False
        return bool(GeneralEngine._ok)

    def _engine(self):
        if GeneralEngine._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            GeneralEngine._ocr = RapidOCR()
        return GeneralEngine._ocr

    def read(self, rgb, det):
        img = rgb
        if det.rotate:
            img = _rotate(img, det.rotate)
        h = img.shape[0]
        if h < 96:                              # DNN likes a reasonable height
            f = 96.0 / max(h, 1)
            img = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        try:
            res, _ = self._engine()(cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        except Exception:
            res = None
        text = " ".join(t for _b, t, _s in res) if res else ""
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        return _whitelist_filter(text, det.whitelist), gray


_ENGINES = [GeneralEngine(), TesseractEngine()]
_BY_KEY = {e.key: e for e in _ENGINES}


def available_engines() -> list:
    """[(key, label)] for every engine usable in this environment (best first)."""
    return [(e.key, e.label) for e in _ENGINES if e.available()]


def get_engine(key: str) -> _Engine:
    eng = _BY_KEY.get(key)
    if eng is not None and eng.available():
        return eng
    fallback = available_engines()
    return _BY_KEY[fallback[0][0]] if fallback else _BY_KEY["tesseract"]


def default_engine() -> str:
    eng = available_engines()
    return eng[0][0] if eng else "tesseract"


def ocr_backend() -> str:
    """Human-readable summary of available engines (for the config dialog hint)."""
    eng = available_engines()
    return ", ".join(label.split(" —")[0] for _k, label in eng) if eng else "none"
