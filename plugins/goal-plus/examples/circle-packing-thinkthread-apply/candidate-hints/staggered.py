"""Deterministic staggered-lattice candidate for 26 circles."""

import math


def run_packing():
    radius = 1.0 / (2.0 + 5.0 * math.sqrt(3.0)) - 1e-7
    vertical_spacing = math.sqrt(3.0) * radius
    centers = []
    for row in range(6):
        columns = range(5) if row % 2 == 0 else range(4)
        offset = 0.1 if row % 2 == 0 else 0.2
        for column in columns:
            centers.append((offset + 0.2 * column, radius + row * vertical_spacing))
    centers = centers[:26]
    radii = [radius] * len(centers)
    return centers, radii, sum(radii)
