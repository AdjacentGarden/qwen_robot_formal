import base64

import cv2
import numpy as np
import pytest

from robot_bridge.bridge import RosAdapter
from robot_bridge.map_codec import occupancy_to_png


def test_occupancy_map_encoding_and_vertical_orientation():
    encoded, stats = occupancy_to_png([0, 100, -1, 0], 2, 2)
    image = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image.tolist() == [[205, 254], [254, 0]]
    assert stats == {"width": 2, "height": 2, "occupied_cells": 1, "free_cells": 2, "unknown_cells": 1}


def test_empty_or_malformed_map_is_rejected_before_opencv():
    with pytest.raises(ValueError, match="invalid_map_dimensions"):
        occupancy_to_png([], 0, 0)
    with pytest.raises(ValueError, match="invalid_map_cell_count"):
        occupancy_to_png([0], 2, 2)


def test_transient_empty_ros_map_does_not_kill_callback_thread():
    class Item:
        pass

    message = Item()
    message.info = Item()
    message.info.width = 0
    message.info.height = 0
    message.info.resolution = 0.05
    message.header = Item()
    message.header.stamp = Item()
    message.header.stamp.sec = 1
    message.header.stamp.nanosec = 2
    message.data = []

    adapter = RosAdapter.__new__(RosAdapter)
    adapter.map_signature = None
    adapter.map_message = None
    adapter._map_callback(message)
    assert adapter.map_message is None
    assert adapter.map_signature == (0, 0, 0.05, 1, 2)
