import struct
import sys
from pathlib import Path

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqgTerrain
import eqZones


def flatTerrain(tiles, quads=2, spacing=10.0):
  return {"tileSize": quads * spacing, "header": {"quadsPerTile": quads, "unitsPerVertex": spacing}, "tiles": tiles}


def tile(x, y, heights):
  return {"x": x, "y": y, "heights": numpy.array(heights, dtype=numpy.float32), "quadFlags": numpy.zeros((2, 2), dtype=numpy.uint8)}


def testHoleQuadsDrawNoTriangles():
  # Rows run along y: quad (0, 0) and quad (1, 1) are holes (bit 1, alone or with bit 4); quad (0, 1) is kind 1 and quad (1, 0) kind 0
  # with its diagonal crossed.
  flags = numpy.array([[0x01, 0x04], [0x82, 0x05]], dtype=numpy.uint8)
  triangles = eqgTerrain.tileTriangles(flags)
  assert triangles.tolist() == [[1, 2, 5], [3, 4, 6], [1, 5, 4], [4, 7, 6]]


class Archive:
  """A zone archive's entries, as eqArchive.EQArchive lists and reads them."""

  def __init__(self, files):
    self.entries = files

  def read(self, name):
    return self.entries[name]


def litFile(count, colors):
  return struct.pack("<I", count) + struct.pack(f"<{len(colors)}I", *colors)


def testPlacementGroundComesFromTheTileThatListsIt():
  # The listing tile rises 1 a unit along x; the tile east of it stands at 100. An offset past the listing tile's edge reads the listing
  # tile's own ground where it wraps to, not the ground under the placement; one landing exactly on the far edge reads the query's 0.
  rising = [[0, 10, 20], [0, 10, 20], [0, 10, 20]]
  listing, east = tile(0.0, 0.0, rising), tile(20.0, 0.0, [[100] * 3] * 3)
  terrain = flatTerrain([listing, east])
  tilesByOrigin = {(0.0, 0.0): listing, (20.0, 0.0): east}
  placement = {"position": (25.0, 5.0, 1.0), "offset": (25.0, 5.0, 1.0), "listingTile": (0.0, 0.0)}
  assert eqgTerrain.placedPosition(terrain, tilesByOrigin, placement).tolist() == [25.0, 5.0, 6.0]
  behind = placement | {"position": (-15.0, 5.0, 0.0), "offset": (-15.0, 5.0, 0.0)}
  assert eqgTerrain.placedPosition(terrain, tilesByOrigin, behind).tolist() == [-15.0, 5.0, 5.0]
  edge = placement | {"position": (-20.0, 5.0, 2.0), "offset": (-20.0, 5.0, 2.0)}
  assert eqgTerrain.placedPosition(terrain, tilesByOrigin, edge).tolist() == [-20.0, 5.0, 2.0]


def testWrappingMatchesTheOffsetsEQEmuMeasuredInTheClient():
  # azone2's dat.cpp records the offsets the client was found to read placements' ground at by hand-editing zone files (tile sides 160).
  for offset, wrapped in ((-14.0, 146.0), (219.0, 59.0), (396.406, 76.406), (-193.584, 126.416), (166.512, 6.512), (-4.658, 155.342)):
    assert abs(eqgTerrain.wrappedIntoTile(offset, 160.0) - wrapped) < 1e-9


def testGroupMembersStandFromTheGroupsWorldHeight():
  group = {"position": (100.0, 200.0, 50.0), "rotationDegrees": (0.0, 0.0, 90.0), "scale": (2.0, 2.0, 2.0), "memberLift": 3.0}
  member = {"position": (10.0, 0.0, 1.0), "rotationDegrees": (0.0, 0.0, 10.0), "scale": 1.5}
  transform, position = eqZones.groupMemberTransform(group, member)
  # The offset turns a quarter turn and doubles, and the lift doubles; nothing reads the ground.
  assert numpy.allclose(position, (100.0, 220.0, 58.0))
  assert numpy.allclose(transform, eqgTerrain.placementMatrix((0.0, 0.0, 100.0), (3.0, 3.0, 3.0)))
  # Tilted groups add their turns to the member's, axis by axis, rather than composing the two rotations.
  tilted = group | {"rotationDegrees": (30.0, 0.0, 90.0)}
  leaning = member | {"rotationDegrees": (0.0, 20.0, 0.0)}
  transform, _ = eqZones.groupMemberTransform(tilted, leaning)
  assert numpy.allclose(transform, eqgTerrain.placementMatrix((30.0, 20.0, 90.0), (3.0, 3.0, 3.0)))
  assert not numpy.allclose(transform, eqgTerrain.placementMatrix((30.0, 0.0, 90.0), (2.0, 2.0, 2.0)) @ eqgTerrain.placementMatrix((0.0, 20.0, 0.0), (1.5, 1.5, 1.5)))


def testMemberBakedLightIsTakenOnlyAsTheClientTakesIt():
  archive = Archive({
    "fits.lit": litFile(2, [0xFF102030, 0x80405060]),
    "long.lit": litFile(2, [0xFF102030, 0x80405060, 0x01020304]),
    "miscounted.lit": litFile(3, [0, 0, 0]),
    "short.lit": litFile(2, [0xFF102030]),
  })
  colors, problem = eqZones.memberBakedLight(archive, "fits.lit", 2)
  assert problem is None and colors.tolist() == [[0x10, 0x20, 0x30, 0xFF], [0x40, 0x50, 0x60, 0x80]]
  colors, problem = eqZones.memberBakedLight(archive, "long.lit", 2)
  assert problem is None and colors.tolist() == [[0x10, 0x20, 0x30, 0xFF], [0x40, 0x50, 0x60, 0x80]]
  assert eqZones.memberBakedLight(archive, "miscounted.lit", 2) == (None, "notFitting")
  assert eqZones.memberBakedLight(archive, "short.lit", 2) == (None, "short")
  assert eqZones.memberBakedLight(archive, "absent.lit", 2) == (None, "missing")
