#!/usr/bin/env python3

import json

from exploration_core import astar, load_map_yaml

grid = load_map_yaml("/home/test/project_0727_fixed_points_home_scenes/maps/navigation/current.yaml")
clearance = grid.clearance_m()
origin = grid.world_to_cell(0.0, 0.0)
goals = [
    ("left_vertical", -1.625, 0.883),
    ("top_horizontal", -0.475, 2.233),
    ("right_vertical", 0.375, 3.183),
    ("inner_horizontal", 0.225, 3.833),
]
for name, x, y in goals:
    path = astar(grid, origin, grid.world_to_cell(x, y), 0.30)
    values = [float(clearance[cell]) for cell in path or []]
    print(json.dumps({
        "name": name,
        "path_length_m": round(max(0, len(values) - 1) * grid.resolution, 3),
        "minimum_clearance_m": round(min(values), 3) if values else None,
        "cells_below_0_35": sum(value < 0.35 - 1e-6 for value in values),
        "cells_below_0_40": sum(value < 0.40 - 1e-6 for value in values),
        "cells_below_0_45": sum(value < 0.45 - 1e-6 for value in values),
        "cells": len(values),
    }, ensure_ascii=False))
