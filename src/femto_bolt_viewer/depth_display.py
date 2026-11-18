"""Fixed metric colors for depth previews; measurement arrays stay untouched."""

from __future__ import annotations

import math

import numpy as np

DEFAULT_DEPTH_RANGE_MM = (150.0, 800.0)


def colorize_depth_mm(values: np.ndarray, minimum_mm: float = DEFAULT_DEPTH_RANGE_MM[0],
                      maximum_mm: float = DEFAULT_DEPTH_RANGE_MM[1]) -> np.ndarray:
    """Render positive finite distances with TURBO and black missing samples.

    Values outside the display interval use its endpoint colors. A distant
    background never changes the color assigned to a foreground distance.
    This display interval is independent of the sensor's measurement range.
    """
    import cv2

    if not (math.isfinite(minimum_mm) and math.isfinite(maximum_mm)
            and 0 <= minimum_mm < maximum_mm):
        raise ValueError("Depth display bounds must be finite, nonnegative and increasing")
    values = np.asarray(values, dtype=np.float64)
    if values.ndim != 2 or values.size == 0:
        raise ValueError("Depth display requires a nonempty two-dimensional raster")
    valid = np.isfinite(values) & (values > 0)
    safe = np.where(valid, values, minimum_mm)
    scaled = np.uint8(np.clip((safe - minimum_mm) / (maximum_mm - minimum_mm), 0, 1) * 255)
    colored = cv2.applyColorMap(scaled, cv2.COLORMAP_TURBO)
    colored[~valid] = 0
    return colored
