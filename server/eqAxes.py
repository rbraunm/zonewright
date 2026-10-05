"""Every conversion between zonewright's frames, units, and headings, without Blender.

Frames: Blender and the zone files (.zon, .ter, .mod) hold (x, y, z up); the server (.map collision, the zone row's safe point,
zone_points) holds (zone y, zone x, z); Recast and Detour (.nav) hold (zone y, zone z, zone x), an even permutation that keeps a
triangle's winding.

Headings: zonewright's headingDegrees is 0 toward +Y, clockwise seen from above. EQ's heading runs eqHeadingUnits to a turn; the
client's heading toward a point (eqgame.exe 0x4ef250) is 0 toward the server's +y and 128 toward its +x, which through the axis swap
is a counter-clockwise turn about Z of heading * 360 / eqHeadingUnits degrees for a model whose front is +X."""
eqHeadingUnits = 512


def serverFromZone(point):
  return [point[1], point[0], point[2]]


def zoneFromServer(point):
  return [point[1], point[0], point[2]]


def recastFromZone(point):
  return [point[1], point[2], point[0]]


def zoneFromRecast(point):
  return [point[2], point[0], point[1]]


def turnFromHeading(headingDegrees):
  """The counter-clockwise turn about Z (degrees) that faces a model whose front is +X along headingDegrees."""
  return 90 - headingDegrees


def turnFromEQHeading(eqHeading):
  return eqHeading * 360 / eqHeadingUnits


def eqHeadingFromHeading(headingDegrees):
  return ((90 - headingDegrees) * eqHeadingUnits / 360) % eqHeadingUnits


def eqHeadingFromTurn(counterclockwiseDegrees):
  return (counterclockwiseDegrees * eqHeadingUnits / 360) % eqHeadingUnits
