import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles


async def surfaceHeights(session, points):
  measured = await session.expectSuccess("measure", {"points": [[x, y, 100] for x, y in points], "snapToSurface": True})
  return [round(point[2], 3) for point in measured["points"]]


def testPassesAreAdjustedRemovedAndCollapsedWithoutRedoingOthers(stageBlenderServer):
  hilltop, dipCenter = (0, 0), (16, 16)

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 8, "location": [0, 0, 0]})
    added = await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 20, "strength": 10, "direction": [0, 0, 1]})
    full = await surfaceHeights(session, [hilltop])
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "hill", "strength": 0.5})
    half = await surfaceHeights(session, [hilltop])
    # Shaping at half strength still moves the ground as asked: the pass takes the move divided by its strength.
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 5], "radius": 20, "strength": 2, "direction": [0, 0, 1]})
    raisedAtHalf = await surfaceHeights(session, [hilltop])
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "hill", "muted": True})
    muted = await surfaceHeights(session, [hilltop])
    mutedWrite = await session.expectError("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 20, "strength": 1})
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "hill", "muted": False, "strength": 1})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "dip"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "lower", "center": [16, 16, 0], "radius": 10, "strength": 4, "direction": [0, 0, 1]})
    both = await surfaceHeights(session, [hilltop, dipCenter])
    faceChange = await session.expectError("subdivide", {"objectName": "ground", "selector": {"all": True}, "cuts": 1})
    removed = await session.expectSuccess("removeShapingPass", {"objectName": "ground", "name": "hill"})
    withoutHill = await surfaceHeights(session, [hilltop, dipCenter])
    collapsed = await session.expectSuccess("collapseShapingPasses", {"objectName": "ground"})
    afterCollapse = await surfaceHeights(session, [hilltop, dipCenter])
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    subdivided = await session.expectSuccess("subdivide", {"objectName": "ground", "selector": {"all": True}, "cuts": 1})
    return added, full, half, raisedAtHalf, muted, mutedWrite, both, faceChange, removed, withoutHill, collapsed, afterCollapse, detail, subdivided

  (added, full, half, raisedAtHalf, muted, mutedWrite, both, faceChange, removed, withoutHill, collapsed, afterCollapse, detail, subdivided) = stageBlenderServer.session(steps)
  assert added["passes"] == [{"name": "hill", "strength": 1.0, "muted": False, "active": True}]
  assert full == [10.0] and half == [5.0]
  # The hill now holds 10 + 2 / 0.5 = 14 units: 7 at half strength, as seen after the raise.
  assert raisedAtHalf == [7.0]
  assert muted == [0.0]
  assert "muted" in mutedWrite
  # The dip lies outside the hill's reach and the hill outside the dip's.
  assert both == [14.0, -4.0]
  assert "collapse them (collapseShapingPasses)" in faceChange
  assert removed["passes"] == [{"name": "dip", "strength": 1.0, "muted": False, "active": True}]
  assert withoutHill == [0.0, -4.0]
  assert collapsed["collapsed"] == ["dip"] and afterCollapse == [0.0, -4.0]
  assert detail["shapingPasses"] == []
  assert subdivided["faces"] > 0


def testPassesRefuseWhatTheyCannotHold(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [32, 32], "spacing": 8, "location": [0, 0, 0]})
    reserved = await session.expectError("addShapingPass", {"objectName": "ground", "name": "base"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "forms"})
    duplicate = await session.expectError("addShapingPass", {"objectName": "ground", "name": "forms"})
    tooStrong = await session.expectError("setShapingPass", {"objectName": "ground", "name": "forms", "strength": 3})
    unknown = await session.expectError("removeShapingPass", {"objectName": "ground", "name": "erosion"})
    return reserved, duplicate, tooStrong, unknown

  reserved, duplicate, tooStrong, unknown = stageBlenderServer.session(steps)
  assert "'base' names the shape passes build on" in reserved
  assert "already has a shaping pass 'forms'" in duplicate
  assert "strength must be within [-1.0, 2.0]" in tooStrong
  assert "has no shaping pass 'erosion'; its passes: ['forms']" in unknown


def testExportWritesTheTerrainWithItsPassesCombined(stageBlenderServer, tmp_path):
  texturePath = tmp_path / "ground.png"
  Image.new("RGBA", (8, 8), (90, 120, 60, 255)).save(texturePath)
  archivePath = tmp_path / "passplot.eqg"

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 32, "direction": [0, 0, 1]})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 20, "strength": 10, "direction": [0, 0, 1]})
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "hill", "strength": 0.3})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "passplot.blend")})
    return await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})

  stageBlenderServer.session(steps)
  terrain = eqgFiles.parseModel(eqArchive.EQArchive(archivePath).read("ter_passplot.ter"), "ter_passplot.ter")
  assert round(float(terrain["vertices"][:, 2].max()), 4) == 3.0


def testViewsSeeAPassShapedJustBefore(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 8, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", {
      "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.5, 0.5, 0.5],
      "sunAzimuthDegrees": 135, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 30, "fogEnd": 200, "fogDensity": 0.33, "fogOn": True, "maxClip": 400,
      "newEngineZone": False,
    })
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "pit"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "lower", "center": [0, 0, 0], "radius": 20, "strength": 10, "direction": [0, 0, 1]})
    _, description = await session.expectImage("renderView", {"view": {"map": {"center": [0, 0], "width": 64}}, "shading": "layout"})
    return description

  assert stageBlenderServer.session(steps)["heightRange"] == [-10.0, 0.0]
