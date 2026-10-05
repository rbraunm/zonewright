import collections
import sys
from pathlib import Path

import numpy
import pytest

from conftest import everquestClient
from serverReference import referenceBytes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import machineProfile
import recastHelper
import serverMapDrawing
import serverMapFiles
import serverNav

pytestmark = pytest.mark.clientData("serverNav")
# Peridot's zone row gives the safe point (-219, -148, -24) in server axes.
highpassSafePoint = (-148.0, -219.0, -24.0)
# thulehouse2.navprj, the map_edit project Peridot's thulehouse2.nav was built from, lowers its bounds' top by hand to 255.2.
thulehouseProjectCeiling = numpy.float32(255.2)
detailCounts = ("detailVertexCount", "detailTriangleCount")


def noProgress(done, of, message):
  pass


def navOfPeridotsFiles(zone, toolingRoot, reportProgress=noProgress):
  """Our .nav of Peridot's own .map and .wtr for a zone, and the helper's report."""
  return serverNav.navBytes(referenceBytes(f"base/{zone}.map"), referenceBytes(f"water/{zone}.wtr"), toolingRoot, reportProgress)


def tilesByKey(nav):
  return {(tile["header"]["x"], tile["header"]["y"], tile["header"]["layer"]): tile for tile in nav["tiles"]}


def polygonsWithoutLinks(tile):
  """A tile's vertices and polygons as bytes, each polygon's firstLink cleared: it indexes the tile's link pool, which fills in the order
  tiles are added."""
  polygons = tile["polygons"].copy()
  polygons["firstLink"] = 0
  return tile["vertices"].tobytes(), polygons.tobytes()


def linkSets(nav):
  """Each polygon's links as a set of (target tile key, target polygon, edge, side, low, high), by (tile key, polygon): the mesh's
  connectivity, free of the link pool's order and of tile references, which both follow the order tiles were added."""
  tileBits = nav["parameters"]["maximumTiles"].bit_length() - 1
  polygonBits = 22 - tileBits
  tiles = tilesByKey(nav)
  keyOfSlot = {(tile["reference"] >> polygonBits) & ((1 << tileBits) - 1): key for key, tile in tiles.items()}
  result = {}
  for key, tile in tiles.items():
    for index, polygon in enumerate(tile["polygons"]):
      found = set()
      link = int(polygon["firstLink"])
      while link != 0xFFFFFFFF:
        entry = tile["links"][link]
        reference = int(entry["reference"])
        target = keyOfSlot[(reference >> polygonBits) & ((1 << tileBits) - 1)]
        found.add((target, reference & ((1 << polygonBits) - 1), int(entry["edge"]), int(entry["side"]), int(entry["low"]), int(entry["high"])))
        link = int(entry["next"])
      result[(key, index)] = found
  return result


def detailOf(tile, index):
  """A polygon's detail mesh: its own detail vertices and its triangles, as bytes."""
  mesh = tile["detailMeshes"][index]
  vertexBase, triangleBase = int(mesh["vertexBase"]), int(mesh["triangleBase"])
  vertices = tile["detailVertices"][vertexBase:vertexBase + int(mesh["vertexCount"])]
  triangles = tile["detailTriangles"][triangleBase:triangleBase + int(mesh["triangleCount"])]
  return vertices.tobytes() + triangles.tobytes()


def assertTileForTile(ours, report, peridotFile, tileCount, areas, differingDetails):
  """Peridot's tiles matched by their headers' (x, y, layer), never by reference or file order, which record the order map_edit's
  threads finished in."""
  theirs, mine = serverMapFiles.readNav(peridotFile, "Peridot's .nav"), serverMapFiles.readNav(ours, "our .nav")
  assert mine["parameters"] == theirs["parameters"]
  ourTiles, theirTiles = tilesByKey(mine), tilesByKey(theirs)
  assert sorted(ourTiles) == sorted(theirTiles)
  assert len(ourTiles) == tileCount
  differing = 0
  for key, theirTile in theirTiles.items():
    ourTile = ourTiles[key]
    assert {name: value for name, value in ourTile["header"].items() if name not in detailCounts} == {name: value for name, value in theirTile["header"].items() if name not in detailCounts}
    assert polygonsWithoutLinks(ourTile) == polygonsWithoutLinks(theirTile)
    differing += sum(detailOf(ourTile, index) != detailOf(theirTile, index) for index in range(len(ourTile["polygons"])))
  assert linkSets(mine) == linkSets(theirs)
  assert collections.Counter(int(area) for tile in mine["tiles"] for area in tile["polygons"]["areaAndType"] & 0x3F) == areas
  assert report["mostChunksPerTile"] < 512
  # The pinned Recast (EQEmu 710dabe) carries upstream 13dc549, "Improve triangulateHull", which the 2017 copy that built Peridot's navs
  # lacked: polygons identical, detail triangulation not. docs/serverFiles.md records both counts.
  assert differing == differingDetails
  ordered = sorted(ourTiles, key=lambda key: (key[1], key[0]))
  polygonBits = 22 - (mine["parameters"]["maximumTiles"].bit_length() - 1)
  assert list(ourTiles) == ordered
  assert [ourTiles[key]["reference"] for key in ordered] == [(1 << 22) | (index << polygonBits) for index in range(tileCount)]


def testNavReproducesHighpassHoldTileForTile(recastToolingRoot):
  ours, report = navOfPeridotsFiles("highpasshold", recastToolingRoot)
  assertTileForTile(ours, report, referenceBytes("nav/highpasshold.nav"), 28, {0: 7692, 1: 452, 11: 53}, 4709)
  assert (report["tilesWide"], report["tilesHigh"], report["tileBits"], report["polygonBits"]) == (5, 9, 6, 16)
  assert serverNav.inspectNav(ours, None, [], recastToolingRoot, noProgress)["polygons"] == 8197


def testNavReproducesThulehouse2TileForTile(recastToolingRoot):
  collision = serverMapFiles.mapCollision(referenceBytes("base/thulehouse2.map"))
  water = serverMapFiles.readWater(referenceBytes("water/thulehouse2.wtr"))
  # Peridot's nav was built with its project's bounds, whose top (255.2) lies below the collision's (270.25), so map_edit dropped the two
  # triangles reaching above it, which also set the collision's x and y extents. Exports always take the collidable extents
  # (navFromCollision); to compare, the helper is given the project's bounds and the triangles map_edit kept.
  low, high = serverNav.navBounds(collision)
  high[1] = float(thulehouseProjectCeiling)
  kept = collision[(collision[..., 2] <= thulehouseProjectCeiling).all(axis=1)]
  assert len(collision) - len(kept) == 2
  inputBytes = recastHelper.navInput(
    serverMapFiles.inRecastAxes(kept), (low, high), serverNav.navVolumes(water), serverNav.serverNavSettings, machineProfile.workerCount(),
  )
  payload, report = recastHelper.runHelper(recastToolingRoot, "nav", inputBytes, noProgress)
  ours = serverMapFiles.navFile(payload)
  assertTileForTile(ours, report, referenceBytes("nav/thulehouse2.nav"), 22, {0: 3440, 1: 6, 11: 114}, 1865)
  assert serverNav.inspectNav(ours, None, [], recastToolingRoot, noProgress)["polygons"] == 3560


def testNavIsDeterministic(recastToolingRoot):
  progress = []
  first, report = navOfPeridotsFiles("highpasshold", recastToolingRoot, lambda done, of, message: progress.append((done, of, message)))
  second, _ = navOfPeridotsFiles("highpasshold", recastToolingRoot)
  assert first == second
  assert report["threads"] == min(machineProfile.workerCount(), 45) > 1
  assert progress[0] == (0, 1, "partitioning collidable triangles")
  assert progress[-1] == (45, 45, "building nav tiles")


def testNavFromOurHighpassMapMatchesPeridots(recastToolingRoot):
  client = Path(everquestClient)
  ourMap = serverMapFiles.mapBytes(serverMapFiles.zoneFilesOf((client / "highpasshold.eqg").read_bytes(), (client / "highpasshold.zon").read_bytes()))
  ours, report = serverNav.navBytes(ourMap, referenceBytes("water/highpasshold.wtr"), recastToolingRoot, noProgress)
  mine, theirs = serverMapFiles.readNav(ours, "our .nav"), serverMapFiles.readNav(referenceBytes("nav/highpasshold.nav"), "Peridot's .nav")
  assert mine["parameters"] == theirs["parameters"]
  ourTiles, theirTiles = tilesByKey(mine), tilesByKey(theirs)
  assert sorted(ourTiles) == sorted(theirTiles)
  assert len(ourTiles) == 28

  # Which grid tiles' rasterized squares (the tile and its 5-cell border) any terrain or placed-model triangle's footprint reaches, in
  # Recast x and z.
  collision = serverMapFiles.inRecastAxes(serverMapFiles.mapCollision(ourMap))[..., [0, 2]]
  terrainCount = len(serverMapFiles.readMap(ourMap)["collidableIndices"]) // 3
  low, high = collision.min(axis=1), collision.max(axis=1)
  origin = numpy.array(mine["parameters"]["origin"])[[0, 2]]
  tileWidth = mine["parameters"]["tileWidth"]
  border = serverNav.serverNavSettings["borderSize"] * serverNav.serverNavSettings["cellSize"]

  def reached(tx, ty):
    tileLow = origin + numpy.array([tx, ty]) * tileWidth - border
    tileHigh = origin + numpy.array([tx + 1, ty + 1]) * tileWidth + border
    return ((high >= tileLow).all(axis=1) & (low <= tileHigh).all(axis=1))
  grid = [(tx, ty) for ty in range(report["tilesHigh"]) for tx in range(report["tilesWide"])]
  holdsTerrain = {key for key in grid if reached(*key)[:terrainCount].any()}
  holdsPlaced = {key for key in grid if reached(*key)[terrainCount:].any()}
  # No tile holds terrain alone: every built tile holds placed-model triangles, five of them nothing else, so no tile's input is
  # bit-identical to Peridot's, and every one of the 28 is compared below.
  assert sorted(holdsTerrain - holdsPlaced) == []
  builtTiles = {(x, y) for x, y, _ in ourTiles}
  assert builtTiles <= holdsPlaced
  assert sorted(builtTiles - holdsTerrain) == [(0, 1), (3, 1), (4, 1), (4, 2), (4, 3)]
  # Our turns are the .zon's, Peridot's azone's round trip of them (up to 2.4e-7 rad apart): no voxel changes, so no tile's polygons
  # differ (docs/serverFiles.md), and the whole file is the one Peridot's own .map gives.
  differing = [key for key, ourTile in ourTiles.items() if polygonsWithoutLinks(ourTile) != polygonsWithoutLinks(theirTiles[key])]
  assert differing == []
  assert ours == navOfPeridotsFiles("highpasshold", recastToolingRoot)[0]


def testIslandsAndProbeOnHighpassHold(recastToolingRoot, tmp_path):
  ours, _ = navOfPeridotsFiles("highpasshold", recastToolingRoot)
  water = serverMapFiles.readWater(referenceBytes("water/highpasshold.wtr"))
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
  theirs = serverNav.inspectNav(referenceBytes("nav/highpasshold.nav"), highpassSafePoint, targets, recastToolingRoot, noProgress)
  assert {key: theirs[key] for key in ("mainPiece", "islands", "probes", "findings")} == {key: inspection[key] for key in ("mainPiece", "islands", "probes", "findings")}

  areas = ["Normal: 7,692 polygons", "Water: 452 polygons", "Disabled: 53 polygons"]
  byArea = serverMapDrawing.drawNav(tmp_path / "highpassholdNavByArea.png", [
    serverMapDrawing.areaPanel("Peridot's highpasshold.nav", theirs), serverMapDrawing.areaPanel("ours, from Peridot's .map and .wtr", inspection),
    serverMapDrawing.differencePanel("polygons that differ", inspection, theirs),
  ])
  assert byArea["legends"] == [areas, areas, ["0 polygons the other lacks", "0 polygons only the other has"]]
  islandsPlan = serverMapDrawing.drawNav(tmp_path / "highpassholdIslands.png", [serverMapDrawing.componentPanel("NPC islands of our highpasshold.nav", inspection)], panelWidth=1600)
  assert islandsPlan["legends"] == [["main piece: 3,677 polygons", "640 islands, 297 at snap risk", "Disabled and zone line: 53 polygons"]]
