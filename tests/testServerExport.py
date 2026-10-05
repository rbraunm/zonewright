import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import numpy
import pytest
from PIL import Image

from conftest import StagedServer, junction, pinnedBlender, pinnedRecast, writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import exportPipeline
import planDrawing
import recastHelper
import serverMapDrawing
import serverMapFiles
import serverNav

environment = {
  "ambientColor": [0.3, 0.3, 0.35], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.15], "sunColor": [0.6, 0.5, 0.4],
  "sunAzimuthDegrees": 40, "sunElevationDegrees": 35, "fogColor": [0.5, 0.55, 0.6], "fogOn": False, "minClip": 50, "maxClip": 4000,
  "sky": "none", "newEngineZone": False,
}
# Off the origin and the diagonal, so the safe point's axes decide where it is: (60, 150) lies off the ground.
safe = {"safePoint": [150, 60, 1, 0], "underworld": -50}
target = {"zone": "highpasshold", "x": 100, "y": 200, "z": 5, "headingDegrees": 90}
pond = {"minimum": [-60, -60, -10], "maximum": [-30, -30, 2]}
northLine = {"minimum": [236, -20, -5], "maximum": [244, 20, 30]}
# Two daises, one model placed twice, unturned: 16 across, so their tops hold NPC nav, and 8 high, over the NPC's climb.
daises = {"dais": [-100.5, 30.25], "dais.001": [79.5, -29.75]}
daisHalfWidth, daisHeight = 8, 8


def noProgress(done, of, message):
  pass


async def serverZone(session, folder, playerValues=True):
  """Flat ground 512 north to south and 192 across with the two daises, a bush marked passable, a boundary wall, a swim box standing
  alone, a zone line, and the zone row's view values (and the safe point and underworld), saved as servertest.blend in folder."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [512, 192], "spacing": 16, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(writePNG(folder / "grass.png", 4, 4, (90, 120, 60, 255)))})
  await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(writePNG(folder / "stone.png", 4, 4, (120, 110, 100, 255)))})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
  first, second = daises.items()
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": first[0], "size": [16, 16, daisHeight], "location": [*first[1], 0]})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "bush", "size": [6, 6, 6], "location": [40, 40, 0]})
  for name in ("dais", "bush"):
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "stone"})
    await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("duplicateObjects", {"names": [first[0]], "offset": [second[1][0] - first[1][0], second[1][1] - first[1][1], 0], "linkData": True})
  await session.expectSuccess("markPassable", {"objects": ["bush"]})
  await session.expectSuccess("placeBoundaryWall", {"name": "southWall", "path": [[-200, -90], [200, -90]], "height": 30})
  await session.expectSuccess("placeSwimVolume", {"name": "pond", "liquid": "water"} | pond)
  await session.expectSuccess("placeZoneLine", {"number": 1, "label": "north", "target": target} | northLine)
  await session.expectSuccess("setZoneProperties", environment | (safe if playerValues else {}))
  await session.expectSuccess("saveFile", {"path": str(folder / "servertest.blend")})


def checkFolderOf(server):
  return server.toolingRoot / "exports" / "servertest" / "check"


def solidTriangles(archive):
  """The archive's triangles the client collides with, placed as the client places them and rounded once to float32, in the server's
  axes and in the .map's order: the terrain's, then each other placement's in .zon order."""
  zone = eqgFiles.parseZone(archive.read("servertest.zon"), "servertest.zon")
  placements = sorted(zone["placements"], key=lambda placement: not placement["model"].endswith(".ter"))
  parts = []
  for placement in placements:
    model = eqgFiles.parseModel(archive.read(placement["model"]), placement["model"])
    solid = model["triangles"][(model["triangleFlags"] & eqgFiles.passableFlag) == 0]
    parts.append(eqgFiles.placeVertices(model["vertices"].astype(numpy.float64), placement)[solid])
  return numpy.concatenate(parts)[..., [1, 0, 2]].astype(numpy.float32)


def expectedNavParameters(collision):
  """The nav parameters map_edit derives from the collidable extents and its settings (Recast axes: zone y, height, zone x)."""
  points = serverMapFiles.inRecastAxes(collision).reshape(-1, 3)
  low, high = points.min(axis=0), points.max(axis=0)
  settings = serverNav.serverNavSettings
  tileWidth = settings["tileSize"] * settings["cellSize"]
  tiles = [-(-int((high[axis] - low[axis]) / settings["cellSize"] + 0.5) // settings["tileSize"]) for axis in (0, 2)]
  tileBits = min((tiles[0] * tiles[1] - 1).bit_length(), 14)
  return {
    "origin": tuple(float(value) for value in low), "tileWidth": float(numpy.float32(tileWidth)), "tileHeight": float(numpy.float32(tileWidth)),
    "maximumTiles": 1 << tileBits, "maximumPolygons": 1 << (22 - tileBits),
  }


def testCheckExportGameBuildsTheServerFilesInScratch(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "servertest.eqg"

  async def steps(session):
    await serverZone(session, tmp_path)
    return await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})

  checked = stageBlenderServer.session(steps)
  folder = checkFolderOf(stageBlenderServer)
  assert [failure["failure"] for failure in checked["failures"]] == ["containment not checked"]
  assert checked["checkFolder"] == str(folder) and not archivePath.exists()
  paths = {kind: folder / "server" / "maps" / kind / f"servertest.{extension}" for kind, extension in (("base", "map"), ("water", "wtr"), ("nav", "nav"))}
  assert checked["serverFiles"]["files"] == {"map": str(paths["base"]), "water": str(paths["water"]), "nav": str(paths["nav"])}
  assert sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()) == [
    "server/maps/base/servertest.map", "server/maps/nav/servertest.nav", "server/maps/water/servertest.wtr", "servertest.eqg", "servertest_export.json",
  ]

  # The server's collision is the archive's solid triangles, the bush (marked passable) left out, the boundary wall in, to the last bit.
  archive = eqArchive.EQArchive(folder / "servertest.eqg")
  mapContent = serverMapFiles.readMap(paths["base"].read_bytes())
  collision = serverMapFiles.collisionTriangles(mapContent)
  solid = solidTriangles(archive)
  assert collision.dtype == numpy.float32 and collision.shape == solid.shape and numpy.array_equal(collision, solid)
  # The bush is placed with its polygons not marked vis, which the server never collides with.
  assert [placement["name"] for placement in mapContent["placements"]] == ["obj_dais.mod", "obj_bush_passable.mod", "obj_dais.mod"]
  assert len(serverMapFiles.passableTriangles(mapContent)) == 12

  # The swim box, then the zone line: each its type, its center, and its half extents, unturned.
  records = serverMapFiles.readWater(paths["water"].read_bytes())
  boxes = [(1, pond), (3, northLine)]
  assert [(record["type"], record["position"], record["rotation"], record["scale"], record["halfExtents"]) for record in records] == [
    (kind, tuple((numpy.add(box["minimum"], box["maximum"]) / 2).tolist()), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), tuple((numpy.subtract(box["maximum"], box["minimum"]) / 2).tolist()))
    for kind, box in boxes
  ]

  # The nav's parameters are map_edit's over the .map's collidable extents: 512 north to south makes two tiles of 409.6.
  nav = serverMapFiles.readNav(paths["nav"].read_bytes())
  assert nav["parameters"] == expectedNavParameters(collision)
  assert (nav["parameters"]["maximumTiles"], len(nav["tiles"])) == (2, 2)

  # Each dais holds two NPC islands, listed with the main piece around the safe point and the probe to the zone line: its top, and its
  # hollow inside, where Recast finds the dais's bottom face under 8 of open height, more than the NPC's 6.55, as it does in any closed
  # mesh. Nav polygons lie 0.2 over what they cover.
  summary = checked["serverFiles"]["nav"]
  assert summary["mainPiece"]["standIn"] is False and summary["islandCount"] == 4 and summary["islandsAtSnapRisk"] == 4
  placed = sorted(
    (name, island["center"][2]) for island in summary["largestIslands"] for name, (x, y) in daises.items()
    if abs(island["center"][0] - x) < daisHalfWidth and abs(island["center"][1] - y) < daisHalfWidth
  )
  assert placed == [("dais", 0.2), ("dais", daisHeight + 0.2), ("dais.001", 0.2), ("dais.001", daisHeight + 0.2)]
  assert [(probe["name"], probe["reached"]) for probe in summary["probes"]] == [("ATP_1_north", True)]
  islands = [finding for finding in checked["findings"] if finding["finding"] == "NPC islands"]
  assert [(finding["islands"], finding["atSnapRisk"]) for finding in islands] == [(4, 4)]

  # The server's view from the check folder's files alone.
  zone = eqgFiles.parseZone(archive.read("servertest.zon"), "servertest.zon")
  terrain = next(placement["model"] for placement in zone["placements"] if placement["model"].endswith(".ter"))
  wallTriangles = int((eqgFiles.parseModel(archive.read(terrain), terrain)["triangleMaterials"] == -1).sum())
  inspection = serverNav.inspectNav(paths["nav"].read_bytes(), safe["safePoint"][:3], [], stageBlenderServer.toolingRoot, noProgress)
  plan = serverMapDrawing.drawServerPlan(
    tmp_path / "serverPlan.png", mapContent, records, [region["name"] for region in zone["regions"]], inspection,
    [{"at": safe["safePoint"][:2], "label": "safe point"}],
  )
  frame = planDrawing.PlanFrame(plan["frame"]["center"], plan["frame"]["width"], plan["frame"]["size"])
  pixels = numpy.asarray(Image.open(tmp_path / "serverPlan.png").convert("RGB"), dtype=numpy.int64)

  def pixelAt(point):
    x, y = frame.pixel(point)
    return pixels[int(y), int(x)]
  # Open ground is the main piece's green over the grey relief, a dais's top (beside its number) an island's orange.
  ground, dais = pixelAt((12, 60)), pixelAt(numpy.add(daises["dais"], -4))
  assert ground[1] > ground[0] and ground[1] > ground[2], ground
  assert dais[0] > dais[1] > dais[2], dais
  # The zone line's box is outlined in its green across its south side, three pixels wide.
  x, y = frame.pixel((northLine["minimum"][0], 0))
  assert [tuple(color) for color in pixels[int(y) - 3:int(y) + 4, int(x)]].count(planDrawing.zoneLineColor) == 3
  # The boundary wall, which covers no pixel seen from above, is a line three pixels wide along its length, and nothing past its end.
  # The upright triangles are the wall's and the daises' sides (two daises, four sides of two triangles each).
  for along, drawn in ((-140, 3), (10, 3), (140, 3), (230, 0)):
    x, y = frame.pixel((along, -90))
    assert [tuple(color) for color in pixels[int(y), int(x) - 6:int(x) + 7]].count(serverMapDrawing.uprightColor) == drawn, along
  assert plan["legend"][:5] == [
    f"collision (.map): {len(collision):,} triangles, lighter higher", f"upright collision (.map): {wallTriangles + 2 * 8} triangles, as lines",
    "never collided with (.map): 12 triangles", "Water boxes (.wtr): 1", "ZoneLine boxes (.wtr): 1",
  ]
  assert plan["legend"][6] == "4 NPC islands, 4 at snap risk; players' reach not checked" and plan["islandNumbersWritten"] == 4


def testCheckExportGameSaysWhyServerFilesWereNotBuilt(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "servertest.eqg"

  async def steps(session):
    await serverZone(session, tmp_path, playerValues=False)
    gapsOnly = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "bare", "size": [4, 4, 4], "location": [0, 60, 0]})
    await session.expectSuccess("assignMaterial", {"objectName": "bare", "materialName": "stone"})
    await session.expectSuccess("saveFile", {})
    hard = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})
    hardLeftNoCheck = not checkFolderOf(stageBlenderServer).exists()
    await session.expectSuccess("projectUVs", {"objectName": "bare", "method": "box", "worldUnitsPerRepeat": 8})
    await session.expectSuccess("transformObjects", {"names": ["bare"], "translate": [0, 0, -16000]})
    await session.expectSuccess("saveFile", {})
    deep = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})
    return gapsOnly, hard, hardLeftNoCheck, deep

  gapsOnly, hard, hardLeftNoCheck, deep = stageBlenderServer.session(steps)
  assert [failure["failure"] for failure in gapsOnly["failures"]] == ["safe point or underworld missing", "containment not checked"]
  nav = gapsOnly["serverFiles"]["nav"]
  assert gapsOnly["serverFiles"]["built"] is True and nav["mainPiece"]["standIn"] is True and nav["islandCount"] == 4
  assert [(probe["name"], probe["result"]) for probe in nav["probes"]] == [("ATP_1_north", "noSafePoint")]
  assert [finding["message"] for finding in gapsOnly["findings"] if finding["finding"] == "NPC nav"] == [
    "No safe point: islands are counted against the largest component, which stands in for the main piece",
    "NPC paths were not probed, as there is no safe point to start from: to ATP_1_north",
  ]
  assert [failure["failure"] for failure in hard["failures"]] == ["no texture coordinates", "safe point or underworld missing", "containment not checked"]
  assert hard["serverFiles"] == {"built": False, "message": "server files not built: the checks found an unsaved file or what no zone file can hold: ['no texture coordinates (bare)']"}
  assert hardLeftNoCheck
  # A vertex at or below z -15000, whose triangles map_edit drops quietly, is a nav the helper refuses: a failure saying why, and the
  # .map and .wtr written without a .nav.
  assert [failure["failure"] for failure in deep["failures"]] == ["safe point or underworld missing", "containment not checked", "nav not built"]
  refused = deep["failures"][-1]["message"]
  assert refused.startswith("The server's nav mesh cannot be built: recastHelper nav: collidable triangle ") and "at or below z -15000" in refused, refused
  folder = checkFolderOf(stageBlenderServer)
  maps = folder / "server" / "maps"
  assert deep["serverFiles"]["nav"] is None and deep["serverFiles"]["files"] == {"map": str(maps / "base" / "servertest.map"), "water": str(maps / "water" / "servertest.wtr")}
  assert sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()) == [
    "server/maps/base/servertest.map", "server/maps/water/servertest.wtr", "servertest.eqg", "servertest_export.json",
  ]


def testExportRefusesAPathInsideTheClientFolder(installedLocalAppData, tmp_path):
  client = tmp_path / "EverQuest"
  clientMap = client / "maps" / "base" / "servertest.map"
  clientMap.parent.mkdir(parents=True)
  clientMap.write_bytes(b"the client folder's map")
  (client / "eqgame.exe").write_bytes(b"")
  junction(tmp_path / "clientLink", client)
  (tmp_path / "staging").mkdir()
  (tmp_path / "linked").mkdir()
  junction(tmp_path / "linked" / "server", client)
  server = StagedServer(tmp_path / "staged", {"blender": pinnedBlender, "extensions": {}}, installedLocalAppData, clientFolder=client)
  loopback = "\\\\localhost\\" + client.drive[0] + "$" + str(client)[len(client.drive):]
  # The client folder by a junction, an extended-length prefix, a UNC loopback share and the two together, and a staging folder whose
  # server folder is a junction to it, where a test export would remove the client folder's map.
  inside = [
    client / "servertest.eqg", tmp_path / "clientLink" / "servertest.eqg", Path("\\\\?\\" + str(client)) / "servertest.eqg",
    Path(loopback) / "servertest.eqg", Path("\\\\?\\UNC\\" + loopback[2:]) / "servertest.eqg", tmp_path / "linked" / "servertest.eqg",
  ]
  outside = client / ".." / "staging" / "servertest.eqg"

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 16, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(writePNG(tmp_path / "grass.png", 4, 4, (90, 120, 60, 255)))})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "servertest.blend")})
    refusals = [await session.expectError("exportZone", {"path": str(path), "purpose": "test"}) for path in inside]
    refusals.append(await session.expectError("checkExport", {"path": str(client / "servertest.eqg"), "purpose": "game"}))
    staged = await session.expectSuccess("exportZone", {"path": str(outside), "purpose": "test"})
    return refusals, staged

  try:
    refusals, staged = server.session(steps)
    notTheClient = server.callToolExpectingError(
      "exportZone", {"path": str(tmp_path / "staging" / "servertest.eqg"), "purpose": "test"},
      environment={"LOCALAPPDATA": str(server.localAppData), "EVERQUEST_CLIENT": str(tmp_path / "staging")},
    )
  finally:
    server.close()
  paths = [*inside, client / "servertest.eqg"]
  firstInside = [*inside[:5], tmp_path / "linked" / "server" / "maps" / "base" / "servertest.map", client / "servertest.eqg"]
  assert len(refusals) == 7
  for refusal, path, first in zip(refusals, paths, firstInside):
    assert f"An export to '{path}' writes or removes '{first}', which lies inside the EverQuest client folder ({client})" in refusal, refusal
  # '..' is taken before the check: the client folder's parent's staging folder is outside it.
  assert staged["path"] == str(outside) and (tmp_path / "staging" / "servertest.eqg").is_file()
  assert sorted(path.relative_to(client).as_posix() for path in client.rglob("*")) == ["eqgame.exe", "maps", "maps/base", "maps/base/servertest.map"]
  assert clientMap.read_bytes() == b"the client folder's map"
  assert f"EVERQUEST_CLIENT '{tmp_path / 'staging'}' has no eqgame.exe" in notTheClient


def testExportRefusesAShortNameOverThirtyOneCharacters(stageBlenderServer, tmp_path):
  async def steps(session):
    await serverZone(session, tmp_path)
    tooLong = await session.expectError("exportZone", {"path": str(tmp_path / f"{'a' * 32}.eqg"), "purpose": "test"})
    longest = await session.expectSuccess("exportZone", {"path": str(tmp_path / f"{'a' * 31}.eqg"), "purpose": "test"})
    return tooLong, longest

  tooLong, longest = stageBlenderServer.session(steps)
  assert f"Zone name '{'a' * 32}' is 32 characters; a zone's short name holds at most 31" in tooLong
  assert longest["zone"] == "a" * 31 and (tmp_path / f"{'a' * 31}.eqg").is_file() and not (tmp_path / f"{'a' * 32}.eqg").exists()


def testATestExportRemovesTheZonesServerFiles(stageBlenderServer, tmp_path):
  exportFolder = tmp_path / "export"
  exportFolder.mkdir()
  others = {exportFolder / "server" / "maps" / kind / f"otherzone.{extension}": f"otherzone's {extension}".encode("ascii") for kind, extension in (("base", "map"), ("water", "wtr"), ("nav", "nav"))}

  async def steps(session):
    await serverZone(session, tmp_path)
    await session.expectSuccess("checkExport", {"path": str(exportFolder / "servertest.eqg"), "purpose": "game"})
    shutil.copytree(checkFolderOf(stageBlenderServer) / "server", exportFolder / "server")
    for path, data in others.items():
      path.write_bytes(data)
    return await session.expectSuccess("exportZone", {"path": str(exportFolder / "servertest.eqg"), "purpose": "test"})

  exported = stageBlenderServer.session(steps)
  ours = [exportFolder / "server" / "maps" / kind / f"servertest.{extension}" for kind, extension in (("base", "map"), ("water", "wtr"), ("nav", "nav"))]
  assert exported["serverFilesRemoved"] == [str(path) for path in ours]
  assert not any(path.exists() for path in ours) and all(path.read_bytes() == data for path, data in others.items())
  manifest = json.loads((exportFolder / "servertest_export.json").read_text(encoding="ascii"))
  assert manifest["purpose"] == "test" and sorted(manifest["files"]) == ["servertest.eqg"]
  assert sorted(path.relative_to(exportFolder).as_posix() for path in exportFolder.rglob("*") if path.is_file()) == sorted(
    ["servertest.eqg", "servertest_export.json"] + [path.relative_to(exportFolder).as_posix() for path in others]
  )


def testAFailedWritePutsEveryFileBack(stageBlenderServer, tmp_path):
  exportFolder = tmp_path / "export"
  exportFolder.mkdir()
  archivePath = exportFolder / "servertest.eqg"
  serverFiles = {kind: exportFolder / "server" / "maps" / kind / f"servertest.{extension}" for kind, extension in (("base", "map"), ("water", "wtr"), ("nav", "nav"))}

  def files():
    return {path.relative_to(exportFolder).as_posix(): path.read_bytes() for path in exportFolder.rglob("*") if path.is_file()}

  async def steps(session):
    await serverZone(session, tmp_path)
    await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    for kind in ("base", "water"):
      serverFiles[kind].parent.mkdir(parents=True)
      serverFiles[kind].write_bytes(f"{kind} from an earlier game export".encode("ascii"))
    serverFiles["nav"].mkdir(parents=True)
    (serverFiles["nav"] / "kept.txt").write_bytes(b"a file in a folder named as the nav")
    before = files()
    await session.expectSuccess("transformObjects", {"names": ["dais"], "translate": [0, -20, 0]})
    await session.expectSuccess("saveFile", {})
    failed = await session.expectError("exportZone", {"path": str(archivePath), "purpose": "test"})
    after = files()
    left = sorted(path.name for path in exportFolder.rglob("*") if path.suffix in (".partial", ".previous"))
    navStillAFolder = serverFiles["nav"].is_dir()
    shutil.rmtree(serverFiles["nav"])
    retried = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    return before, failed, after, left, navStillAFolder, retried, files()

  before, failed, after, left, navStillAFolder, retried, released = stageBlenderServer.session(steps)
  # A folder where the nav goes refuses the export before anything is written.
  assert f"IsADirectoryError: {serverFiles['nav']} is a folder, where the export writes or removes a file" in failed
  assert after == before and left == [] and navStillAFolder
  assert retried["serverFilesRemoved"] == [str(serverFiles["base"]), str(serverFiles["water"])]
  assert sorted(released) == ["servertest.eqg", "servertest_export.json"] and released["servertest.eqg"] != before["servertest.eqg"]


def folderContents(folder):
  """Every file and folder under folder, each file with its bytes."""
  return {path.relative_to(folder).as_posix(): path.read_bytes() if path.is_file() else None for path in folder.rglob("*")}


def lastExport(folder):
  """A folder holding a last export's archive, emitter list, housing list, and manifest, and nothing under server."""
  folder.mkdir()
  for name in ("z.eqg", "z_EnvironmentEmitters.txt", "z_housing.json", "z_export.json"):
    (folder / name).write_bytes(f"last {name}".encode("ascii"))
  return folder


def nextExport(folder):
  """The next export's other files: a new emitter list, no housing list, the server's maps in folders not made yet, and a new manifest
  (the last the export puts in place before the archive)."""
  serverFiles = exportPipeline.serverFilePaths(folder, "z")
  return {
    folder / "z_EnvironmentEmitters.txt": b"next emitters", folder / "z_housing.json": None,
    serverFiles["map"]: b"next map", serverFiles["water"]: b"next wtr", serverFiles["nav"]: b"next nav", folder / "z_export.json": b"next manifest",
  }


def testAFailureBeforeTheArchivePutsEveryFileBack(tmp_path):
  folder = lastExport(tmp_path / "export")
  before = folderContents(folder)
  # A program holding the manifest's temporary file open, as Python opens files (shared for reading and writing, not for renaming),
  # lets the export write it and stops it being put in place: every other file is in place by then, and the archive is not.
  held = folder / "z_export.json.partial"
  held.write_bytes(b"")
  with held.open("rb"):
    with pytest.raises(PermissionError) as raised:
      exportPipeline.replaceExportFiles(folder / "z.eqg", b"next archive", nextExport(folder))
    during = folderContents(folder)
  assert f"{held!s}' -> '{folder / 'z_export.json'!s}'" in str(raised.value).replace("\\\\", "\\")
  # Every file is back and the server folders made for the maps are gone; only the held file stays, holding what was written into it.
  assert during == before | {"z_export.json.partial": b"next manifest"}
  notes = raised.value.__notes__
  assert len(notes) == 1 and notes[0].startswith("Not put back: PermissionError: [WinError 32]") and ";" not in notes[0], notes
  assert notes[0].replace("\\\\", "\\").endswith(f"'{held}'")


def testAFailureTakingAFileAsidePutsEveryFileBack(tmp_path):
  folder = lastExport(tmp_path / "export")
  before = folderContents(folder)
  # Held open, the last export's manifest cannot be taken aside, the last file to be; the two before it go back, and nothing is left
  # that could not be put back.
  with (folder / "z_export.json").open("rb"):
    with pytest.raises(PermissionError) as raised:
      exportPipeline.replaceExportFiles(folder / "z.eqg", b"next archive", nextExport(folder))
  assert f"{folder / 'z_export.json'!s}' -> '{folder / 'z_export.json.previous'!s}'" in str(raised.value).replace("\\\\", "\\")
  assert folderContents(folder) == before and not hasattr(raised.value, "__notes__")


def testWhatCannotBePutBackRefusesBeforeAnythingIsWritten(tmp_path):
  def refusal(folder):
    before = folderContents(folder)
    with pytest.raises(OSError) as raised:
      exportPipeline.replaceExportFiles(folder / "z.eqg", b"next archive", nextExport(folder))
    assert folderContents(folder) == before
    return f"{type(raised.value).__name__}: {raised.value}"

  # Windows renames a read-only file but will not remove it: refused every time, so no attempt leaves a file taken aside.
  readOnly = lastExport(tmp_path / "readOnly")
  os.chmod(readOnly / "z_EnvironmentEmitters.txt", stat.S_IREAD)
  try:
    attempts = [refusal(readOnly), refusal(readOnly)]
  finally:
    os.chmod(readOnly / "z_EnvironmentEmitters.txt", stat.S_IREAD | stat.S_IWRITE)
  assert attempts == [f"PermissionError: {readOnly / 'z_EnvironmentEmitters.txt'} is read-only, where the export replaces or removes a file"] * 2
  # A file an export that did not finish took aside may be the only copy of the last export's file.
  unfinished = lastExport(tmp_path / "unfinished")
  (unfinished / "z_export.json.previous").write_bytes(b"the manifest before last")
  assert refusal(unfinished) == (
    f"FileExistsError: {unfinished / 'z_export.json.previous'} is left from an export that did not finish and may hold the last export's file:"
    " put it back as z_export.json or remove it"
  )
  archiveFolder = lastExport(tmp_path / "archiveFolder")
  (archiveFolder / "z.eqg").unlink()
  (archiveFolder / "z.eqg").mkdir()
  assert refusal(archiveFolder) == f"IsADirectoryError: {archiveFolder / 'z.eqg'} is a folder, where the export writes or removes a file"
  serverFile = lastExport(tmp_path / "serverFile")
  (serverFile / "server").write_bytes(b"a file named server")
  assert refusal(serverFile) == f"NotADirectoryError: {serverFile / 'server'} is a file, where the export needs a folder"


def recordOf(path):
  data = path.read_bytes()
  return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def testTheManifestListsEveryFileWithItsHash(stageBlenderServer, tmp_path):
  exportFolder = tmp_path / "export"
  exportFolder.mkdir()

  async def steps(session):
    await serverZone(session, tmp_path)
    await session.expectSuccess("placeEmitters", {"emitters": [{"name": "campfire01", "position": [5, 5, 1], "definition": 259, "lifespan": 4000000}]})
    await session.expectSuccess("saveFile", {})
    exported = await session.expectSuccess("exportZone", {"path": str(exportFolder / "servertest.eqg"), "purpose": "test"})
    checked = await session.expectSuccess("checkExport", {"path": str(exportFolder / "servertest.eqg"), "purpose": "game"})
    return exported, checked

  exported, checked = stageBlenderServer.session(steps)
  manifestPath = exportFolder / "servertest_export.json"
  assert exported["manifest"] == str(manifestPath)
  assert json.loads(manifestPath.read_text(encoding="ascii")) == {
    "purpose": "test", "shortName": "servertest", "blend": str(tmp_path / "servertest.blend"),
    "files": {name: recordOf(exportFolder / name) for name in ("servertest.eqg", "servertest_EnvironmentEmitters.txt")},
    "failures": 0, "findings": len(exported["findings"]),
  }

  folder = checkFolderOf(stageBlenderServer)
  manifest = json.loads((folder / "servertest_export.json").read_text(encoding="ascii"))
  listed = {path.relative_to(folder).as_posix(): recordOf(path) for path in folder.rglob("*") if path.is_file() and path.name != "servertest_export.json"}
  assert sorted(listed) == [
    "server/maps/base/servertest.map", "server/maps/nav/servertest.nav", "server/maps/water/servertest.wtr", "servertest.eqg", "servertest_EnvironmentEmitters.txt",
  ]
  assert manifest["files"] == listed
  assert (manifest["purpose"], manifest["shortName"], manifest["blend"]) == ("game", "servertest", str(tmp_path / "servertest.blend"))
  assert (manifest["failures"], manifest["findings"]) == (len(checked["failures"]), len(checked["findings"]))
  assert manifest["serverNavSettings"] == serverNav.serverNavSettings
  assert manifest["recastHelper"] == {"commit": pinnedRecast["commit"], "fingerprint": recastHelper.helperFingerprint(pinnedRecast, recastHelper.toolchain()[1])}
  summary = checked["serverFiles"]["nav"]
  assert {key: value for key, value in manifest["nav"].items() if key != "islands"} == {key: value for key, value in summary.items() if key != "largestIslands"}
  assert manifest["nav"]["islands"] == summary["largestIslands"] and len(manifest["nav"]["islands"]) == summary["islandCount"] == 4


def testAGameExportNeedsTheRecastHelper(stageServer, tmp_path):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  refusals = [server.callToolExpectingError(tool, {"path": str(tmp_path / "servertest.eqg"), "purpose": "game"}) for tool in ("checkExport", "exportZone")]
  assert all("The Recast helper is missing" in refusal and "run syncTooling to build it" in refusal for refusal in refusals), refusals
