"""Areas for BBR's spatial filter, which takes EPSG:25832 (ETRS89 / UTM 32N) only.

WGS84 longitude/latitude (EPSG:4326, what most data and maps use) is converted with the transverse Mercator series of
Krüger as given by Karney (2011), accurate to well under a millimetre in Denmark. ETRS89 and WGS84 differ by less
than a metre, which is below what a building lookup needs.
"""

import math
import re

# GRS80 (ETRS89), UTM zone 32: central meridian 9° E, scale 0.9996, false easting 500 km.
A, F = 6378137.0, 1 / 298.257222101
K0, LON0, FALSE_EASTING = 0.9996, math.radians(9.0), 500000.0
N = F / (2 - F)
A_HAT = A / (1 + N) * (1 + N**2 / 4 + N**4 / 64)
ALPHA = (
    N / 2 - 2 * N**2 / 3 + 5 * N**3 / 16,
    13 * N**2 / 48 - 3 * N**3 / 5,
    61 * N**3 / 240,
)
E = math.sqrt(F * (2 - F))

# Denmark with a margin, in EPSG:25832: a coordinate outside is a mistake (e.g. lon/lat given as 25832).
DK_BOUNDS = (380_000.0, 6_000_000.0, 920_000.0, 6_430_000.0)
MAX_AREA_KM2 = 25.0

NUMBER = r"-?\d+(?:\.\d+)?"
POLYGON = re.compile(r"^\s*POLYGON\s*\(\s*\((.*)\)\s*\)\s*$", re.I | re.S)


class AreaError(ValueError):
    pass


def wgs84_to_utm32(lon: float, lat: float) -> tuple[float, float]:
    phi, lam = math.radians(lat), math.radians(lon) - LON0
    tau = math.tan(phi)
    sigma = math.sinh(E * math.atanh(E * tau / math.sqrt(1 + tau**2)))
    tau_p = tau * math.sqrt(1 + sigma**2) - sigma * math.sqrt(1 + tau**2)
    xi_p = math.atan2(tau_p, math.cos(lam))
    eta_p = math.asinh(math.sin(lam) / math.sqrt(tau_p**2 + math.cos(lam) ** 2))
    xi = xi_p + sum(a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p) for j, a in enumerate(ALPHA, 1))
    eta = eta_p + sum(a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p) for j, a in enumerate(ALPHA, 1))
    return FALSE_EASTING + K0 * A_HAT * eta, K0 * A_HAT * xi


def to_utm32(points: list[tuple[float, float]], crs: str) -> list[tuple[float, float]]:
    if crs == "4326":
        for lon, lat in points:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise AreaError(f"({lon}, {lat}) is not a longitude/latitude pair (EPSG:4326 is lon, lat in degrees)")
        points = [wgs84_to_utm32(lon, lat) for lon, lat in points]
    elif crs != "25832":
        raise AreaError("crs must be 25832 (ETRS89 / UTM 32N, metres) or 4326 (WGS84 longitude, latitude)")
    xmin, ymin, xmax, ymax = DK_BOUNDS
    for x, y in points:
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            raise AreaError(
                f"({x:.0f}, {y:.0f}) in EPSG:25832 is outside Denmark: check the crs and the order (x, y / lon, lat)"
            )
    return points


def ring_area_km2(ring: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True))) / 2e6


def wkt(ring: list[tuple[float, float]]) -> str:
    closed = ring if ring[0] == ring[-1] else [*ring, ring[0]]
    return "POLYGON((" + ",".join(f"{x:.2f} {y:.2f}" for x, y in closed) + "))"


def checked(ring: list[tuple[float, float]]) -> tuple[str, float]:
    """The ring as WKT for BBR and its area, refusing an area too large to list building by building."""
    if len(set(ring)) < 3:
        raise AreaError("an area needs at least three distinct corners")
    area = ring_area_km2(ring)
    if area > MAX_AREA_KM2:
        raise AreaError(f"the area is {area:.1f} km²; at most {MAX_AREA_KM2:.0f} km² per call: split it or narrow it")
    return wkt(ring), area


def bbox(values: list[float], crs: str) -> tuple[str, float]:
    if len(values) != 4:
        raise AreaError("bbox is [min_x, min_y, max_x, max_y] (or [min_lon, min_lat, max_lon, max_lat] with crs 4326)")
    x1, y1, x2, y2 = values
    if x1 >= x2 or y1 >= y2:
        raise AreaError("bbox is [min_x, min_y, max_x, max_y]: each min must be below its max")
    corners = to_utm32([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], crs)
    return checked(corners)


def polygon(text: str, crs: str) -> tuple[str, float]:
    match = POLYGON.match(text)
    if not match or "(" in match.group(1):
        raise AreaError("polygon_wkt must be a single POLYGON((x y, x y, ...)) without holes")
    points = []
    for pair in match.group(1).split(","):
        numbers = re.findall(NUMBER, pair)
        if len(numbers) != 2:
            raise AreaError(f"cannot read the corner {pair.strip()!r} of polygon_wkt")
        points.append((float(numbers[0]), float(numbers[1])))
    ring = to_utm32(points, crs)
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    return checked(ring)


def circle(x: float, y: float, radius_m: float, crs: str = "25832") -> tuple[str, float]:
    if not 0 < radius_m <= 2500:
        raise AreaError("radius_m must be above 0 and at most 2500")
    ((cx, cy),) = to_utm32([(x, y)], crs)
    ring = [
        (cx + radius_m * math.cos(2 * math.pi * i / 32), cy + radius_m * math.sin(2 * math.pi * i / 32))
        for i in range(32)
    ]
    return checked(ring)
