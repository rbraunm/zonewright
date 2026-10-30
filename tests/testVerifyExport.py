import base64
import functools
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import numpy
import pytest
from PIL import Image

from conftest import StagedServer, pinnedBlender, serverRowValues, wholeResult, writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import buildTolerances
import eqArchive
import exportVerification
import eqgFiles
import eqgWriter
import planDrawing
import serverMapDrawing
import serverMapFiles
import serverNav
import serverRows
import playerScale

# The medium body group, which holds the walked human male, is the one these tests size their courses by.
medium = playerScale.groups["medium"]
playerHeight, stepHeight = medium.playerHeight, medium.stepHeight
from testModelsAndDressing import freshScene
from testReach import cube, rectangle, region

short = "verified"
environment = {
  "ambientColor": [0.3, 0.3, 0.35], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.15], "sunColor": [0.6, 0.5, 0.4],
  "sunAzimuthDegrees": 40, "sunElevationDegrees": 35, "fogColor": [0.5, 0.55, 0.6], "fogStart": 100, "fogEnd": 3000, "fogDensity": 0.1,
  "fogOn": False, "minClip": 50, "maxClip": 4000, "sky": "none", "newEngineZone": False,
}
# 240 north to south and 96 across, so the safe point with its axes swapped lies off the ground.
halfLength, halfWidth = 120.0, 48.0
safePoint = [80, 20, 0, 0]
wallHeight = stepHeight + 2 * playerHeight
otherZone = {"zone": "qeynos2", "x": 10, "y": 20, "z": 3, "headingDegrees": 90}
northLine = {"minimum": [104, -10, -5], "maximum": [116, 10, 20]}
loopLine = {"minimum": [-116, -10, -5], "maximum": [-104, 10, 20]}
loopLanding = [-60, -20, 0]
pond = {"minimum": [-40, -40, -10], "maximum": [-20, -24, 2]}
fromQeynos = {"name": "fromQeynos", "at": [-40, 30], "headingDegrees": 90, "kind": "zoneIn", "fromZone": "qeynos2", "fromNumber": 3}
overview = {"standAt": [-70, 0], "headingDegrees": 90, "pitchDegrees": -5}
checkNames = ["manifest", "current", "archive", "map", "water", "nav", "rows", "ground", "clientModels", "containment", "looks"]
# How often, a quarter second apart, the scene's status is read after a cancelled verifyExport until the work file is open again.
reopenPolls = 240


async def referenceArchive(session, folder):
  """A small zone exported for a test load, to stand in the verified zone as an imported reference zone: ground lifted 20 over the
  origin with a pillar on it."""
  await freshScene(session)
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [32, 32], "spacing": 8, "location": [0, 0, 20], "collection": "terrain"})
  await session.expectSuccess("createMaterial", {"name": "slate", "diffuseTexture": str(writePNG(folder / "slate.png", 4, 4, (60, 70, 140, 255)))})
  await cube(session, "pillar", [-4, -4, 20], [4, 4, 36])
  for name in ("ground", "pillar"):
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "slate"})
    await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("setZoneProperties", environment)
  await session.expectSuccess("saveFile", {"path": str(folder / "reference.blend")})
  await session.expectSuccess("exportZone", {"path": str(folder / "reference.eqg"), "purpose": "test"})
  return folder / "reference.eqg"


async def verifiedZone(session, folder):
  """Flat ground walled all round with a crate, a swim box standing alone, a zone line to qeynos2 and one leading back into the zone, a
  zoneIn entry naming its neighbour's row, one play region, and the zone row's values; an imported reference zone and a placed client
  object stand in view of a review camera. Saved as verified.blend in folder; returns the reference zone's archive."""
  reference = await referenceArchive(session, folder)
  await freshScene(session)
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [2 * halfLength, 2 * halfWidth], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("createMaterial", {
    "name": "grass", "diffuseTexture": str(writePNG(folder / "grass.png", 4, 4, (90, 120, 60, 255))),
    "normalTexture": str(writePNG(folder / "grass_n.png", 4, 4, (192, 128, 192, 255))),
  })
  await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(writePNG(folder / "stone.png", 4, 4, (120, 110, 100, 255)))})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
  x, y = halfLength - 0.1, halfWidth - 0.1
  for name, path in {"westWall": [[-x, y], [x, y]], "northWall": [[x, y], [x, -y]], "eastWall": [[x, -y], [-x, -y]], "southWall": [[-x, -y], [-x, y]]}.items():
    await session.expectSuccess("placeBoundaryWall", {"name": name, "path": path, "height": wallHeight})
  await cube(session, "crate", [30, -30, 0], [38, -22, 8])
  await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "stone"})
  await session.expectSuccess("projectUVs", {"objectName": "crate", "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("placeSwimVolume", {"name": "pond", "liquid": "water"} | pond)
  await session.expectSuccess("setZoneProperties", environment | serverRowValues(short) | {"underworld": -50, "safePoint": safePoint})
  await session.expectSuccess("placeZoneLine", {"number": 1, "label": "north", "target": otherZone} | northLine)
  loop = {"zone": short, "x": loopLanding[0], "y": loopLanding[1], "z": loopLanding[2], "headingDegrees": 0}
  await session.expectSuccess("placeZoneLine", {"number": 2, "label": "loop", "target": loop} | loopLine)
  await region(session, "yard", rectangle([-halfLength, -halfWidth], [halfLength, halfWidth]), "play", bottom=-20, top=40)
  await session.expectImage("placeEntry", fromQeynos)
  await session.expectSuccess("importZoneFile", {"path": str(reference)})
  await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "kiln", "location": [40, 10, 0], "headingDegrees": 0, "interactive": True, "objectType": "potteryWheel"})
  await session.expectSuccess("saveReviewCamera", {"name": "overview", "view": overview, "note": "the yard, the kiln, and the reference zone"})
  await session.expectSuccess("saveFile", {"path": str(folder / f"{short}.blend")})
  return reference


async def verified(session, path):
  """verifyExport's pictures (each its bytes and type) and result."""
  result, _ = await session.call("verifyExport", {"path": str(path)})
  texts = [content.text for content in result.content if content.type == "text"]
  assert result.is_error is False, texts
  pictures = [(base64.b64decode(content.data), content.mime_type) for content in result.content if content.type == "image"]
  return pictures, wholeResult(texts[-1])


def byCheck(result):
  return {check["check"]: check for check in result["checks"]}


@pytest.fixture(scope="module")
def exportedZone(warmServer, tmp_path_factory):
  """The verified zone saved and exported for the game once, for the tests that break a copy of its export."""
  folder = tmp_path_factory.mktemp("verifiedZone")
  export = folder / "export"
  export.mkdir()

  async def steps(session):
    reference = await verifiedZone(session, folder)
    await session.expectSuccess("exportZone", {"path": str(export / f"{short}.eqg"), "purpose": "game"})
    return reference

  reference = warmServer.session(steps)
  return SimpleNamespace(folder=folder, export=export, blend=folder / f"{short}.blend", reference=reference)


def copiedExport(exportedZone, tmp_path):
  """A copy of the zone's export to break, its manifest still naming the zone's .blend."""
  copy = tmp_path / "export"
  shutil.copytree(exportedZone.export, copy)
  return copy


def verifyCopy(server, exportedZone, copy):
  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend), "discardUnsavedChanges": True})
    return await verified(session, copy / f"{short}.eqg")
  return server.session(steps)


def pixels(data):
  return numpy.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=numpy.int64)


def testVerifyExportPassesAFreshGameExport(stageBlenderServer, exportedZone):
  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    # The kiln and the reference zone stand in the review camera's view, drawn as the scene stands.
    seen, _ = await session.expectImage("renderView", {"view": {"camera": "overview"}})
    _, labelled = await session.expectImage("renderView", {"view": {"camera": "overview"}, "labels": ["kiln", "reference"]})
    pictures, result = await verified(session, exportedZone.export / f"{short}.eqg")
    return seen, labelled, pictures, result

  seen, labelled, pictures, result = stageBlenderServer.session(steps)
  assert sorted(place["object"] for place in labelled["labels"]["shown"]) == ["kiln", "reference"]
  assert result["passed"] is True and result["failedChecks"] == [] and [check["check"] for check in result["checks"]] == checkNames
  checks = byCheck(result)
  assert checks["manifest"]["details"]["listed"] == 7
  # The kiln is a row of the object table naming the copy of its model in the archive, standing where the server stands it.
  assert checks["clientModels"]["details"] == {"copies": 1, "doors": 0, "objects": 1, "failures": []}
  assert checks["current"]["details"]["identical"] is True
  # The ground's normal map stays in the work file: the archive's terrain draws its diffuse alone, and the looks below agree.
  terrainName = f"ter_{short}.ter"
  terrain = eqgFiles.parseModel(eqArchive.EQArchive(exportedZone.export / f"{short}.eqg").read(terrainName), terrainName)
  grass = next(material for material in terrain["materials"] if material["name"] == "grass")
  assert grass["shader"] == eqgWriter.shaderOpaque and "e_TextureNormal0" not in grass["properties"]
  assert checks["archive"]["details"]["regions"] == ["AWT_pond", "ATP_1_north", "ATP_2_loop"]
  collision = checks["map"]["details"]
  assert collision["collisionTriangles"] == collision["archiveSolidTriangles"] > 0 and collision["largestDifference"] <= 3.2e-4
  # The nav's water polygons, which must lie in the pond's box, are there to be judged.
  maps = exportedZone.export / "server" / "maps"
  inspection = serverNav.inspectNav((maps / "nav" / f"{short}.nav").read_bytes(), safePoint[:3], [], stageBlenderServer.toolingRoot, lambda *arguments: None)
  water = [polygon for tile in inspection["tiles"] for polygon in tile["polygons"] if polygon["area"] == 1]
  assert water and all(pond["minimum"][0] - 1 < x < pond["maximum"][0] + 1 and pond["minimum"][1] - 1 < y < pond["maximum"][1] + 1 for polygon in water for x, y in polygon["outline"])
  assert checks["rows"]["details"] == {"zonePoints": [10, 20], "guards": 7, "waysIn": True, "failures": []}
  ground = checks["ground"]["details"]
  assert [(place["arrival"], place["at"]) for place in ground["places"]] == [
    ("safe point", [float(value) for value in safePoint[:3]]), ("fromQeynos", [-40.0, 30.0, 0.0]), ("zone_points 20", loopLanding),
  ]
  assert [(landed["zonePoint"], landed["zone"]) for landed in ground["otherZones"]] == [(10, "qeynos2")] and ground["otherZones"][0]["ground"] is not None
  lane = checks["containment"]["details"]
  every = dict.fromkeys(playerScale.groupNames, True)
  assert [(entry["name"], entry["connected"]) for entry in lane["entries"]] == [("safe point", every), ("fromQeynos", every)]
  assert lane["reachedArea"] == lane["manifestReachedArea"] and all(lane["reachedArea"][name]["walk"] > 0 for name in playerScale.groupNames)
  # Every review camera and the tour's eight eyes, the scene drawn without the kiln and the reference zone as the archive draws.
  views = checks["looks"]["details"]["views"]
  assert sorted(view["name"] for view in views) == ["overview"] + [f"tour {number}" for number in range(1, 9)]
  assert all(view["passed"] and (view["meanDifference"], view["percentileDifference"], view["changedShare"]) == (0, 0, 0) for view in views)
  camera = next(view for view in views if view["name"] == "overview")
  scene, archive = (numpy.asarray(Image.open(camera[key]).convert("RGB"), dtype=numpy.int64) for key in ("scene", "archive"))
  shown = numpy.abs(pixels(seen) - scene).max(axis=-1) > buildTolerances.roundTripPercentileLevels
  assert shown.mean() > 0.01 and numpy.abs(scene - archive).max() <= buildTolerances.roundTripPercentileLevels
  # Both pictures: the server's view, the comparison sheet of five rows, largest differences first.
  assert [kind for _, kind in pictures] == ["image/png", "image/jpeg"]
  view = result["serverView"]
  assert view["notDrawn"] == {} and view["legend"][2:4] == ["Water boxes (.wtr): 1", "ZoneLine boxes (.wtr): 2"]
  frame = planDrawing.PlanFrame(view["frame"]["center"], view["frame"]["width"], view["frame"]["size"])
  plan = pixels(pictures[0][0])
  # The nav holds no island, so the legend names none, and no line of it runs into the sheet's right margin.
  assert not any("island" in text for text in view["legend"])
  assert (plan[view["frame"]["size"][1]:, -serverMapDrawing.panelGap:] == 255).all()
  for point in (safePoint[:2], [-40, 30], loopLanding[:2]):
    x, y = frame.pixel(point)
    assert tuple(plan[int(y), int(x)]) == (255, 255, 255), point
  comparison = result["comparison"]
  assert (comparison["shown"], comparison["views"]) == (5, 9) and [len(sheet["views"]) for sheet in comparison["sheets"]] == [5]
  assert comparison["sheets"][0]["caption"] == (
    "5 of 9 views shown, those that draw otherwise first, then by mean difference, largest first. A view draws alike when its mean"
    " difference is under 0.5 levels, its 99th percentile at most 2 levels, and under 0.1% of its pixels are over 8 levels off."
  )


def testVerifyExportCatchesAMovedSwimBox(stageBlenderServer, tmp_path):
  folder = tmp_path / "export"
  folder.mkdir()

  async def steps(session):
    await verifiedZone(session, tmp_path)
    await session.expectSuccess("exportZone", {"path": str(folder / f"{short}.eqg"), "purpose": "game"})
    await session.expectSuccess("transformObjects", {"names": ["AWT_pond"], "translate": [10, 0, 0]})
    await session.expectSuccess("saveFile", {})
    return await verified(session, folder / f"{short}.eqg")

  pictures, result = stageBlenderServer.session(steps)
  assert result["failedChecks"] == ["current", "archive"]
  checks = byCheck(result)
  assert checks["current"]["details"]["failures"] == [
    {"entry": f"{short}.zon", "change": "differs", "message": f"{short}.zon differs: the scene changed since this export"},
  ]
  moved = checks["archive"]["details"]["failures"]
  assert [(failure["region"], failure["center"]) for failure in moved] == [("AWT_pond", [-30.0, -32.0, -4.0])]
  assert moved[0]["message"] == "Region AWT_pond stands at [-30.0, -32.0, -4.0] with half extents [10.0, 8.0, 6.0] in the .zon, at [-20.0, -32.0, -4.0] with [10.0, 8.0, 6.0] in the scene"
  assert len(pictures) == 2


def testVerifyExportCatchesAViewThatDrawsOtherwise(stageBlenderServer, tmp_path):
  folder = tmp_path / "export"
  folder.mkdir()

  async def steps(session):
    await verifiedZone(session, tmp_path)
    await session.expectSuccess("exportZone", {"path": str(folder / f"{short}.eqg"), "purpose": "game"})
    await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [0, 12, 0]})
    await session.expectSuccess("saveFile", {})
    return await verified(session, folder / f"{short}.eqg")

  pictures, result = stageBlenderServer.session(steps)
  assert result["failedChecks"] == ["current", "looks"]
  checks = byCheck(result)
  assert [failure["entry"] for failure in checks["current"]["details"]["failures"]] == [f"{short}.zon"]
  # The views that hold the crate draw it where the scene has it and the archive does not; those that do not hold it pass. The sheet
  # leads with the largest difference.
  views = checks["looks"]["details"]["views"]
  failed = [failure["view"] for failure in checks["looks"]["details"]["failures"]]
  assert failed and failed == [view["name"] for view in views if not view["passed"]] and "overview" in failed
  assert views[0]["meanDifference"] == max(view["meanDifference"] for view in views) > buildTolerances.roundTripMeanLevels
  ordered = [(view["passed"], -view["meanDifference"]) for view in views]
  assert ordered == sorted(ordered)
  assert all(
    view["meanDifference"] < buildTolerances.roundTripMeanLevels and view["percentileDifference"] <= buildTolerances.roundTripPercentileLevels
    and view["changedShare"] < buildTolerances.roundTripChangedShare for view in views if view["passed"]
  )
  (tmp_path / "comparisonSheet.jpg").write_bytes(pictures[1][0])


def quietlyDiffering(views):
  """The views that draw otherwise though their mean and 99th percentile are within the round trip's tolerances."""
  return [
    view for view in views if not view["passed"] and view["meanDifference"] < buildTolerances.roundTripMeanLevels
    and view["percentileDifference"] <= buildTolerances.roundTripPercentileLevels
  ]


def assertEveryDifferingViewPictured(result, pictures):
  """More views draw otherwise than one comparison sheet holds: each is pictured, on a second sheet past the first's rows."""
  views = byCheck(result)["looks"]["details"]["views"]
  differing = [view["name"] for view in views if not view["passed"]]
  comparison = result["comparison"]
  rows = [len(sheet["views"]) for sheet in comparison["sheets"]]
  assert len(differing) > rows[0] and rows == [rows[0], len(differing) - rows[0]]
  assert [name for sheet in comparison["sheets"] for name in sheet["views"]] == differing
  assert [sheet["caption"].split(",")[0] for sheet in comparison["sheets"]] == [f"{len(differing)} of {len(views)} views shown (sheet {index} of 2)" for index in (1, 2)]
  assert [kind for _, kind in pictures] == ["image/png", "image/jpeg", "image/jpeg"]


def testVerifyExportCatchesARedCrateTexture(stageBlenderServer, tmp_path):
  folder = tmp_path / "export"
  folder.mkdir()

  async def steps(session):
    await verifiedZone(session, tmp_path)
    await session.expectSuccess("exportZone", {"path": str(folder / f"{short}.eqg"), "purpose": "game"})
    writePNG(tmp_path / "stone.png", 4, 4, (220, 30, 30, 255))
    await session.expectSuccess("openFile", {"path": str(tmp_path / f"{short}.blend")})
    return await verified(session, folder / f"{short}.eqg")

  pictures, result = stageBlenderServer.session(steps)
  assert result["failedChecks"] == ["current", "looks"]
  checks = byCheck(result)
  assert [failure["entry"] for failure in checks["current"]["details"]["failures"]] == ["stone.dds"]
  # Every view the crate shows in draws otherwise, some by the share of pixels changed alone; those without it draw alike.
  views = checks["looks"]["details"]["views"]
  assert [view["passed"] for view in views] == [view["changedShare"] == 0 for view in views] and not all(view["passed"] for view in views)
  assert quietlyDiffering(views) and all(view["changedShare"] >= buildTolerances.roundTripChangedShare for view in quietlyDiffering(views))
  assertEveryDifferingViewPictured(result, pictures)


def testVerifyExportCatchesAMissingPlacementModel(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  archivePath = copy / f"{short}.eqg"
  archive = eqArchive.EQArchive(archivePath, archivePath.read_bytes())
  data = eqgWriter.archiveBytes({name: archive.read(name) for name in archive.entries if name != "obj_crate.mod"})
  archivePath.write_bytes(data)
  for name in (f"{short}.sql", f"{short}_waysIn.sql"):
    rows = copy / "server" / "sql" / name
    rows.write_text(re.sub(r"-- archive sha256: [0-9a-f]{64}", f"-- archive sha256: {hashlib.sha256(data).hexdigest()}", rows.read_text(encoding="ascii")), encoding="ascii", newline="")
  rehashed(copy)
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  assert result["failedChecks"] == ["current", "archive", "map", "looks"]
  checks = byCheck(result)
  assert checks["current"]["details"]["failures"] == [
    {"entry": "obj_crate.mod", "change": "only in the scene's rebuild", "message": "obj_crate.mod only in the scene's rebuild: the scene changed since this export"},
  ]
  assert checks["archive"]["details"]["failures"] == [{
    "placement": "OBJ_crate01", "model": "obj_crate.mod", "at": [34.0, -26.0, 0.0],
    "message": "Placement 1 'OBJ_crate01' at [34.0, -26.0, 0.0] places obj_crate.mod, which the archive does not hold",
  }]
  assert [failure["message"] for failure in checks["map"]["details"]["failures"]] == [
    "The archive makes no .map to compare: ValueError: Placement 1 'OBJ_crate01' of obj_crate.mod: the zone's files hold no obj_crate.mod;"
    " azone would drop the placement and the server would have no collision for it",
  ]
  views = checks["looks"]["details"]["views"]
  assert [view["passed"] for view in views] == [view["changedShare"] == 0 for view in views] and not all(view["passed"] for view in views)
  # The missing crate fails the overview, and some view draws otherwise within the round trip's tolerances yet still fails.
  assert not next(view for view in views if view["name"] == "overview")["passed"] and quietlyDiffering(views)
  assertEveryDifferingViewPictured(result, pictures)


def testVerifyExportCatchesATruncatedWaterMap(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  water = copy / "server" / "maps" / "water" / f"{short}.wtr"
  water.write_bytes(water.read_bytes()[:-10])
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  assert result["failedChecks"] == ["manifest", "water", "nav", "containment"]
  checks = byCheck(result)
  told = checks["water"]["details"]["failures"]
  assert [failure["message"] for failure in told] == [
    'the .wtr: region 2 at byte 122 needs 52 bytes; 42 remain: the server would log "Loaded Water Map" and drop it (water_map.cpp:48-63),'
    " and without a water map turns NPC nav pathing off (mob_movement_manager.cpp:1035)",
  ]
  assert [failure["file"] for failure in checks["manifest"]["details"]["failures"]] == [f"server/maps/water/{short}.wtr"]
  # The server's view needs the .wtr; the comparison sheet is drawn.
  assert result["serverView"] == {"drawn": False, "why": "the .wtr does not read: the .wtr: region 2 at byte 122 needs 52 bytes; 42 remain"}
  assert [kind for _, kind in pictures] == ["image/jpeg"]


def testVerifyExportCatchesAMapFromAnotherArchive(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  other = serverMapFiles.mapBytes(serverMapFiles.zoneFilesOf(exportedZone.reference.read_bytes()))
  (copy / "server" / "maps" / "base" / f"{short}.map").write_bytes(other)
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  checks = byCheck(result)
  assert result["failedChecks"] == ["manifest", "map", "nav", "ground", "clientModels", "containment"]
  # The kiln's object row, standing on the zone's ground, would be stood elsewhere on the other zone's collision.
  assert [failure["row"] for failure in checks["clientModels"]["details"]["failures"]] == ["IT10800_ACTORDEF"]
  told = checks["map"]["details"]["failures"]
  assert len(told) == 1 and told[0]["message"].startswith("The .map is not the one the archive makes (serverMapFiles.mapBytes): this .map, then the archive's, hold")
  ours = serverMapFiles.readMap((exportedZone.export / "server" / "maps" / "base" / f"{short}.map").read_bytes())
  theirs = serverMapFiles.readMap(other)
  assert told[0]["counts"]["collidableIndices"] == {"map": len(theirs["collidableIndices"]), "archive": len(ours["collidableIndices"])}
  # The nav is judged against the .map it lies beside: its parameters and payload are not the other archive's.
  navTold = [failure["message"] for failure in checks["nav"]["details"]["failures"]]
  assert len(navTold) == 2 and navTold[0].startswith("The .nav's parameters ['origin'] are ")
  assert navTold[1].startswith("The pinned helper rebuilt on this .map and .wtr makes another nav")
  # On the other zone's collision the safe point stands over nothing.
  assert [failure["arrival"] for failure in checks["ground"]["details"]["failures"]] == ["safe point", "fromQeynos", "zone_points 20"]
  assert len(pictures) == 2


def testVerifyExportCatchesAMissingNav(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  (copy / "server" / "maps" / "nav" / f"{short}.nav").unlink()
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  assert result["failedChecks"] == ["manifest", "nav"]
  checks = byCheck(result)
  assert checks["manifest"]["details"]["failures"] == [{"file": f"server/maps/nav/{short}.nav", "message": f"server/maps/nav/{short}.nav is missing"}]
  assert checks["nav"]["details"]["failures"] == [{"message": "No .nav lies in the export: the server would path NPCs straight, through walls"}]
  # The server's view is drawn without a nav, and says so.
  assert result["serverView"]["drawn"] is True and "no NPC nav drawn: no .nav was read" in result["serverView"]["legend"]
  assert len(pictures) == 2


def testVerifyExportCatchesServerFilesNamedInAnotherCase(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  maps = copy / "server" / "maps"
  (maps / "base" / f"{short}.map").rename(maps / "base" / "Verified.map")
  (maps / "nav").rename(maps / "Nav")
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  # Windows finds both under their manifest names, so every other check reads them; the server on Linux would find neither.
  assert result["failedChecks"] == ["manifest"]
  assert byCheck(result)["manifest"]["details"]["failures"] == [
    {
      "file": "server/maps/base/verified.map",
      "message": "server/maps/base/verified.map lies as server/maps/base/Verified.map: the server on Linux opens files by their exact names and would find no server/maps/base/verified.map",
    },
    {
      "file": "server/maps/nav/verified.nav",
      "message": "server/maps/nav/verified.nav lies as server/maps/Nav/verified.nav: the server on Linux opens files by their exact names and would find no server/maps/nav/verified.nav",
    },
  ]
  assert len(pictures) == 2


def testVolumeFailuresFindNavPolygonsOutsideTheirBoxes(stageBlenderServer, exportedZone):
  maps = exportedZone.export / "server" / "maps"
  inspection = serverNav.inspectNav((maps / "nav" / f"{short}.nav").read_bytes(), safePoint[:3], [], stageBlenderServer.toolingRoot, lambda *arguments: None)
  records = serverMapFiles.readWater((maps / "water" / f"{short}.wtr").read_bytes())
  marked = [
    (list(tile["key"]), index, serverMapFiles.navAreaNames[polygon["area"]]) for tile in inspection["tiles"]
    for index, polygon in enumerate(tile["polygons"]) if polygon["area"] in serverNav.volumeAreas.values()
  ]
  water, disabled = ([place for place in marked if place[2] == area] for area in ("Water", "Disabled"))
  assert water and disabled and len(water) + len(disabled) == len(marked)
  assert exportVerification.volumeFailures(inspection, records) == []
  # The pond's box moved past its own length, its water polygons lie in no water box; without the zone lines' boxes, their Disabled
  # polygons lie in none.
  moved = [record | {"position": [record["position"][0] + 30, *record["position"][1:]]} if record["type"] == 1 else record for record in records]
  assert [(failure["tile"], failure["polygon"], failure["area"]) for failure in exportVerification.volumeFailures(inspection, moved)] == water
  unlined = [record for record in records if record["type"] != 3]
  assert [(failure["tile"], failure["polygon"], failure["area"]) for failure in exportVerification.volumeFailures(inspection, unlined)] == disabled


def testContainmentOnTheFilesTakesEachEntrysIsolation(stageBlenderServer, exportedZone):
  files = exportVerification.exportedFiles(exportedZone.export, short)
  manifest = json.loads((exportedZone.export / f"{short}_export.json").read_text(encoding="ascii"))

  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    return (await session.expectSuccess("getEntries", {}))["entries"], (await session.expectSuccess("getRegions", {}))["regions"]

  entries, regions = stageBlenderServer.session(steps)
  # A landing on the crate's top, which players drop from and never climb to.
  onCrate = {"name": "onCrate", "kind": "landing", "at": [34.0, -26.0, 8.0], "headingDegrees": 0.0, "source": "placeEntry", "fromZone": None, "fromNumber": None}

  def judged(landing):
    return exportVerification.containmentCheck(
      files["map"], files["water"], entries + [landing], regions, -50, None, manifest, stageBlenderServer.toolingRoot, lambda *arguments: None,
    )

  assert judged(onCrate | {"isolated": True})["passed"] is True
  joined = judged(onCrate | {"isolated": False})["details"]["failures"]
  assert [(failure["failure"], failure["entry"]) for failure in joined] == [("disconnectedEntry", "onCrate")]
  with pytest.raises(KeyError, match="isolated"):
    judged(onCrate)


def swappedSafeAxes(text):
  """<short>.sql with the zone row's safe_x and safe_y swapped in its insert and its update."""
  lines = text.split("\n")
  insert = next(index for index, line in enumerate(lines) if line.startswith("  INSERT INTO zone ("))
  head, rest = lines[insert].split(" SELECT ", 1)
  selected, tail = rest.split(" FROM zone AS ", 1)
  items = selected.split(", ")
  assert len(items) == len(serverRows.zoneColumns)
  first, second = serverRows.zoneColumns.index("safe_x"), serverRows.zoneColumns.index("safe_y")
  items[first], items[second] = items[second], items[first]
  lines[insert] = f"{head} SELECT {', '.join(items)} FROM zone AS {tail}"
  update = next(index for index, line in enumerate(lines) if line.startswith("  UPDATE zone SET "))
  lines[update] = re.sub(r"safe_x = ([^,]+), safe_y = ([^,]+),", r"safe_x = \2, safe_y = \1,", lines[update])
  return "\n".join(lines)


def testVerifyExportCatchesSwappedSafeAxes(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  rowsPath = copy / "server" / "sql" / f"{short}.sql"
  rowsPath.write_text(swappedSafeAxes(rowsPath.read_text(encoding="ascii")), encoding="ascii", newline="")
  swapped = serverRows.readRows(rowsPath.read_text(encoding="ascii"))
  assert (swapped["zoneUpdate"]["values"]["safe_x"], swapped["zoneUpdate"]["values"]["safe_y"]) == (safePoint[0], safePoint[1])
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  assert result["failedChecks"] == ["manifest", "rows", "ground"]
  checks = byCheck(result)
  columns = [(failure["file"], failure["column"], failure["found"], failure["expected"]) for failure in checks["rows"]["details"]["failures"]]
  assert columns == [
    (f"{short}.sql's zone insert", "safe_x", safePoint[0], safePoint[1]), (f"{short}.sql's zone insert", "safe_y", safePoint[1], safePoint[0]),
    (f"{short}.sql's zone update", "safe_x", safePoint[0], safePoint[1]), (f"{short}.sql's zone update", "safe_y", safePoint[1], safePoint[0]),
  ]
  fallen = checks["ground"]["details"]["failures"]
  assert [(failure["arrival"], failure["at"], failure["ground"]) for failure in fallen] == [("safe point", [20.0, 80.0, 0.0], None)]
  assert fallen[0]["message"].startswith("The safe point at [20.0, 80.0, 0.0] stands over no ground the server finds in the .map")
  assert len(pictures) == 2


def rehashed(copy):
  """The copy's manifest recording its files as they now lie, as an export that wrote them so would."""
  manifestPath = copy / f"{short}_export.json"
  manifest = json.loads(manifestPath.read_text(encoding="ascii"))
  manifest["files"] = {name: {"bytes": len((copy / name).read_bytes()), "sha256": hashlib.sha256((copy / name).read_bytes()).hexdigest()} for name in manifest["files"]}
  manifestPath.write_text(json.dumps(manifest), encoding="ascii")


def changedRows(text):
  """<short>.sql with zone_points 10 moved out of its box and leading elsewhere in qeynos2, zone_points 20 facing another way and
  shown to one client version, and the template guard dropped."""
  edits = [
    ("('verified', 0, 10, 110.0, 0.0, 7.5, 0, 10, 20, 3, 0.0, 0, 2, 0, 0, 4294967295,", "('verified', 0, 10, 50.0, 0.0, 7.5, 0, 10, 2000, 3, 0.0, 0, 2, 0, 0, 4294967295,"),
    ("-60, -20, 0, 128.0, 0, 901, 0, 0, 4294967295,", "-60, -20, 0, 64.0, 0, 901, 0, 0, 1,"),
  ]
  for old, new in edits:
    assert text.count(old) == 1, old
    text = text.replace(old, new)
  lines = text.split("\n")
  kept = [line for line in lines if "not exactly one to copy" not in line]
  assert len(kept) == len(lines) - 1
  return "\n".join(kept)


def testVerifyExportCatchesChangedZonePointsAndADroppedGuard(stageBlenderServer, exportedZone, tmp_path):
  copy = copiedExport(exportedZone, tmp_path)
  rowsPath = copy / "server" / "sql" / f"{short}.sql"
  rowsPath.write_text(changedRows(rowsPath.read_text(encoding="ascii")), encoding="ascii", newline="")
  rehashed(copy)
  pictures, result = verifyCopy(stageBlenderServer, exportedZone, copy)
  assert result["failedChecks"] == ["rows"]
  told = byCheck(result)["rows"]["details"]["failures"]
  assert [failure["message"] for failure in told if "column" not in failure] == [
    "zone_points 10 stands at [50.0, 0.0, 7.5], outside zone line ATP_1_north's box [104.0, -10.0, -5.0] to [116.0, 10.0, 20.0]",
    f"{short}.sql lacks its template guard on {{'shortName': '{short}', 'template': 'qeynos2'}}",
  ]
  # Every column a row writes is compared with what the scene's zone line makes: its place, its target and heading, and the rest.
  assert [(failure["file"], failure["column"], failure["found"], failure["expected"]) for failure in told if "column" in failure] == [
    (f"{short}.sql's zone_points 10", "target_x", 2000, 20), (f"{short}.sql's zone_points 10", "y", 50.0, 110.0),
    (f"{short}.sql's zone_points 20", "client_version_mask", 1, 4294967295), (f"{short}.sql's zone_points 20", "target_heading", 64.0, 128.0),
  ]
  assert len(pictures) == 2


def testVerifyExportLeavesTheWorkFileAsSaved(stageBlenderServer, exportedZone):
  def blendHash():
    return hashlib.sha256(exportedZone.blend.read_bytes()).hexdigest()

  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    before = await session.expectSuccess("getSceneSummary", {})
    savedBefore = blendHash()
    _, result = await verified(session, exportedZone.export / f"{short}.eqg")
    after = await session.expectSuccess("getSceneSummary", {})
    return before, savedBefore, result, after

  before, savedBefore, result, after = stageBlenderServer.session(steps)
  assert result["passed"] is True
  # The renders hid the kiln and the reference zone, and a new file held the archive; the work file, opened again, is as saved.
  assert after == before and after["status"]["filePath"] == str(exportedZone.blend) and after["status"]["unsavedChanges"] is False
  assert {entry["name"]: entry["hiddenInRender"] for entry in after["objects"] if entry["name"] in ("kiln", "reference")} == {"kiln": False, "reference": False}
  assert blendHash() == savedBefore


def blenderProcessID(server):
  """The process id of the server's Blender, known by its staged bridge script on its command line."""
  script = str(server.staged.repositoryPath / "server" / "bridgeMain.py")
  query = f"(Get-CimInstance Win32_Process -Filter \"Name = 'blender.exe'\" | Where-Object {{ $_.CommandLine.Contains('{script}') }}).ProcessId"
  found = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, encoding="utf-8", check=True).stdout.split()
  assert len(found) == 1, found
  return int(found[0])


def startsArchiveRenders(message):
  return message is not None and message.startswith("rendering archive view 1 of ")


def testVerifyExportListsArchiveRendersThatFailAndOpensTheWorkFileAgain(stageBlenderServer, exportedZone):
  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    blender = await anyio.to_thread.run_sync(blenderProcessID, stageBlenderServer)
    killed = []

    async def killBlender(done, total, message):
      if startsArchiveRenders(message) and not killed:
        os.kill(blender, signal.SIGTERM)
        killed.append(blender)

    result = await session.client.call_tool("verifyExport", {"path": str(exportedZone.export / f"{short}.eqg")}, progress_callback=killBlender)
    status = (await session.expectSuccess("getSceneSummary", {"objectLimit": 0}))["status"]
    return killed, result, status

  killed, result, status = stageBlenderServer.session(steps)
  # Blender dies while it draws the archive: the other checks stand, the looks name why no view was compared, and the work file is open
  # again in a new Blender, as saved.
  assert len(killed) == 1 and result.is_error is False
  verdict = wholeResult([content.text for content in result.content if content.type == "text"][-1])
  assert verdict["failedChecks"] == ["looks"] and verdict["comparison"] == {"drawn": False, "why": "no view was compared"}
  told = byCheck(verdict)["looks"]["details"]
  assert told["views"] == [] and [failure["message"].split(";")[0] for failure in told["failures"]] == [
    "The archive's views were not drawn, so none was compared: Blender exited with code 15 during renderView",
  ]
  assert [content.type for content in result.content] == ["image", "text"]
  assert status == {"filePath": str(exportedZone.blend), "unsavedChanges": False, "scene": "Scene"}


def testVerifyExportCancelledOpensTheWorkFileAgain(stageBlenderServer, exportedZone):
  async def steps(session):
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    rendering = anyio.Event()

    async def cancelOnArchiveRenders(done, total, message):
      if startsArchiveRenders(message):
        rendering.set()

    async with anyio.create_task_group() as group:
      group.start_soon(functools.partial(session.client.call_tool, "verifyExport", {"path": str(exportedZone.export / f"{short}.eqg")}, progress_callback=cancelOnArchiveRenders))
      await rendering.wait()
      group.cancel_scope.cancel()
    # The cancelled call opens the work file again after the render it was in; until then the archive's new file is open, never saved.
    for _ in range(reopenPolls):
      status = (await session.expectSuccess("getSceneSummary", {"objectLimit": 0}))["status"]
      if status["filePath"] is not None:
        return status
      await anyio.sleep(0.25)
    return status

  status = stageBlenderServer.session(steps)
  assert status == {"filePath": str(exportedZone.blend), "unsavedChanges": False, "scene": "Scene"}


def testVerifyExportRefusesWhatItCannotJudge(stageBlenderServer, exportedZone, tmp_path):
  archive = exportedZone.export / f"{short}.eqg"
  bare = tmp_path / "bare"
  bare.mkdir()
  shutil.copy(archive, bare / f"{short}.eqg")
  failing = copiedExport(exportedZone, tmp_path)
  manifestPath = failing / f"{short}_export.json"
  manifestPath.write_text(json.dumps(json.loads(manifestPath.read_text(encoding="ascii")) | {"failures": 3}), encoding="ascii")
  (tmp_path / "checked").mkdir()

  async def steps(session):
    refusals = {
      "relative": await session.expectError("verifyExport", {"path": f"{short}.eqg"}),
      "noManifest": await session.expectError("verifyExport", {"path": str(bare / f"{short}.eqg")}),
      "testExport": await session.expectError("verifyExport", {"path": str(exportedZone.reference)}),
    }
    await session.expectSuccess("openFile", {"path": str(exportedZone.folder / "reference.blend")})
    refusals["otherFile"] = await session.expectError("verifyExport", {"path": str(archive)})
    await session.expectSuccess("openFile", {"path": str(exportedZone.blend)})
    refusals["failures"] = await session.expectError("verifyExport", {"path": str(failing / f"{short}.eqg")})
    checked = await session.expectSuccess("checkExport", {"path": str(tmp_path / "checked" / f"{short}.eqg"), "purpose": "game"})
    refusals["check"] = await session.expectError("verifyExport", {"path": str(Path(checked["checkFolder"]) / f"{short}.eqg")})
    await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [0, 0, 1]})
    refusals["unsaved"] = await session.expectError("verifyExport", {"path": str(archive)})
    return refusals, checked

  refusals, checked = stageBlenderServer.session(steps)
  assert f"'{short}.eqg' is not an absolute path to an exported <short>.eqg" in refusals["relative"]
  assert f"No manifest {short}_export.json lies beside '{bare / f'{short}.eqg'}'" in refusals["noManifest"]
  assert "is a 'test' export's manifest; verifyExport reads back a game export" in refusals["testExport"]
  assert f"{manifestPath} records 3 failures; exportZone writes nothing while a check fails, so these are no export's files" in refusals["failures"]
  # A game check of the zone as exported passes and builds every file a game export writes; its folder is still no export.
  assert checked["failures"] == [] and checked["serverFiles"]["built"] is True
  checkManifest = Path(checked["checkFolder"]) / f"{short}_export.json"
  assert f"{checkManifest} is checkExport's manifest of a game check: a check run is not an export" in refusals["check"]
  assert f"{short}.eqg was exported from '{exportedZone.blend}', but the open file is '{exportedZone.folder / 'reference.blend'}'" in refusals["otherFile"]
  assert "The open file has unsaved changes; verifyExport judges an export against the scene as saved" in refusals["unsaved"]


def testVerifyExportNeedsTheRecastHelper(stageServer, tmp_path):
  server = stageServer({"blender": pinnedBlender, "extensions": {}})
  (tmp_path / f"{short}.eqg").write_bytes(b"an archive")
  manifest = {"purpose": "game", "writtenBy": "exportZone", "shortName": short, "blend": str(tmp_path / "zone.blend"), "files": {}, "failures": 0}
  (tmp_path / f"{short}_export.json").write_text(json.dumps(manifest), encoding="ascii")
  refusal = server.callToolExpectingError("verifyExport", {"path": str(tmp_path / f"{short}.eqg")})
  assert "The Recast helper is missing" in refusal and "run syncTooling to build it" in refusal, refusal
