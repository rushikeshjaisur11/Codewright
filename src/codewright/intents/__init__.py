"""Intent detection: label code with the engineering intent it implements."""

from . import (  # noqa: F401  (register further detectors)
    adk,
    adk_more,
    architecture,
    conventions,
    libs,
    more,
    readiness,
)
from .detectors import DETECTORS, detect

__all__ = ["DETECTORS", "detect"]
