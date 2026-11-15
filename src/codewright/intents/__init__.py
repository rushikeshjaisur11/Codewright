"""Intent detection: label code with the engineering intent it implements."""

from . import adk, adk_more, architecture, conventions, libs, more  # noqa: F401  (register further detectors)
from .detectors import DETECTORS, detect

__all__ = ["DETECTORS", "detect"]
