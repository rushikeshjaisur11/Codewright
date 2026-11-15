"""Intent detection: label code with the engineering intent it implements."""

from . import more  # noqa: F401  (registers further detectors)
from .detectors import DETECTORS, detect

__all__ = ["DETECTORS", "detect"]
