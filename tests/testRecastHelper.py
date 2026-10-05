import shutil
import struct
import sys
import zlib
from pathlib import Path

import numpy
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from conftest import StagedServer, junction, pinnedBlender, pinnedRecast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
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

  pitch, side = 24, 16
  platforms = [floor(row * pitch, row * pitch + side, column * pitch, column * pitch + side, 0) for row in range(17) for column in range(17)]
  span = 91 * tileWorldSize - 1
  with pytest.raises(ToolError, match=r"tile \(0, 0\), zone x 0\.0 to 409\.6, y 0\.0 to 409\.6 has 289 polygons, more than the 256 its 8 polygon bits address"):
    serverNav.navFromCollision(soup(*platforms, floor(span - 1, span, span - 1, span, 0)), [], recastToolingRoot, noProgress)

  sunk = soup(ground, floor(0, 10, 0, 10, -15000.5))
  with pytest.raises(ToolError, match=r"collidable triangle 2 has a vertex at zone \(0\.00, 0\.00, -15000\.50\), at or below z -15000"):
    serverNav.navFromCollision(sunk, [], recastToolingRoot, noProgress)

  waterBox = {"type": 1, "position": (20.0, 20.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0), "halfExtents": (5.0, 5.0, 5.0)}
  unknownBox = waterBox | {"type": 11}
  with pytest.raises(ToolError, match=r"\.wtr record 1 at zone \(20\.0, 20\.0, 0\.0\) is type 11, which no nav area maps; map_edit would quietly make it Disabled"):
    serverNav.navFromCollision(ground, [waterBox, unknownBox], recastToolingRoot, noProgress)


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
  assert [component for tile in inspection["polygonComponents"] for component in tile].count(0) == inspection["mainPiece"]["polygons"]

  offMesh = serverNav.inspectNav(navFile, (1000, 1000, 0), targets[:1], recastToolingRoot, noProgress)
  assert offMesh["mainPiece"] == inspection["mainPiece"] | {"standIn": True}
  assert [probe["result"] for probe in offMesh["probes"]] == ["noStartPolygon"]
  assert offMesh["findings"][0] == ("The safe point is off the NPC mesh: no polygon lies within (5.0, 100.0, 5.0) of it; islands are counted"
    " against the largest component, which stands in for the main piece")

  withoutSafePoint = serverNav.inspectNav(navFile, None, targets, recastToolingRoot, noProgress)
  assert withoutSafePoint["probes"] == []
  assert withoutSafePoint["islands"] == inspection["islands"]
  assert withoutSafePoint["findings"] == ["No safe point: islands are counted against the largest component, which stands in for the main piece"]


def testInspectRefusesWhatTheServerWouldDrop(recastToolingRoot):
  navFile, _ = serverNav.navFromCollision(floor(0, 60, 0, 60, 0), [], recastToolingRoot, noProgress)
  payload = serverNav.navPayload(navFile)
  zeroReference = payload[:32] + struct.pack("<I", 0) + payload[36:]
  with pytest.raises(ToolError, match=r"Nav tile 0 has reference 0 and size \d+; the server drops the whole mesh on a zero"):
    serverNav.navContainer(zeroReference)
  compressed = zlib.compress(zeroReference)
  zeroFile = b"EQNAVMESH" + struct.pack("<3I", 2, len(compressed), len(zeroReference)) + compressed
  with pytest.raises(ToolError, match=r"recastHelper inspect: payload tile 0 has reference 0 and size \d+; the server drops the whole mesh on a zero"):
    serverNav.inspectNav(zeroFile, None, [], recastToolingRoot, noProgress)

  tileCount = struct.unpack_from("<I", payload, 0)[0]
  assert tileCount == 1
  twice = struct.pack("<I", 2) + payload[4:] + payload[32:]
  with pytest.raises(ToolError, match=r"recastHelper inspect: payload tile 1 with reference \d+ fails dtNavMesh::addTile \(status \d+\); the server would lose it without a word"):
    serverNav.inspectNav(serverNav.navContainer(twice), None, [], recastToolingRoot, noProgress)
