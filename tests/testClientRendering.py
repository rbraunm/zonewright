import math
import sys
from pathlib import Path

from PIL import Image

from conftest import writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqCalibration

texture = (200, 150, 100, 255)
environment = {
  "ambientColor": [0.2, 0.25, 0.3], "specialAmbientColor": [0.05, 0, 0], "bounceColor": [0.1, 0.1, 0.2], "sunColor": [0.5, 0.4, 0.3],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 30, "fogColor": [0.4, 0.5, 0.6], "fogStart": 0, "fogEnd": 1000, "fogDensity": 0,
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
    await session.expectSuccess("setZoneProperties", {"fogStart": 0, "fogEnd": 40, "fogDensity": 0.33})
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


def testImportZoneBringsTheClientsZone(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return await session.expectSuccess("importZone", {"zone": "poknowledge"})

  imported = stageBlenderServer.session(steps)
  assert imported["source"] | {"particleCloudsNotDrawn": None} == {
    "archive": "poknowledge.s3d", "format": "wld", "regionMeshes": 1802, "placements": 1249, "placedObjects": 1249,
    "objectArchives": ["poknowledge_obj.s3d"], "missingModels": [], "missingTextures": [], "droppedTriangles": 0, "particleCloudsNotDrawn": None,
  }
  assert imported["dimensions"] == [1968.0, 1968.0, 1011.931]


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


def testCalibrationFitsTheFogOfAShotWithoutAZoneHeader(stageBlenderServer, tmp_path):
  shotName = "poknowledge,396.22,-192.08,-156.87,73.44,12.48"
  known = environment | {
    "ambientColor": [0.5, 0.5, 0.55], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.3, 0.3, 0.25], "sunAzimuthDegrees": 120,
    "sunElevationDegrees": 40, "fogColor": [0.6, 0.65, 0.75], "fogStart": 50, "fogEnd": 600, "fogDensity": 0.33,
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
  # baked light file does not fit its model.
  assert source["placedObjects"] == 5732
  assert source["litFilesNotMatchingModels"] == ["zoneout_obj_zone_out.lit"]
  assert source["missingModels"] == [] and source["missingTextures"] == []
