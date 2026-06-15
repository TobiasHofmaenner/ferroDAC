"""OCR backend + frame helpers.

Tesseract (via its CLI, reading PNG from stdin) is the default engine: apt-
installable, cross-platform, and solid on printed/LCD digits with a character
whitelist. It is deliberately isolated here so a seven-segment recognizer
(ssocr) or a DNN model can be slotted in later without touching the detector.
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
    return _TESSERACT is not None and _HAVE_CV2


def ocr_backend() -> str:
    if not _HAVE_CV2:
        return "unavailable (OpenCV missing)"
    if _TESSERACT is None:
        return "unavailable (tesseract missing)"
    return "tesseract"


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
