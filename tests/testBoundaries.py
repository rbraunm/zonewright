import io
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
from conftest import writePNG
from testModelsAndDressing import freshScene
from testWater import basin, liquidMaterials

environment = {
  "ambientColor": [0.3, 0.3, 0.35], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.15], "sunColor": [0.6, 0.5, 0.4],
  "sunAzimuthDegrees": 40, "sunElevationDegrees": 35, "fogColor": [0.5, 0.55, 0.6], "fogStart": 0, "fogEnd": 2000, "fogDensity": 0, "fogOn": False, "maxClip": 4000,
  "newEngineZone": False,
}
faceNormals = """
result = {name: [[round(value, 3) for value in polygon.normal] for polygon in bpy.data.objects[name].data.polygons][:3] for name in ('eastWall', 'lid', 'floor')}
"""
target = {"zone": "highpasshold", "x": 100, "y": "keep", "z": 5.5, "headingDegrees": 90}


def pixel(image, x, y):
  return numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)[y, x]


async def groundedBasin(session, folder):
  """The water tests' basin (flat at 0, a cone down to -20 inside 100 of the origin) in grass, flooded to -5."""
  await freshScene(session)
  await basin(session)
  await liquidMaterials(session, folder)
  await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(writePNG(folder / "grass.png", 4, 4, (90, 120, 60, 255)))})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
  await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})


def testBoundariesBlockPlayersFollowTheGroundAndDrawOnlyAsGuides(stageBlenderServer, tmp_path):
  async def steps(session):
    await groundedBasin(session, tmp_path)
    wall = await session.expectSuccess("placeBoundaryWall", {"name": "eastWall", "path": [[150, -100], [150, 100]], "height": 40})
    lid = await session.expectSuccess("placeBoundaryPlane", {"name": "lid", "kind": "lid", "outline": [[-180, -180], [-120, -180], [-120, -120], [-180, -120]], "height": 4})
    floor = await session.expectSuccess("placeBoundaryPlane", {"name": "floor", "kind": "floor", "outline": [[120, 120], [180, 120], [180, 180], [120, 180]], "height": -50})
    normals = (await session.expectSuccess("runPython", {"code": faceNormals}))["result"]
    await session.expectSuccess("setZoneProperties", environment)
    blocked = await session.expectSuccess("walkRoute", {"path": [[100, 0, 0], [190, 0, 0]]})
    around = await session.expectSuccess("walkRoute", {"path": [[100, 110, 0], [190, 110, 0]]})
    under = await session.expectSuccess("walkRoute", {"path": [[-190, -150, 0], [-110, -150, 0]]})
    wading = await session.expectSuccess("walkRoute", {"path": [[-160, 0, 0], [0, 0, 0]]})
    view = {"eye": [100, 0, 10], "target": [150, 0, 10]}
    guided, _ = await session.expectImage("renderView", {"view": view})
    clean, _ = await session.expectImage("renderView", {"view": view, "guides": False})
    mapped, _ = await session.expectImage("renderView", {"view": {"map": {"center": [0, 0], "width": 400}}})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [150, 0, 0], "radius": 30, "strength": 12})
    redrawn = await session.expectSuccess("placeBoundaryWall", {"name": "eastWall", "path": [[150, -100], [150, 100]], "height": 40})
    listed = await session.expectSuccess("getBoundaries", {})
    taken = await session.expectError("placeBoundaryWall", {"name": "pool", "path": [[0, 0], [10, 0]], "height": 10})
    offGround = await session.expectError("placeBoundaryWall", {"name": "far", "path": [[150, 150], [300, 150]], "height": 10})
    return wall, lid, floor, normals, blocked, around, under, wading, guided, clean, mapped, redrawn, listed, taken, offGround

  wall, lid, floor, normals, blocked, around, under, wading, guided, clean, mapped, redrawn, listed, taken, offGround = stageBlenderServer.session(steps)
  # The wall runs 200 along flat ground, sampled every 4: 51 columns from 5 under the ground to 40 over it.
  assert wall["samples"] == 51 and wall["triangles"] == 100 and wall["ground"] == [0.0, 0.0] and wall["length"] == 200.0
  assert wall["minimum"] == [150.0, -100.0, -5.0] and wall["maximum"] == [150.0, 100.0, 40.0] and wall["replaced"] is False
  # It faces left of its path (-x, walking north); a lid faces down and a floor up.
  assert normals["eastWall"] == [[-1.0, 0.0, 0.0]] * 3 and normals["lid"] == [[0.0, 0.0, -1.0]] * 2 and normals["floor"] == [[0.0, 0.0, 1.0]] * 2
  assert lid["area"] == 3600.0 and floor["minimum"][2] == -50.0
  # Players are stopped by the wall, walk round its end, are too tall to pass under the lid, and wade into the pool to its bed.
  assert not blocked["walkable"] and [(problem["kind"], problem["boundary"]) for problem in blocked["problems"]] == [("blocked", [150.0, 0.0, 3.0])]
  assert 149 <= blocked["problems"][0]["at"][0] < 150 and 150 < blocked["problems"][0]["resumesAt"][0] <= 151
  assert around["walkable"] and around["length"] == 90.0
  assert [(problem["kind"], problem["lowest"]) for problem in under["problems"]] == [("headroom", 4.0)]
  assert wading["walkable"] and wading["deepestWater"]["depth"] >= 14
  # With guides the wall shows as a red see-through slab over the fog; without them nothing is drawn there, as in the client.
  red, green, _ = pixel(guided, 480, 270)
  assert red - green > 40, (red, green)
  assert numpy.abs(pixel(clean, 480, 270) - [128, 140, 153]).max() <= 1
  # From above the wall is a red line at x 150 (pixel 840) on the grass, which is green just beside it.
  wallRed, wallGreen, _ = pixel(mapped, 840, 270)
  besideRed, besideGreen, _ = pixel(mapped, 820, 270)
  assert wallRed - wallGreen > 40 and besideGreen > besideRed, (pixel(mapped, 840, 270), pixel(mapped, 820, 270))
  # Placed again after the ground rose under it, the wall follows the ground as it now is.
  assert redrawn["replaced"] is True and redrawn["ground"][0] == 0.0 and 5 < redrawn["ground"][1] <= 12 and abs(redrawn["maximum"][2] - redrawn["ground"][1] - 40) <= 0.011
  assert [(entry["name"], entry["kind"], entry["triangles"]) for entry in listed["boundaries"]] == [("eastWall", "wall", 100), ("floor", "floor", 2), ("lid", "lid", 2)]
  assert listed["errors"] == [] and listed["passable"] == []
  assert "already exists and is not a boundary" in taken and "No ground players stand on under [201.3, 150.0]" in offGround


async def boundedPlot(session, folder):
  """The basin with a pool, a waterfall, a cutout card, a crate, a mossy crate marked passable, walls along the east edge with a gap,
  a zone line in the gap, a lid over the west, and the pool's swim volumes."""
  await groundedBasin(session, folder)
  await session.expectSuccess("pourWaterfall", {"name": "fall", "lip": [[-10, 60, 20], [10, 60, 20]], "bottom": -5, "throw": 0, "material": "falls"})
  await session.expectSuccess("createMaterial", {"name": "leaves", "diffuseTexture": str(writePNG(folder / "leaves.png", 4, 4, (40, 110, 40, 200))), "cutout": True})
  await session.expectSuccess("createMaterial", {"name": "wood", "diffuseTexture": str(writePNG(folder / "wood.png", 4, 4, (140, 100, 60, 255)))})
  await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "card", "size": [8, 8, 0], "location": [-60, 120, 4], "rotationDegrees": [90, 0, 0]})
  await session.expectSuccess("assignMaterial", {"objectName": "card", "materialName": "leaves"})
  await session.expectSuccess("projectUVs", {"objectName": "card", "method": "planar", "worldUnitsPerRepeat": 8, "direction": [0, 1, 0]})
  for name, location in (("crate", [120, 60, 0]), ("moss", [120, -60, 0])):
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [8, 8, 8], "location": location})
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "wood"})
    await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("markPassable", {"objects": ["moss"]})
  await session.expectSuccess("placeBoundaryWall", {"name": "eastSouth", "path": [[150, -150], [150, -20]], "height": 60})
  await session.expectSuccess("placeBoundaryWall", {"name": "eastNorth", "path": [[150, 20], [150, 150]], "height": 60})
  await session.expectSuccess("placeBoundaryPlane", {"name": "westLid", "kind": "lid", "outline": [[-190, -60], [-150, -60], [-150, 60], [-190, 60]], "height": 70})
  await session.expectSuccess("placeZoneLine", {"number": 1, "label": "east", "minimum": [148, -20, -5], "maximum": [160, 20, 60], "target": target})
  await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
  await session.expectSuccess("setZoneProperties", environment)


def testExportWritesWallsFlagsAndZoneLinesAndTheArchiveBringsThemBack(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "boundplot.eqg"

  async def steps(session):
    await boundedPlot(session, tmp_path)
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "boundplot.blend")})
    checked = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "test"})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    builtBoundaries = await session.expectSuccess("getBoundaries", {})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    lines = await session.expectSuccess("getZoneLines", {})
    blocked = await session.expectSuccess("walkRoute", {"path": [[100, 80, 0], [190, 80, 0]]})
    through = await session.expectSuccess("walkRoute", {"path": [[100, 0, 0], [190, 0, 0]]})
    wading = await session.expectSuccess("walkRoute", {"path": [[-160, 0, 0], [0, 0, 0]]})
    reference = await session.expectSuccess("checkExport", {"path": str(tmp_path / "again.eqg"), "purpose": "test"})
    return checked, exported, builtBoundaries, imported, lines, blocked, through, wading, reference

  checked, exported, builtBoundaries, imported, lines, blocked, through, wading, reference = stageBlenderServer.session(steps)
  assert checked["failures"] == [] and checked["boundaries"] == ["eastNorth", "eastSouth", "westLid"] and checked["zoneLines"] == ["ATP_1_east"]
  # A test export needs no zone row values, and lists what a game export would still refuse.
  assert [(finding["finding"], finding["missing"]) for finding in checked["findings"] if "missing" in finding] == [("view values missing", ["minClip", "sky"]), ("safe point or underworld missing", ["safePoint", "underworld"])]
  wallTriangles = sum(entry["triangles"] for entry in builtBoundaries["boundaries"])
  assert wallTriangles == 2 * 33 + 2 * 33 + 2 and exported["boundaryTriangles"] == wallTriangles
  archive = eqArchive.EQArchive(archivePath)
  terrain = eqgFiles.parseModel(archive.read("ter_boundplot.ter"), "ter_boundplot.ter")
  walls = terrain["triangleMaterials"] == eqgFiles.noMaterial
  # The walls are the terrain's triangles without a material, which block (flag 0) like the rest of the ground.
  assert int(walls.sum()) == wallTriangles and terrain["triangleFlags"].tolist() == [0] * len(walls)
  flags = {name: eqgFiles.parseModel(archive.read(name), name)["triangleFlags"] for name in ("obj_pool.mod", "obj_fall.mod", "obj_card.mod", "obj_crate.mod", "obj_moss_passable.mod")}
  # Liquid surfaces, the cutout card, and the crate marked passable let players through; the plain crate does not.
  assert {name: sorted(set(values.tolist())) for name, values in flags.items()} == {"obj_pool.mod": [1], "obj_fall.mod": [1], "obj_card.mod": [1], "obj_crate.mod": [0], "obj_moss_passable.mod": [1]}
  assert exported["passableTriangles"] == {name: len(values) for name, values in flags.items() if name != "obj_crate.mod"}
  zone = eqgFiles.parseZone(archive.read("boundplot.zon"), "boundplot.zon")
  line = next(region for region in zone["regions"] if region["name"] == "ATP_1_east")
  assert line == {"name": "ATP_1_east", "center": (154.0, 0.0, 27.5), "rotation": (0.0, 0.0, 0.0), "halfExtents": (6.0, 20.0, 32.5)}
  # The archive brings the walls back as one boundary and the region as a zone line, both reference, the target not in the file.
  assert imported["boundary"]["triangles"] == wallTriangles and imported["boundary"]["kind"] == "imported" and imported["boundary"]["clientContent"] == "zoneFile"
  assert imported["boundary"]["minimum"][0] == -190.0 and imported["boundary"]["maximum"] == [150.0, 150.0, 70.0]
  assert lines["zoneLines"] == [{"name": "ATP_1_east", "number": 1, "label": "east", "minimum": [148.0, -20.0, -5.0], "maximum": [160.0, 20.0, 60.0], "target": None, "clientContent": "zoneFile"}]
  assert lines["errors"] == [] and lines["gaps"] == [] and imported["zoneLinesTurned"] == [] and imported["passableTriangles"] == exported["passableTriangles"]
  # Walked over the archive as read back: the wall stops players, the zone line's gap lets them through, and the pool's surface,
  # flagged passable, lets them wade in to its bed.
  assert blocked["problems"][0]["kind"] == "blocked" and blocked["problems"][0]["boundary"][:2] == [150.0, 80.0]
  assert through["walkable"] and wading["walkable"] and wading["deepestWater"] is None and min(row["at"][2] for row in wading["profile"]) <= -15
  reasons = {group["reason"]: group["objects"] for group in reference["excluded"]}
  assert reasons["part of an imported zone archive: reference"][:3] == ["ATP_1_east", "boundplot", "boundplot boundaries"]


def testGameExportNeedsTheZoneRowAndZoneLineTargets(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "gameplot.eqg"
  check = {"path": str(archivePath), "purpose": "game"}

  async def steps(session):
    await groundedBasin(session, tmp_path)
    await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    await session.expectSuccess("setZoneProperties", environment)
    refusals = [
      await session.expectError("setZoneProperties", {"safePoint": [0, 150, 2, 0], "underworld": 10}),
      await session.expectError("setZoneProperties", {"safePoint": [0, 150, 2, 360]}),
      await session.expectError("placeZoneLine", {"number": 0, "label": "east", "minimum": [0, 0, 0], "maximum": [1, 1, 1], "target": target}),
      await session.expectError("placeZoneLine", {"number": 2, "label": "east gate", "minimum": [0, 0, 0], "maximum": [1, 1, 1], "target": target}),
      await session.expectError("placeZoneLine", {"number": 2, "label": "east", "minimum": [0, 0, 0], "maximum": [1, 0, 1], "target": target}),
      await session.expectError("placeZoneLine", {"number": 2, "label": "east", "minimum": [0, 0, 0], "maximum": [1, 1, 1], "target": target | {"zone": "High Pass"}}),
      await session.expectError("placeZoneLine", {"number": 2, "label": "east", "minimum": [0, 0, 0], "maximum": [1, 1, 1], "target": {"zone": "highpasshold", "x": 1}}),
    ]
    first = await session.expectSuccess("placeZoneLine", {"number": 2, "label": "gate", "minimum": [180, -20, -5], "maximum": [200, 20, 40], "target": target})
    replacing = await session.expectSuccess("placeZoneLine", {"number": 2, "label": "eastGate", "minimum": [180, -30, -5], "maximum": [200, 30, 40], "target": target | {"x": "keep"}})
    await session.expectSuccess("placeZoneLine", {"number": 3, "label": "north", "minimum": [-20, 180, -5], "maximum": [20, 200, 40], "target": target})
    await session.expectSuccess("organize", {"renames": {"ATP_3_north": "ATP_2_north"}})
    await session.expectSuccess("setZoneProperties", {"safePoint": [500, 500, 10, 90], "underworld": -100})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "gameplot.blend")})
    gaps = await session.expectSuccess("checkExport", check)
    tested = await session.expectSuccess("checkExport", check | {"purpose": "test"})
    await session.expectSuccess("duplicateObjects", {"names": ["ATP_2_eastGate"], "offset": [0, 0, 0]})
    await session.expectSuccess("saveFile", {})
    copied = await session.expectSuccess("checkExport", check | {"purpose": "test"})
    await session.expectSuccess("deleteObjects", {"names": ["ATP_2_eastGate.001"]})
    await session.expectSuccess("organize", {"renames": {"ATP_2_north": "ATP_3_north"}})
    await session.expectSuccess("setZoneProperties", {"minClip": 100, "sky": {"type": "gameplot", "hour": 12, "minute": 0}, "safePoint": [0, 150, 2, 90]})
    await session.expectSuccess("saveFile", {})
    ready = await session.expectSuccess("checkExport", check)
    return refusals, first, replacing, gaps, tested, copied, ready

  refusals, first, replacing, gaps, tested, copied, ready = stageBlenderServer.session(steps)
  assert "underworld 10.0 must lie below the safe point's height 2.0" in refusals[0] and "headingDegrees runs from 0 up to 360" in refusals[1]
  assert "whole number from 1" in refusals[2] and "letters, digits, and underscores" in refusals[3] and "maximum must lie above" in refusals[4]
  assert "zone short name" in refusals[5] and "target is {zone, x, y, z, headingDegrees}" in refusals[6]
  assert first["name"] == "ATP_2_gate" and first["target"] == target and first["minimum"] == [180.0, -20.0, -5.0]
  assert replacing["replaced"] == ["ATP_2_gate"] and replacing["name"] == "ATP_2_eastGate" and replacing["target"]["x"] == "keep"
  # A game export refuses the view values and a safe point over no ground, and a number two lines share; a test export lists them.
  assert [failure["failure"] for failure in gaps["failures"]] == ["view values missing", "safe point over no ground", "zone line number used twice", "containment not checked"]
  assert gaps["failures"][1]["at"] == [500, 500, 10] and gaps["failures"][2] == {"failure": "zone line number used twice", "number": 2, "zoneLines": ["ATP_2_eastGate", "ATP_2_north"]}
  assert tested["failures"] == [] and [finding["finding"] for finding in tested["findings"] if "object" not in finding] == ["view values missing", "safe point over no ground", "zone line number used twice"]
  # A copy keeps the name with a suffix the client cannot read; no zone file can hold it.
  assert [failure["message"] for failure in copied["failures"]] == ["'ATP_2_eastGate.001' is not named ATP_<number>_<label> with a number from 1; placeZoneLine names it"]
  assert [failure["failure"] for failure in ready["failures"]] == ["containment not checked"] and ready["zoneLines"] == ["ATP_2_eastGate", "ATP_3_north"]
