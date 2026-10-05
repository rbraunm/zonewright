import sys
from pathlib import Path

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqAxes
import playerScale


def testEQHeadingInvertsPlacementFrame():
  headings = [45.0 * index for index in range(8)]
  for heading in headings:
    eqHeading = eqAxes.eqHeadingFromHeading(heading)
    assert 0 <= eqHeading < eqAxes.eqHeadingUnits
    assert abs((eqAxes.turnFromEQHeading(eqHeading) - eqAxes.turnFromHeading(heading) + 180) % 360 - 180) < 1e-9
    assert abs(eqAxes.eqHeadingFromTurn(eqAxes.turnFromHeading(heading)) - eqHeading) < 1e-9
  # zonewright's 0 looks along zone +Y, the server's +x, which the client's heading toward a point calls 128; zone +X (server +y) is 0.
  assert [eqAxes.eqHeadingFromHeading(heading) for heading in headings] == [128.0, 64.0, 0.0, 448.0, 384.0, 320.0, 256.0, 192.0]


def testAxesRoundTripAndRecastKeepsWinding():
  # Highpass Hold's safe point, zone (-148, -219, -24), is the server's (-219, -148, -24).
  assert eqAxes.serverFromZone([-148, -219, -24]) == [-219, -148, -24]
  assert eqAxes.recastFromZone([-148, -219, -24]) == [-219, -24, -148]
  triangle = [[1.5, -2.0, 0.25], [7.0, 3.0, -1.0], [-4.0, 6.5, 2.0]]
  for corner in triangle:
    assert eqAxes.zoneFromServer(eqAxes.serverFromZone(corner)) == corner
    assert eqAxes.zoneFromRecast(eqAxes.recastFromZone(corner)) == corner
  zoneCorners = numpy.array(triangle)
  zoneNormal = numpy.cross(zoneCorners[1] - zoneCorners[0], zoneCorners[2] - zoneCorners[0])
  recastCorners = numpy.array([eqAxes.recastFromZone(corner) for corner in triangle])
  serverCorners = numpy.array([eqAxes.serverFromZone(corner) for corner in triangle])
  assert numpy.allclose(numpy.cross(recastCorners[1] - recastCorners[0], recastCorners[2] - recastCorners[0]), eqAxes.recastFromZone(zoneNormal))
  # The server's swap is odd: the same corners in order face the other way there.
  assert numpy.allclose(numpy.cross(serverCorners[1] - serverCorners[0], serverCorners[2] - serverCorners[0]), -numpy.array(eqAxes.serverFromZone(zoneNormal)))


def testPlayerScaleNamesASourceForEveryConstant():
  constants = {name for name, value in vars(playerScale).items() if not name.startswith("_") and isinstance(value, float)}
  assert {"playerHeight", "walkableNormalZ", "stepHeight", "eyeHeight", "swimEyeAboveSurface"} <= constants
  assert set(playerScale.sources) == constants
  for name, source in playerScale.sources.items():
    assert source.startswith(("measured: ", "unmeasured: ")), name
  # A measured limit's source states the value taken from it.
  assert f"(normal z {playerScale.walkableNormalZ:g})" in playerScale.sources["walkableNormalZ"]
  assert f"to {playerScale.stepHeight:.2f}" in playerScale.sources["stepHeight"]
