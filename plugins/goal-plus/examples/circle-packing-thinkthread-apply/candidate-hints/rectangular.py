"""Deterministic 5-by-6 rectangular-lattice candidate for 26 circles."""


def run_packing():
    radius = 1.0 / 12.0 - 1e-7
    centers = [
        ((column + 0.5) / 5.0, (row + 0.5) / 6.0)
        for row in range(6)
        for column in range(5)
    ][:26]
    radii = [radius] * len(centers)
    return centers, radii, sum(radii)
