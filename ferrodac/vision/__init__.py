"""Computer-vision sources: read instruments optically (OCR) from a frame ROI.

A :class:`~ferrodac.vision.detector.Detector` is the platform's first *transform
node* — it consumes an image stream (from a display sink) and produces a scalar
source, turning "any readout a camera can see" into a routable value.
"""

from .detector import Detector
from .ocr import have_ocr, ocr_backend
from .runner import CVRunner

__all__ = ["Detector", "CVRunner", "have_ocr", "ocr_backend"]
