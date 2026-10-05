import collections
import struct
import sys
from pathlib import Path

import numpy
import pytest

import peridotServerFiles as peridot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import machineProfile
import navDrawing
import recastHelper
import serverNav

pytestmark = pytest.mark.clientData("serverNav")
# Peridot's zone row gives the safe point (-219, -148, -24) in server axes.
highpassSafePoint = (-148.0, -219.0, -24.0)
# thulehouse2.navprj, the map_edit project Peridot's thulehouse2.nav was built from, lowers its bounds' top by hand to 255.2.
thulehouseProjectCeiling = numpy.float32(255.2)
detailCounts = ("detailVertCount", "detailTriCount")


def noProgress(done, of, message):
  pass


def peridotInputs(zone):
  return peridot.mapCollision(peridot.referenceBytes(f"base/{zone}.map")), peridot.waterRecords(peridot.referenceBytes(f"water/{zone}.wtr"))


def assertTileForTile(ours, report, peridotFile, tileCount, areas, differingDetails):
  """Peridot's tiles matched by their headers' (x, y, layer), never by reference or file order, which record the order map_edit's
  threads finished in."""
  theirs, mine = peridot.navTiles(peridotFile), peridot.navTiles(ours)
  assert mine["params"] == theirs["params"]
  assert sorted(mine["tiles"]) == sorted(theirs["tiles"])
  assert len(mine["tiles"]) == tileCount
  differing = 0
  for key, theirTile in theirs["tiles"].items():
    ourTile = mine["tiles"][key]
    assert {name: value for name, value in ourTile["header"].items() if name not in detailCounts} == {name: value for name, value in theirTile["header"].items() if name not in detailCounts}
    assert ourTile["sections"]["verts"] == theirTile["sections"]["verts"]
    # firstLink indexes the tile's link pool, which fills in the order tiles are added; the links are compared as sets below.
    ourPolygons, theirPolygons = ourTile["polys"].copy(), theirTile["polys"].copy()
    ourPolygons["firstLink"] = theirPolygons["firstLink"] = 0
    assert ourPolygons.tobytes() == theirPolygons.tobytes()
    differing += sum(peridot.detailOf(ourTile, index) != peridot.detailOf(theirTile, index) for index in range(len(ourTile["polys"])))
  assert peridot.linkSets(mine) == peridot.linkSets(theirs)
  assert collections.Counter(int(polygon["areaAndType"] & 0x3F) for tile in mine["tiles"].values() for polygon in tile["polys"]) == areas
  assert report["mostChunksPerTile"] < 512
  # The pinned Recast (EQEmu 710dabe) carries upstream 13dc549, "Improve triangulateHull", which the 2017 copy that built Peridot's navs
  # lacked: polygons identical, detail triangulation not.
  assert differing == differingDetails
  ordered = sorted(mine["tiles"], key=lambda key: (key[1], key[0]))
  polygonBits = 22 - (struct.unpack_from("<i", mine["params"], 20)[0].bit_length() - 1)
  assert mine["order"] == ordered
  assert [mine["tiles"][key]["reference"] for key in ordered] == [(1 << 22) | (index << polygonBits) for index in range(tileCount)]


def testNavReproducesHighpassHoldTileForTile(recastToolingRoot):
  collision, water = peridotInputs("highpasshold")
  ours, report = serverNav.navFromCollision(collision, water, recastToolingRoot, noProgress)
  assertTileForTile(ours, report, peridot.referenceBytes("nav/highpasshold.nav"), 28, {0: 7692, 1: 452, 11: 53}, 4709)
  assert (report["tilesWide"], report["tilesHigh"], report["tileBits"], report["polygonBits"]) == (5, 9, 6, 16)
  assert serverNav.inspectNav(ours, None, [], recastToolingRoot, noProgress)["polygons"] == 8197


def testNavReproducesThulehouse2TileForTile(recastToolingRoot):
  collision, water = peridotInputs("thulehouse2")
  # Peridot's nav was built with its project's bounds, whose top (255.2) lies below the collision's (270.25), so map_edit dropped the two
  # triangles reaching above it, which also set the collision's x and y extents. Exports always take the collidable extents
  # (navFromCollision); to compare, the helper is given the project's bounds and the triangles map_edit kept.
  low, high = serverNav.navBounds(collision)
  high[1] = float(thulehouseProjectCeiling)
  kept = collision[(collision[..., 2] <= thulehouseProjectCeiling).all(axis=1)]
  assert len(collision) - len(kept) == 2
  inputBytes = recastHelper.navInput(
    serverNav.recastFromServer(kept), (low, high), serverNav.navVolumes(water), serverNav.serverNavSettings, machineProfile.workerCount(),
  )
  payload, report = recastHelper.runHelper(recastToolingRoot, "nav", inputBytes, noProgress)
  ours = serverNav.navContainer(payload)
  assertTileForTile(ours, report, peridot.referenceBytes("nav/thulehouse2.nav"), 22, {0: 3440, 1: 6, 11: 114}, 1865)
  assert serverNav.inspectNav(ours, None, [], recastToolingRoot, noProgress)["polygons"] == 3560


def testNavIsDeterministic(recastToolingRoot):
  collision, water = peridotInputs("highpasshold")
  progress = []
  first, report = serverNav.navFromCollision(collision, water, recastToolingRoot, lambda done, of, message: progress.append((done, of, message)))
  second, _ = serverNav.navFromCollision(collision, water, recastToolingRoot, noProgress)
  assert first == second
  assert report["threads"] == min(machineProfile.workerCount(), 45) > 1
  assert progress[0] == (0, 1, "partitioning collidable triangles")
  assert progress[-1] == (45, 45, "building nav tiles")


# TODO: testNavFromOurHighpassMapMatchesPeridots needs S1's serverMapFiles.mapBytes (our .map from the client's archive and loose .zon):
# the same params and tile set, identical polygons in every tile that holds no placed-model triangle (named here, at least one), and the
# count of tiles whose polygons differ recorded with its cause (the rotation floats) in docs/serverFiles.md.


def testIslandsAndProbeOnHighpassHold(recastToolingRoot, tmp_path):
  collision, water = peridotInputs("highpasshold")
  ours, _ = serverNav.navFromCollision(collision, water, recastToolingRoot, noProgress)
  targets = [{"name": f"the zone line of .wtr record {index}", "point": record["position"]} for index, record in enumerate(water) if record["type"] == 3]
  assert len(targets) == 5
  inspection = serverNav.inspectNav(ours, highpassSafePoint, targets, recastToolingRoot, noProgress)
  main = inspection["mainPiece"]
  assert main["standIn"] is False
  safe = main["safePolygon"]
  safePolygon = next(tile for tile in inspection["tiles"] if tile["key"] == safe["tile"])["polygons"][safe["polygon"]]
  assert safePolygon["component"] == 0
  xs, ys = zip(*safePolygon["outline"])
  assert min(xs) - 5 <= highpassSafePoint[0] <= max(xs) + 5 and min(ys) - 5 <= highpassSafePoint[1] <= max(ys) + 5
  islands = inspection["islands"]
  assert (inspection["polygons"], inspection["excludedPolygons"], main["polygons"], len(islands)) == (8197, 53, 3677, 640)
  assert sum(island["snapRisk"] for island in islands) == 297
  # The two largest islands are the backdrop mountains west and east of the pass.
  assert [(island["number"], island["area"], island["snapRisk"]) for island in islands[:2]] == [(1, 117190.3, False), (2, 116640.3, True)]
  # Records 2 and 3 lie at the pass's far ends, past what 1,024 nodes search from the safe point; record 6's center lies more than 10
  # across from any polygon the ground filter walks, its own slab being Disabled.
  assert [(probe["name"], probe["result"], probe["pathPolygons"], probe["outOfNodes"], probe["pathBufferFull"]) for probe in inspection["probes"]] == [
    ("the zone line of .wtr record 2", "partial", 46, True, False),
    ("the zone line of .wtr record 3", "partial", 145, True, False),
    ("the zone line of .wtr record 4", "complete", 15, False, False),
    ("the zone line of .wtr record 5", "complete", 17, False, False),
    ("the zone line of .wtr record 6", "noGoalPolygon", 0, False, False),
  ]
  assert len(inspection["findings"]) == 3
  theirs = serverNav.inspectNav(peridot.referenceBytes("nav/highpasshold.nav"), highpassSafePoint, targets, recastToolingRoot, noProgress)
  assert {key: theirs[key] for key in ("mainPiece", "islands", "probes", "findings")} == {key: inspection[key] for key in ("mainPiece", "islands", "probes", "findings")}

  areas = ["Normal: 7,692 polygons", "Water: 452 polygons", "Disabled: 53 polygons"]
  byArea = navDrawing.drawNav(tmp_path / "highpassholdNavByArea.png", [
    navDrawing.areaPanel("Peridot's highpasshold.nav", theirs), navDrawing.areaPanel("ours, from Peridot's .map and .wtr", inspection),
    navDrawing.differencePanel("polygons that differ", inspection, theirs),
  ])
  assert byArea["legends"] == [areas, areas, ["0 polygons the other lacks", "0 polygons only the other has"]]
  islandsPlan = navDrawing.drawNav(tmp_path / "highpassholdIslands.png", [navDrawing.componentPanel("NPC islands of our highpasshold.nav", inspection)], panelWidth=1600)
  assert islandsPlan["legends"] == [["main piece: 3,677 polygons", "640 islands, 297 at snap risk", "Disabled and zone line: 53 polygons"]]
