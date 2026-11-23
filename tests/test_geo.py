import math

import pytest
from pyproj import Transformer

from bbr_mcp import geo

TO_UTM32 = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)


@pytest.mark.parametrize(
    "lon, lat",
    [
        (12.5530, 55.6987),  # København
        (8.0, 54.6),  # Tønder area, west of the central meridian
        (10.2039, 56.1629),  # Aarhus
        (15.15, 55.1),  # Bornholm, far east of the zone
        (9.0, 57.75),  # Skagen latitude
    ],
)
def test_projection_matches_pyproj(lon, lat):
    x, y = geo.wgs84_to_utm32(lon, lat)
    ex, ey = TO_UTM32.transform(lon, lat)
    assert math.hypot(x - ex, y - ey) < 0.001


def test_bbox_in_wgs84_becomes_a_utm32_polygon():
    wkt, km2 = geo.bbox([12.55, 55.69, 12.56, 55.70], "4326")
    assert wkt.startswith("POLYGON((7") and wkt.count(",") == 4
    assert 0.5 < km2 < 0.8


def test_circle_area():
    _, km2 = geo.circle(723410, 6178373, 1000)
    assert km2 == pytest.approx(math.pi, rel=0.01)


def test_polygon_is_read_and_closed():
    wkt, km2 = geo.polygon("POLYGON((723000 6178000, 724000 6178000, 724000 6179000, 723000 6178000))", "25832")
    assert km2 == pytest.approx(0.5)
    assert wkt.endswith("723000.00 6178000.00))")


@pytest.mark.parametrize(
    "call, message",
    [
        (lambda: geo.bbox([700000, 6100000, 720000, 6120000], "25832"), "km²"),
        (lambda: geo.bbox([12.56, 55.69, 12.55, 55.70], "4326"), "min must be below"),
        (lambda: geo.bbox([55.69, 12.55, 55.70, 12.56], "25832"), "outside Denmark"),
        (lambda: geo.bbox([723000, 6178000, 724000, 6179000], "4326"), "longitude/latitude"),
        (lambda: geo.polygon("MULTIPOLYGON(((1 2, 3 4, 5 6)))", "25832"), "single POLYGON"),
        (lambda: geo.polygon("POLYGON((723000 6178000, 724000 6178000))", "25832"), "three distinct"),
        (lambda: geo.circle(723410, 6178373, 5000), "radius_m"),
        (lambda: geo.to_utm32([(1, 2)], "3857"), "crs must be"),
    ],
)
def test_bad_areas_are_refused(call, message):
    with pytest.raises(geo.AreaError, match=message):
        call()
