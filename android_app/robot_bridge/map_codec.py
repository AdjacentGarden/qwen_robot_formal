from __future__ import annotations

import base64
from typing import Any, Dict, Iterable, Tuple

import cv2
import numpy as np


def occupancy_to_png(data: Iterable[int], width: int, height: int) -> Tuple[str, Dict[str, Any]]:
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise ValueError("invalid_map_dimensions")
    flat = np.asarray(list(data), dtype=np.int16)
    expected = width * height
    if flat.size != expected:
        raise ValueError(f"invalid_map_cell_count:{flat.size}!={expected}")
    values = flat.reshape((height, width))
    image = np.full((height, width), 205, dtype=np.uint8)
    image[values == 0] = 254
    image[values >= 50] = 0
    image = np.flipud(image)
    ok, encoded = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 6])
    if not ok:
        raise RuntimeError("map_png_encode_failed")
    return base64.b64encode(encoded.tobytes()).decode("ascii"), {
        "width": width,
        "height": height,
        "occupied_cells": int(np.count_nonzero(values >= 50)),
        "free_cells": int(np.count_nonzero(values == 0)),
        "unknown_cells": int(np.count_nonzero(values < 0)),
    }
