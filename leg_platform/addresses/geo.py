"""Coordinates of the map: LV95 (EPSG:2056) to WGS84 degrees for the swisstopo base map (D-123).

The approximate formulas of the Federal Office of Topography swisstopo, "Näherungsformeln für die Transformation zwischen
Schweizer Projektionskoordinaten und WGS84", Version Dezember 2016, section 2 (accuracy 0.1 arc second in longitude
and 0.08 in latitude, better than 1 metre on the map). The formulas are for navigation, not for surveying; the map needs
no more. Checked against the example of the document (test ``GeoTests``).
"""

# Formulas of section 2, step 1: the coordinates relative to Bern, in units of 1000 km.
BERN_EASTING = 2_600_000
BERN_NORTHING = 1_200_000
UNIT = 1_000_000


def lv95_to_wgs84(easting, northing):
    """Longitude and latitude in degrees (WGS84) of an LV95 position; the input is in metres."""
    y = (float(easting) - BERN_EASTING) / UNIT
    x = (float(northing) - BERN_NORTHING) / UNIT
    longitude_units = (
        2.6779094 + 4.728982 * y + 0.791484 * y * x + 0.1306 * y * x**2 - 0.0436 * y**3
    )
    latitude_units = (
        16.9023892 + 3.238272 * x - 0.270978 * y**2 - 0.002528 * x**2 - 0.0447 * y**2 * x - 0.0140 * x**3
    )
    # Step 3: the units are 10000 arc seconds; 100 / 36 turns them into degrees.
    return longitude_units * 100 / 36, latitude_units * 100 / 36
