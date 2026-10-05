import shutil
import struct
import sys
from pathlib import Path

import numpy
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from conftest import StagedServer, junction, pinnedBlender, pinnedRecast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import navDrawing
import planDrawing
import recastHelper
import serverMapFiles
import serverNav

tileWorldSize = serverNav.serverNavSettings["tileSize"] * serverNav.serverNavSettings["cellSize"]


def noProgress(done, of, message):
  pass


def floor(x0, x1, y0, y1, z):
  """A flat square floor in zone axes, wound to face up, as two triangles in server axes (zone y, zone x, z)."""
  corners = numpy.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], dtype=numpy.float32)
  return corners[[0, 1, 2, 0, 2, 3]].reshape(2, 3, 3)[..., [1, 0, 2]]


def soup(*floors):
  return numpy.concatenate(floors).astype(numpy.float32)


def tileFrames(payload):
  """Each tile's (reference offset, size offset, data start, size) in a nav payload."""
  frames = []
  offset = serverMapFiles.countLayout.size + serverMapFiles.navParameters.size
  for _ in range(struct.unpack_from("<I", payload, 0)[0]):
    size = struct.unpack_from("<i", payload, offset + 4)[0]
    frames.append((offset, offset + 4, offset + 8, size))
    offset += 8 + size
  return frames


def inspectPayload(payload, toolingRoot):
  return recastHelper.runHelper(toolingRoot, "inspect", recastHelper.inspectInput(payload, None, [], serverNav.serverPathing), noProgress)


@pytest.fixture
def stageWithSharedBlender(tmp_path, sharedLocalAppData):
  """Staged servers whose Blender and profile are the shared install's, and whose tooling roots hold no Recast tree or helper yet."""
  staged = []

  def stage(name, manifest):
    localAppData = tmp_path / name / "localAppData"
    toolingRoot = localAppData / "zonewright"
    toolingRoot.mkdir(parents=True)
    shutil.copy(sharedLocalAppData / "zonewright" / "machineProfile.json", toolingRoot / "machineProfile.json")
    junction(toolingRoot / "blender", sharedLocalAppData / "zonewright" / "blender")
    staged.append(StagedServer(tmp_path / name, manifest, localAppData))
    return staged[-1]
  yield stage
  for server in staged:
    server.close()


@pytest.mark.install
def testSyncBuildsTheRecastHelper(stageWithSharedBlender):
  server = stageWithSharedBlender("fresh", {"blender": pinnedBlender, "extensions": {}})
  missing = server.callToolExpectingSuccess("getToolingStatus")[0]["recastHelper"]
  assert missing["state"] == "missing"
  assert missing["commit"] == pinnedRecast["commit"]

  result, progressMessages = server.callToolExpectingSuccess("syncTooling")
  fetched, built = result["actions"]
  assert fetched == {"tool": "recast", "action": "fetched", "commit": pinnedRecast["commit"]}
  assert (built["tool"], built["action"], built["fingerprint"]) == ("recastHelper", "built", missing["fingerprint"])
  assert {"downloading Recast 710dabee7017", "extracting Recast", "building the Recast helper"} <= set(progressMessages)
  status = result["status"]["recastHelper"]
  assert status["state"] == "built"
  assert status["compileSeconds"] == built["compileSeconds"]
  executable = Path(status["executablePath"])
  assert executable == server.toolingRoot / "recastHelper" / missing["fingerprint"][:16] / "recastHelper.exe"
  assert executable.is_file()
  assert sorted(path.relative_to(server.toolingRoot / "recast" / "710dabee7017").as_posix() for path in (server.toolingRoot / "recast" / "710dabee7017").rglob("*.cpp")) == [
    "Detour/Source/DetourAlloc.cpp", "Detour/Source/DetourAssert.cpp", "Detour/Source/DetourCommon.cpp", "Detour/Source/DetourNavMesh.cpp",
    "Detour/Source/DetourNavMeshBuilder.cpp", "Detour/Source/DetourNavMeshQuery.cpp", "Detour/Source/DetourNode.cpp",
    "Recast/Source/Recast.cpp", "Recast/Source/RecastAlloc.cpp", "Recast/Source/RecastArea.cpp", "Recast/Source/RecastAssert.cpp",
    "Recast/Source/RecastContour.cpp", "Recast/Source/RecastFilter.cpp", "Recast/Source/RecastLayers.cpp", "Recast/Source/RecastMesh.cpp",
    "Recast/Source/RecastMeshDetail.cpp", "Recast/Source/RecastRasterization.cpp", "Recast/Source/RecastRegion.cpp",
    "RecastDemo/Source/ChunkyTriMesh.cpp",
  ]

  editedSource = server.repositoryPath / "recastHelper" / "helperIO.h"
  editedSource.write_text(editedSource.read_text(encoding="ascii") + "\n", encoding="ascii")
  stale = server.callToolExpectingSuccess("getToolingStatus")[0]["recastHelper"]
  assert stale["state"] == "stale"
  assert stale["fingerprint"] != missing["fingerprint"]
  result, _ = server.callToolExpectingSuccess("syncTooling")
  assert [(action["tool"], action["action"], action["fingerprint"]) for action in result["actions"]] == [("recastHelper", "built", stale["fingerprint"])]
  assert result["status"]["recastHelper"]["state"] == "built"
  assert executable.is_file(), "the build of the other fingerprint stays, as another worktree may use it"
  assert server.callToolExpectingSuccess("syncTooling")[0]["actions"] == []

  wrongTree = pinnedRecast | {"treeSha256": "0" * 64}
  server.writeManifest({"blender": pinnedBlender, "extensions": {}, "recast": wrongTree})
  errorText = server.callToolExpectingError("syncTooling")
  assert f"{server.toolingRoot / 'recast' / '710dabee7017'} holds a tree whose digest {pinnedRecast['treeSha256']} does not match pinned treeSha256 {'0' * 64}" in errorText

  fresh = stageWithSharedBlender("wrongPin", {"blender": pinnedBlender, "extensions": {}, "recast": wrongTree})
  errorText = fresh.callToolExpectingError("syncTooling")
  assert f"the extracted tree's digest {pinnedRecast['treeSha256']} does not match pinned treeSha256 {'0' * 64}" in errorText
  assert sorted(entry.name for entry in fresh.toolingRoot.iterdir()) == ["blender", "logs", "machineProfile.json"]


def testNavRefusesWithoutABuiltHelper(tmp_path):
  with pytest.raises(ToolError, match=r"^The Recast helper is missing; run syncTooling to build it$"):
    serverNav.navFromCollision(floor(0, 40, 0, 40, 0), [], tmp_path / "zonewright", noProgress)


def testRecastHelperRefusesWhatMapEditDrops(recastToolingRoot):
  ground = floor(0, 40, 0, 40, 0)

  span = 130 * tileWorldSize - 1
  with pytest.raises(ToolError, match=r"the nav grid needs 130 x 130 = 16900 tiles, more than the 16,384 that 14 tile bits address"):
    serverNav.navFromCollision(soup(ground, floor(span - 1, span, span - 1, span, 0)), [], recastToolingRoot, noProgress)

  # Each 16-unit platform makes one polygon; a far corner stretches the grid to 91 x 91 tiles, which leaves 8 polygon bits.
  pitch, side = 24, 16
  platforms = [floor(row * pitch, row * pitch + side, column * pitch, column * pitch + side, 0) for row in range(16) for column in range(16)]
  span = 91 * tileWorldSize - 1
  corner = floor(span - 1, span, span - 1, span, 0)
  _, report = serverNav.navFromCollision(soup(*platforms, corner), [], recastToolingRoot, noProgress)
  assert (report["polygonBits"], report["builtTiles"], report["polygons"]) == (8, 1, 256)
  oneMore = floor(16 * pitch, 16 * pitch + side, 0, side, 0)
  with pytest.raises(ToolError, match=r"tile \(0, 0\), zone x 0\.0 to 409\.6, y 0\.0 to 409\.6 has 257 polygons, more than the 256 its 8 polygon bits address"):
    serverNav.navFromCollision(soup(*platforms, oneMore, corner), [], recastToolingRoot, noProgress)

  sunk = soup(ground, floor(0, 10, 0, 10, -15000.5))
  with pytest.raises(ToolError, match=r"collidable triangle 2 has a vertex at zone \(0\.00, 0\.00, -15000\.50\), at or below z -15000"):
    serverNav.navFromCollision(sunk, [], recastToolingRoot, noProgress)

  waterBox = {"type": 1, "position": (20.0, 20.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0), "halfExtents": (5.0, 5.0, 5.0)}
  with pytest.raises(ToolError, match=r"^\.wtr record 1 at zone \(20\.0, 20\.0, 0\.0\) is type 11, which no nav area maps; map_edit would quietly make it Disabled$"):
    serverNav.navFromCollision(ground, [waterBox, waterBox | {"type": 11}], recastToolingRoot, noProgress)
  with pytest.raises(ToolError, match=r"^\.wtr record 0 at zone \(20\.0, 20\.0, 0\.0\) is type 0 \(Normal\), which no nav area maps; map_edit would quietly mark it"
      r" area 0, Recast's null area, and cut a hole in the nav under it$"):
    serverNav.navFromCollision(ground, [waterBox | {"type": 0}], recastToolingRoot, noProgress)
  with pytest.raises(ToolError, match=r"^\.wtr record 0 at zone \(20\.0, 20\.0, 0\.0\) is type 10 \(DisableNavMesh\), which no nav area maps; map_edit would quietly make it Disabled$"):
    serverNav.navFromCollision(ground, [waterBox | {"type": 10}], recastToolingRoot, noProgress)
  with pytest.raises(ToolError, match=r"^\.wtr record 0 at zone \(20\.0, 20\.0, 0\.0\) is turned \(0\.0, 0\.0, -90\.0\) degrees; zonewright's \.wtr turns no region"):
    serverNav.navFromCollision(ground, [waterBox | {"rotation": (0.0, 0.0, -90.0)}], recastToolingRoot, noProgress)


def testNavHelperRefusesTrianglesOutsideItsBoundsAndATileDetourCannotCreate(recastToolingRoot):
  ground = floor(0, 40, 0, 40, 0)
  narrowBounds = ([0.0, 0.0, 0.0], [20.0, 0.0, 20.0])
  narrow = recastHelper.navInput(serverMapFiles.inRecastAxes(ground), narrowBounds, [], serverNav.serverNavSettings, 1)
  with pytest.raises(ToolError, match=r"^recastHelper nav: collidable triangle 0 has a vertex at zone \(40\.00, 0\.00, 0\.00\), outside the nav bounds"
      r" zone \(0\.00, 0\.00, 0\.00\) to zone \(20\.00, 20\.00, 0\.00\); map_edit would drop the triangle$"):
    recastHelper.runHelper(recastToolingRoot, "nav", narrow, noProgress)

  # Detour stores at most 6 vertices a polygon, where Recast builds with 7.
  sevenSided = serverNav.serverNavSettings | {"verticesPerPolygon": 7}
  tooWide = recastHelper.navInput(serverMapFiles.inRecastAxes(ground), serverNav.navBounds(ground), [], sevenSided, 1)
  with pytest.raises(ToolError, match=r"^recastHelper nav: tile \(0, 0\), zone x 0\.0 to 409\.6, y 0\.0 to 409\.6: dtCreateNavMeshData failed$"):
    recastHelper.runHelper(recastToolingRoot, "nav", tooWide, noProgress)


def testInspectFindsIslandsSnapRiskAndProbes(recastToolingRoot):
  main = floor(0, 100, 0, 100, 0)
  apart = floor(200, 240, 0, 40, 0)
  above = floor(20, 50, 20, 50, 50)
  navFile, report = serverNav.navFromCollision(soup(main, apart, above), [], recastToolingRoot, noProgress)
  assert report["builtTiles"] == 1
  targets = [{"name": "far floor", "point": (220, 20, 0)}, {"name": "same floor", "point": (80, 80, 0)}, {"name": "nowhere", "point": (500, 500, 0)}]

  inspection = serverNav.inspectNav(navFile, (50, 50, 0), targets, recastToolingRoot, noProgress)
  # Each floor's nav lies 3 cells in from its edges (the ledge filter's cell and the 2-cell agent radius) and one cell height above it.
  main = inspection["mainPiece"]
  assert (main["standIn"], main["boundsMin"], main["boundsMax"]) == (False, [2.4, 2.4, 0.4], [97.6, 97.6, 0.4])
  assert [(island["number"], island["boundsMin"], island["boundsMax"], island["snapRisk"]) for island in inspection["islands"]] == [
    (1, [202.4, 2.4, 0.4], [237.6, 37.6, 0.4], False),
    (2, [22.4, 22.4, 50.4], [48.0, 48.0, 50.4], True),
  ]
  assert (inspection["polygons"], main["polygons"], inspection["excludedPolygons"]) == (35, 17, 0)
  assert [(probe["name"], probe["result"], probe["reached"], probe["goalComponent"]) for probe in inspection["probes"]] == [
    ("far floor", "partial", False, 1), ("same floor", "complete", True, 0), ("nowhere", "noGoalPolygon", False, -1),
  ]
  assert inspection["findings"] == [
    "NPCs cannot path from the safe point to far floor within Peridot's 1,024 search nodes: it lies on island 1, apart from the main piece",
    "NPCs cannot path from the safe point to nowhere within Peridot's 1,024 search nodes: no polygon the server's ground filter allows lies"
    " within (10.0, 200.0, 10.0) of it",
  ]
  components = [polygon["component"] for tile in inspection["tiles"] for polygon in tile["polygons"]]
  assert [components.count(number) for number in (0, 1, 2)] == [17, 9, 9]

  # The main piece is the safe point's, not the largest: on the smaller floor apart, the large floor is an island.
  onApart = serverNav.inspectNav(navFile, (220, 20, 0), [], recastToolingRoot, noProgress)
  assert (onApart["mainPiece"]["standIn"], onApart["mainPiece"]["polygons"]) == (False, 9)
  assert (onApart["mainPiece"]["boundsMin"], onApart["mainPiece"]["boundsMax"]) == ([202.4, 2.4, 0.4], [237.6, 37.6, 0.4])
  assert [(island["number"], island["polygons"], island["boundsMin"], island["boundsMax"], island["snapRisk"]) for island in onApart["islands"]] == [
    (1, 17, [2.4, 2.4, 0.4], [97.6, 97.6, 0.4], False),
    (2, 9, [22.4, 22.4, 50.4], [48.0, 48.0, 50.4], False),
  ]
  safe = onApart["mainPiece"]["safePolygon"]
  safePolygon = next(tile for tile in onApart["tiles"] if tile["key"] == safe["tile"])["polygons"][safe["polygon"]]
  assert safePolygon["component"] == 0
  xs, ys = zip(*safePolygon["outline"])
  assert min(xs) <= 220 <= max(xs) and min(ys) <= 20 <= max(ys)

  offMesh = serverNav.inspectNav(navFile, (1000, 1000, 0), targets[:1], recastToolingRoot, noProgress)
  assert offMesh["mainPiece"] == main | {"standIn": True, "safePolygon": None}
  assert [probe["result"] for probe in offMesh["probes"]] == ["noStartPolygon"]
  assert offMesh["findings"][0] == ("The safe point is off the NPC mesh: no polygon lies within (5.0, 100.0, 5.0) of it; islands are counted"
    " against the largest component, which stands in for the main piece")

  withoutSafePoint = serverNav.inspectNav(navFile, None, targets, recastToolingRoot, noProgress)
  assert withoutSafePoint["mainPiece"] == main | {"standIn": True, "safePolygon": None}
  assert withoutSafePoint["islands"] == inspection["islands"]
  assert [(probe["name"], probe["result"], probe["reached"], probe["goalComponent"]) for probe in withoutSafePoint["probes"]] == [
    ("far floor", "noSafePoint", False, 1), ("same floor", "noSafePoint", False, 0), ("nowhere", "noSafePoint", False, -1),
  ]
  assert withoutSafePoint["findings"] == [
    "No safe point: islands are counted against the largest component, which stands in for the main piece",
    "NPC paths were not probed, as there is no safe point to start from: to far floor, same floor, nowhere",
  ]


def testInspectRefusesWhatTheServerWouldDrop(recastToolingRoot):
  navFile, _ = serverNav.navFromCollision(soup(floor(0, 60, 0, 60, 0), floor(500, 560, 0, 60, 0)), [], recastToolingRoot, noProgress)
  payload = serverMapFiles.navPayload(serverMapFiles.readNav(navFile))
  (firstReferenceAt, firstSizeAt, firstDataAt, firstSize), (secondReferenceAt, _, _, _) = tileFrames(payload)
  firstReference = struct.unpack_from("<I", payload, firstReferenceAt)[0]

  # The helper refuses what the payload's reader refuses on its own, never reading past a tile.
  single = struct.pack("<I", 1) + payload[4:firstDataAt + firstSize]
  polygonCount, vertexCount, linkCount = struct.unpack_from("<3i", single, firstDataAt + 24)
  linksAt = firstDataAt + serverMapFiles.tileHeader.size + vertexCount * 12 + polygonCount * 32
  linkless = (single[:firstSizeAt] + struct.pack("<i", firstSize - linkCount * 12) + single[firstDataAt:firstDataAt + 32] + struct.pack("<i", 0)
    + single[firstDataAt + 36:linksAt] + single[linksAt + linkCount * 12:])
  for broken, refusal in (
    (single[:firstDataAt] + b"XXXX" + single[firstDataAt + 4:], r"payload tile 0 has magic 1482184792 and version 7, not Detour's 1145979222 and 7"),
    (linkless, r"payload tile 0 \(0, 0, layer 0\) has no links; Detour builds no such tile, and addTile would write before its link pool"),
    (single[:firstReferenceAt] + struct.pack("<I", 0) + single[firstReferenceAt + 4:], r"payload tile 0 has reference 0 and size \d+; the server drops the whole mesh on a zero"),
    (single[:firstSizeAt] + struct.pack("<i", 0) + single[firstSizeAt + 4:], r"payload tile 0 has reference \d+ and size 0; the server drops the whole mesh on a zero"),
    (single[:firstSizeAt] + struct.pack("<i", 104) + single[firstDataAt:firstDataAt + 104],
      r"payload tile 0 \(0, 0, layer 0\) is 104 bytes, but its header's counts make \d+; Detour's addTile would read and write by the counts"),
  ):
    with pytest.raises(ToolError, match=rf"^recastHelper inspect: {refusal}$"):
      inspectPayload(broken, recastToolingRoot)

  # Each tile loads at the slot its stored reference names, as the server loads it: one naming a slot already taken fails addTile.
  sharedSlot = payload[:secondReferenceAt] + struct.pack("<I", firstReference) + payload[secondReferenceAt + 4:]
  with pytest.raises(ToolError, match=rf"^recastHelper inspect: payload tile 1 with reference {firstReference} fails dtNavMesh::addTile \(status \d+\);"
      r" the server would lose it without a word$"):
    serverNav.inspectNav(serverMapFiles.navFile(sharedSlot), None, [], recastToolingRoot, noProgress)

  twice = struct.pack("<I", 3) + payload[4:] + payload[firstReferenceAt:firstDataAt + firstSize]
  with pytest.raises(ToolError, match=r"^recastHelper inspect: payload tile 2 with reference \d+ fails dtNavMesh::addTile \(status \d+\); the server would lose it without a word$"):
    serverNav.inspectNav(serverMapFiles.navFile(twice), None, [], recastToolingRoot, noProgress)


def testNavPlansDrawAreasComponentsAndDifferences(recastToolingRoot, tmp_path):
  floors = soup(floor(0, 100, 0, 100, 0), floor(200, 240, 0, 40, 0), floor(20, 50, 20, 50, 50))
  waterBox = {"type": 1, "position": (70.0, 70.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0), "halfExtents": (15.0, 15.0, 5.0)}
  dry, _ = serverNav.navFromCollision(floors, [], recastToolingRoot, noProgress)
  wet, _ = serverNav.navFromCollision(floors, [waterBox], recastToolingRoot, noProgress)
  safePoint = (85, 15, 0)
  dryInspection = serverNav.inspectNav(dry, safePoint, [], recastToolingRoot, noProgress)
  wetInspection = serverNav.inspectNav(wet, safePoint, [], recastToolingRoot, noProgress)
  panels = [
    navDrawing.areaPanel("by area", wetInspection), navDrawing.componentPanel("islands", wetInspection),
    navDrawing.differencePanel("with water against without", wetInspection, dryInspection), navDrawing.differencePanel("against itself", wetInspection, wetInspection),
  ]
  sheetPath = tmp_path / "navPlans.png"
  drawn = navDrawing.drawNav(sheetPath, panels, panelWidth=400)
  # The water box repartitions the whole floor it lies on (36 polygons where there were 17); the two other floors are unchanged.
  assert drawn["legends"] == [
    ["Normal: 51 polygons", "Water: 3 polygons"],
    ["main piece: 36 polygons", "2 islands, 1 at snap risk", "Disabled and zone line: 0 polygons"],
    ["36 polygons the other lacks", "17 polygons only the other has"],
    ["0 polygons the other lacks", "0 polygons only the other has"],
  ]
  assert drawn["labelsWritten"] == [0, 2, 0, 0]
  frame = planDrawing.PlanFrame(drawn["frame"]["center"], drawn["frame"]["width"], drawn["frame"]["size"])

  with Image.open(sheetPath) as sheet:
    def colorAt(panel, point):
      x, y = frame.pixel(point)
      left, top = drawn["panelOrigins"][panel]
      return sheet.getpixel((round(left + x), round(top + y)))

    def filled(color):
      return {color, navDrawing.darker(color)}

    assert colorAt(0, (72, 72)) in filled(navDrawing.areaColors[1])
    assert colorAt(0, (12, 88)) in filled(navDrawing.areaColors[0])
    assert colorAt(1, (85, 15)) in filled(navDrawing.mainPieceColor)
    assert colorAt(1, (208, 32)) in filled(navDrawing.islandColor)
    assert colorAt(1, (165, 65)) == navDrawing.background[:3]
    assert colorAt(2, (72, 72)) in filled(navDrawing.differenceColor)
    assert colorAt(2, (208, 32)) in filled(navDrawing.contextColor)
    assert colorAt(3, (72, 72)) in filled(navDrawing.contextColor)
