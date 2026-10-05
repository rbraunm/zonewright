import io
import math
import sys
from pathlib import Path

import numpy
from PIL import Image
import pytest

from conftest import writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqCalibration

texture = (200, 150, 100, 255)
environment = {
  "ambientColor": [0.2, 0.25, 0.3], "specialAmbientColor": [0.05, 0, 0], "bounceColor": [0.1, 0.1, 0.2], "sunColor": [0.5, 0.4, 0.3],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 30, "fogColor": [0.4, 0.5, 0.6], "fogStart": 0, "fogEnd": 1000, "fogDensity": 0, "fogOn": False, "maxClip": 2000,
  "newEngineZone": False,
}


def clientPixel(normal, fogVisibility=1.0):
  """The client's lit, fogged color of the test texture on a surface facing normal under the test environment (docs/clientRendering.md)."""
  sun = environment["sunElevationDegrees"]
  towardSun = (0.0, math.cos(math.radians(sun)), math.sin(math.radians(sun)))
  facing = sum(n * s for n, s in zip(normal, towardSun))
  light = [
    min(1.0, ambient + special + sunColor * max(facing, 0) + bounce * max(-facing, 0))
    for ambient, special, sunColor, bounce in zip(environment["ambientColor"], environment["specialAmbientColor"], environment["sunColor"], environment["bounceColor"])
  ]
  lit = [channel / 255 * value for channel, value in zip(texture, light)]
  return [fog + fogVisibility * (value - fog) for value, fog in zip(lit, environment["fogColor"])]


async def renderedPixel(session, view):
  _, description = await session.expectImage("renderView", {"view": view})
  pixel = await session.expectSuccess("runPython", {"code": f"""
image = bpy.data.images.load(r'{description['outputPath']}')
width, height = image.size
index = ((height // 2) * width + width // 2) * 4
result = list(image.pixels[index:index + 3])
bpy.data.images.remove(image)
"""})
  return pixel["result"]


def testPreviewLightsAndFogsAsTheClientDoes(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "floor", "size": [64, 64, 0], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [10, 10, 10], "location": [0, 40, 0]})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    for name in ("floor", "wall"):
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "stone"})
    await session.expectSuccess("setZoneProperties", environment)
    floor = await renderedPixel(session, {"eye": [0, 0, 20], "target": [0, 0.001, 0]})
    wall = await renderedPixel(session, {"eye": [0, 20, 5], "target": [0, 40, 5]})
    await session.expectSuccess("setZoneProperties", {"fogStart": 0, "fogEnd": 40, "fogDensity": 0.33, "fogOn": True})
    fogged = await renderedPixel(session, {"eye": [0, 0, 20], "target": [0, 0.001, 0]})
    return floor, wall, fogged

  floor, wall, fogged = stageBlenderServer.session(steps)
  # The floor faces up, half toward the sun 30 degrees above +Y; the wall's face toward -Y turns from the sun, so only bounce reaches it.
  for measured, expected in zip(floor, clientPixel((0, 0, 1))):
    assert abs(measured - expected) <= 1.5 / 255
  for measured, expected in zip(wall, clientPixel((0, -1, 0))):
    assert abs(measured - expected) <= 1.5 / 255
  # Twenty units into fog that ends at 40 is half the ramp: density 0.33 times 5, squared, leaves exp(-2.7225) of the surface.
  for measured, expected in zip(fogged, clientPixel((0, 0, 1), math.exp(-(0.33 * 5) ** 2))):
    assert abs(measured - expected) <= 1.5 / 255



flipCode = """
import bmesh
plane = bpy.data.objects['flipped']
editor = bmesh.new()
editor.from_mesh(plane.data)
bmesh.ops.reverse_faces(editor, faces=list(editor.faces))
editor.to_mesh(plane.data)
editor.free()
plane.data.update()
result = [list(polygon.normal) for polygon in plane.data.polygons]
"""


def testAFaceSeenFromBehindIsLitByItsOwnNormal(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    for name, x in (("floor", 0), ("flipped", 100)):
      await session.expectSuccess("createPrimitive", {"kind": "plane", "name": name, "size": [64, 64, 0], "location": [x, 0, 0]})
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "stone"})
    normals = (await session.expectSuccess("runPython", {"code": flipCode}))["result"]
    await session.expectSuccess("setZoneProperties", environment)
    floor = await renderedPixel(session, {"eye": [0, 0, 20], "target": [0, 0.001, 0]})
    flipped = await renderedPixel(session, {"eye": [100, 0, 20], "target": [100, 0.001, 0]})
    return normals, floor, flipped

  normals, floor, flipped = stageBlenderServer.session(steps)
  # The flipped plane faces down and is seen from above, from behind: the client's vertex shader lights it by its own normal, turned
  # from the sun, so only bounce light reaches it, where a front face there takes the sun.
  assert normals == [[0.0, 0.0, -1.0]]
  for measured, expected in zip(floor, clientPixel((0, 0, 1))):
    assert abs(measured - expected) <= 1.5 / 255
  for measured, expected in zip(flipped, clientPixel((0, 0, -1))):
    assert abs(measured - expected) <= 1.5 / 255


def testFogIsOffWithTheZonesFogSwitchAndPulledInShortOfTheFarClip(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  view = {"eye": [0, 0, 20], "target": [0, 0.001, 0]}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "floor", "size": [64, 64, 0], "location": [0, 0, 0]})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "floor", "materialName": "stone"})
    await session.expectSuccess("setZoneProperties", environment | {"fogStart": 0, "fogEnd": 40, "fogDensity": 0.33, "fogOn": False, "maxClip": 1000})
    off = await renderedPixel(session, view)
    await session.expectSuccess("setZoneProperties", {"fogOn": True, "fogEnd": 100, "maxClip": 60})
    pulledIn = await renderedPixel(session, view)
    refused = await session.expectError("setZoneProperties", {"minClip": 20})
    return off, pulledIn, refused

  off, pulledIn, refused = stageBlenderServer.session(steps)
  # With the fog off the floor draws as if there were none, whatever the density.
  for measured, expected in zip(off, clientPixel((0, 0, 1))):
    assert abs(measured - expected) <= 1.5 / 255
  # The fog end 100 reaches the far clip 60, so the client ends it at 60 - 0.15 * 60 = 51: the floor 20 away is 10 * 20 / 51 into the ramp.
  for measured, expected in zip(pulledIn, clientPixel((0, 0, 1), math.exp(-(0.33 * 10 * 20 / 51) ** 2))):
    assert abs(measured - expected) <= 1.5 / 255
  assert "minClip must be at least 50" in refused

@pytest.mark.clientData("clientFiles")
def testImportZoneBringsTheClientsZone(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "poknowledge"})

  imported = stageBlenderServer.session(steps)
  assert imported["source"] | {"particleCloudsNotDrawn": None} == {
    "archive": "poknowledge.s3d", "format": "wld", "regionMeshes": 1802, "placements": 1249, "placedObjects": 1249,
    "objectArchives": ["poknowledge_obj.s3d"], "missingModels": [], "missingTextures": [], "droppedTriangles": 0, "particleCloudsNotDrawn": None,
    "placementColorsIgnoredBySkeletalActors": 0, "placementColorsShort": [], "placementsLitAtLoadNotDrawn": 883,
  }
  assert imported["dimensions"] == [1968.0, 1968.0, 1011.931]
  # A classic zone's zone lines are BSP regions, whose places are not read.
  assert imported["zoneLines"] is None and imported["zoneLinesTilted"] is None
  assert imported["lights"] == 620 and imported["lightsOfRadiusZero"] == [] and imported["filesTheClientNeverOpens"] == []


@pytest.mark.clientData("clientFiles")
def testImportZoneLeavesOutALightOfRadiusZero(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "runnyeye"})

  imported = stageBlenderServer.session(steps)
  # Every Runnyeye placement carries its own vertex colors; one of its 383 lights has radius 0, which lights nothing in the client.
  assert imported["source"]["placements"] == imported["source"]["placedObjects"] == 648 and imported["source"]["placementColorsShort"] == []
  assert imported["lights"] == 382 and imported["lightsOfRadiusZero"] == ["L277_LDEF"]


def pixelAt(image, x, y):
  return Image.open(io.BytesIO(image)).convert("RGB").getpixel((x, y))


@pytest.mark.clientData("clientFiles")
def testImportZoneBringsAnEQGZonesZoneLinesAsGuides(stageBlenderServer):
  around = {"center": [2111, -729], "width": 900}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZone", {"zone": "draniksscar"})
    await session.expectSuccess("setZoneProperties", environment)
    lines = await session.expectSuccess("getZoneLines", {})
    plan, _ = await session.expectImage("renderSketch", around)
    bare, _ = await session.expectImage("renderSketch", around | {"layers": ["regions"]})
    return imported, lines, plan, bare

  imported, lines, plan, bare = stageBlenderServer.session(steps)
  assert [line["name"] for line in imported["zoneLines"]] == ["ATP_3_", "ATP_2_", "ATP_1_"] and imported["zoneLinesTilted"] == []
  # Each a 120 by 60 by 130 box at the .zon's center, turned by its first turn field read as 512ths of a turn (pi/2, pi, -pi/2 written
  # in radians: a degree or two), with no target: the zone's files never say where a zone line leads.
  assert lines["zoneLines"][1] == {
    "name": "ATP_2_", "number": 2, "label": "", "minimum": [2049.57, -761.67, -272.05], "maximum": [2171.79, -697.09, -142.05], "target": None,
    "headingDegrees": -2.21, "clientContent": "zone",
  }
  assert [(line["name"], line["number"], line["headingDegrees"], line["target"], line["clientContent"]) for line in lines["zoneLines"]] == [
    ("ATP_1_", 1, -1.1, None, "zone"), ("ATP_2_", 2, -2.21, None, "zone"), ("ATP_3_", 3, 1.1, None, "zone"),
  ]
  # Guides an import brings are reference: no export check or game export gap counts them.
  assert lines["errors"] == [] and lines["gaps"] == []
  # The plan at 1.6 pixels a unit, north (+X) up: inside ATP_2_ at (2160, -712), clear of its name, the guide tints the tube green.
  red, green, _ = (inGuide - inBare for inGuide, inBare in zip(pixelAt(plan, 692, 327), pixelAt(bare, 692, 327)))
  assert green - red > 30, (pixelAt(plan, 692, 327), pixelAt(bare, 692, 327))


@pytest.mark.clientData("calibration")
def testCalibrationRecoversTheLightOfAKnownShot(stageBlenderServer, tmp_path):
  shotName = "poknowledge,396.22,-192.08,-156.87,73.44,12.48"
  known = environment | {"ambientColor": [0.35, 0.4, 0.6], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.3, 0.25, 0.1], "sunAzimuthDegrees": 120, "sunElevationDegrees": 40}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZone", {"zone": "poknowledge"})
    await session.expectSuccess("setZoneProperties", known)
    image, _ = await session.expectImage("renderView", {"view": eqCalibration.shotView(eqCalibration.parseShotName(shotName + ".jpg"))})
    (tmp_path / "shot.png").write_bytes(image)
    Image.open(tmp_path / "shot.png").convert("RGB").save(tmp_path / f"{shotName}.jpg", quality=95)
    calibrated = await session.expectImage("calibrateShot", {
      "screenshotPath": str(tmp_path / f"{shotName}.jpg"), "zone": "poknowledge", "newEngineZone": False, "fogColor": known["fogColor"],
      "fogStart": known["fogStart"], "fogEnd": known["fogEnd"], "fogDensity": 0, "discardUnsavedChanges": True,
    })
    return calibrated, await session.expectSuccess("getToolingStatus")

  (_, result), status = stageBlenderServer.session(steps)
  fit = result["fit"]
  # A shot drawn by the renderer itself, through JPEG: the fit finds its ambient and sun and redraws it within a few levels.
  for key in ("ambientColor", "sunColor"):
    for measured, expected in zip(fit[key], known[key]):
      assert abs(measured - expected) <= 0.05, (key, fit[key], known[key])
  assert result["meanPixelDifference"] < 3
  assert Path(result["comparePath"]).is_file()
  # The run is tracked: tooling status lists the screenshot's latest calibration.
  assert [(entry["screenshot"], entry["runs"], entry["meanPixelDifference"]) for entry in status["calibration"]] == [(f"{shotName}.jpg", 1, result["meanPixelDifference"])]


@pytest.mark.clientData("calibration")
def testCalibrationFitsTheFogOfAShotWithoutAZoneHeader(stageBlenderServer, tmp_path):
  shotName = "poknowledge,396.22,-192.08,-156.87,73.44,12.48"
  known = environment | {
    "ambientColor": [0.5, 0.5, 0.55], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.3, 0.3, 0.25], "sunAzimuthDegrees": 120,
    "sunElevationDegrees": 40, "fogColor": [0.6, 0.65, 0.75], "fogStart": 50, "fogEnd": 600, "fogDensity": 0.33, "fogOn": True, "maxClip": 1200,
  }

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZone", {"zone": "poknowledge"})
    await session.expectSuccess("setZoneProperties", known)
    image, _ = await session.expectImage("renderView", {"view": eqCalibration.shotView(eqCalibration.parseShotName(shotName + ".jpg"))})
    (tmp_path / "shot.png").write_bytes(image)
    Image.open(tmp_path / "shot.png").convert("RGB").save(tmp_path / f"{shotName}.jpg", quality=95)
    return await session.expectImage("calibrateShot", {
      "screenshotPath": str(tmp_path / f"{shotName}.jpg"), "zone": "poknowledge", "newEngineZone": False, "discardUnsavedChanges": True,
    })

  _, result = stageBlenderServer.session(steps)
  fit = result["fit"]
  # With no zone header given, the fog is fitted at the client's density alongside the light, and the redraw matches the shot. Where
  # no surface in view is fully fogged, a slightly nearer fog end with a slightly darker fog color draws nearly the same image, so
  # the color is held to 0.08.
  for measured, expected in zip(fit["fogColor"], known["fogColor"]):
    assert abs(measured - expected) <= 0.08, (fit["fogColor"], known["fogColor"])
  assert abs(fit["fogStart"] - known["fogStart"]) <= 60
  assert abs(fit["fogEnd"] - known["fogEnd"]) <= 120
  assert result["meanPixelDifference"] < 4


@pytest.mark.clientData("clientFiles")
def testImportZoneBringsATerrainZone(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "neighborhood"})

  imported = stageBlenderServer.session(steps)
  source = imported["source"]
  assert {key: source[key] for key in ("archive", "format", "tiles", "ecosystems", "terrainCombos", "placements", "objectGroups", "missingObjectGroups")} == {
    "archive": "neighborhood.eqg", "format": "eqtzp", "tiles": 629, "ecosystems": ["dirtpath", "forest", "grassland", "lightdirt"],
    "terrainCombos": [["grassland"], ["grassland", "dirtpath"], ["grassland", "dirtpath", "forest"], ["grassland", "forest", "dirtpath"], ["grassland", "lightdirt"]],
    "placements": 5730, "objectGroups": 5, "missingObjectGroups": ["drgbrownie"],
  }
  # Every tile placement is drawn, plus the merchant tent and the zone-out wall of the two object groups the archive holds; the wall's
  # baked light file does not fit its model. No quad is a hole and every map and baked light file the zone names is in its archive.
  assert source["placedObjects"] == 5732
  assert source["litFilesNotMatchingModels"] == ["zoneout_obj_zone_out.lit"]
  assert source["missingModels"] == [] and source["missingTextures"] == []
  assert (source["holeQuads"], source["terrainMapsMissing"], source["litFilesMissing"], source["litFilesShorterThanTheirCount"]) == (0, {}, [], [])


@pytest.mark.clientData("clientFiles")
def testATerrainDetailMapTheArchiveLacksDrawsBlack(stageBlenderServer):
  # cryptofshade.eqg names detail map di_hill_grass_muddy.dds and normal map ground_grass_10n.dds for the only layer of 'grass', the
  # ecosystem on every tile, without holding either: the client's empty sampler reads black, so the ground draws black where no later
  # ecosystem covers it, while its placed rocks draw with their textures.
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZone", {"zone": "cryptofshade"})
    await session.expectSuccess("setZoneProperties", environment | {"fogOn": False})
    image, _ = await session.expectImage("renderView", {"view": {"map": {"center": [-300, 1300], "width": 2400}}, "guides": False})
    return imported, image

  imported, image = stageBlenderServer.session(steps)
  assert imported["source"]["terrainMapsMissing"] == {"detailMaps": ["di_hill_grass_muddy.dds"], "normalMaps": ["ground_grass_10n.dds"]}
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)
  assert (pixels.max(axis=2) <= 2).mean() > 0.9 and pixels.max() > 40
