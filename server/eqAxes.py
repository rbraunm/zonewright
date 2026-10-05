"""Every conversion between zonewright's frames, units, and headings, without Blender.

Frames: Blender and the zone files (.zon, .ter, .mod) hold (x, y, z up); the server (.map collision, the zone row's safe point,
zone_points) holds (zone y, zone x, z); Recast and Detour (.nav) hold (zone y, zone z, zone x), an even permutation that keeps a
triangle's winding.

Headings: zonewright's headingDegrees is 0 toward +Y, clockwise seen from above. EQ's heading runs eqHeadingUnits to a turn; the
client's heading toward a point (eqgame.exe 0x4ef250) is 0 toward the server's +y and 128 toward its +x, which through the axis swap
is a counter-clockwise turn about Z of heading * 360 / eqHeadingUnits degrees for a model whose front is +X.

Each frame converter takes a point or any array of points (xyz on the last axis) and returns a numpy array of the same shape and dtype."""
import numpy

eqHeadingUnits = 512


def serverFromZone(points):
  return numpy.asarray(points)[..., [1, 0, 2]]


def zoneFromServer(points):
  return numpy.asarray(points)[..., [1, 0, 2]]


def recastFromZone(points):
  return numpy.asarray(points)[..., [1, 2, 0]]


def zoneFromRecast(points):
  return numpy.asarray(points)[..., [2, 0, 1]]


def recastFromServer(points):
  return numpy.asarray(points)[..., [0, 2, 1]]


def turnFromHeading(headingDegrees):
  """The counter-clockwise turn about Z (degrees) that faces a model whose front is +X along headingDegrees."""
  return 90 - headingDegrees


def turnFromEQHeading(eqHeading):
  return eqHeading * 360 / eqHeadingUnits


def eqHeadingFromHeading(headingDegrees):
  return ((90 - headingDegrees) * eqHeadingUnits / 360) % eqHeadingUnits


def eqHeadingFromTurn(counterclockwiseDegrees):
  return (counterclockwiseDegrees * eqHeadingUnits / 360) % eqHeadingUnits
