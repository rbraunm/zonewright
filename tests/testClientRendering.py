import io
import math
import sys
from pathlib import Path

import numpy
from PIL import Image
import pytest

from conftest import writeCubeDDS, writePNG

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqgWriter

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


async def centerPixel(session, outputPath):
  pixel = await session.expectSuccess("runPython", {"code": f"""
image = bpy.data.images.load(r'{outputPath}')
width, height = image.size
index = ((height // 2) * width + width // 2) * 4
result = list(image.pixels[index:index + 3])
bpy.data.images.remove(image)
"""})
  return pixel["result"]


async def renderedPixel(session, view):
  _, description = await session.expectImage("renderView", {"view": view})
  return await centerPixel(session, description["outputPath"])


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


def testACreateMaterialNormalMapIsNotDrawnUnlessAsked(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  # One tangent-space normal over the whole map, tilted half toward the texture's u axis, which runs along x on the floor: its facing
  # to a sun along y is the same whichever way the tangent runs.
  tilt = (192, 128, 192)
  normalPath = writePNG(tmp_path / "stone_n.png", 8, 8, (*tilt, 255))
  view = {"eye": [0, 0, 20], "target": [0, 0.001, 0]}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "floor", "size": [64, 64, 0], "location": [0, 0, 0]})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath), "normalTexture": str(normalPath)})
    await session.expectSuccess("assignMaterial", {"objectName": "floor", "materialName": "stone"})
    await session.expectSuccess("projectUVs", {"objectName": "floor", "method": "planar", "worldUnitsPerRepeat": 32, "direction": [0, 0, 1]})
    await session.expectSuccess("setZoneProperties", environment)
    plain = await renderedPixel(session, view)
    _, mapped = await session.expectImage("renderView", {"view": view, "normalMaps": True})
    mappedPixel = await centerPixel(session, mapped["outputPath"])
    again = await renderedPixel(session, view)
    refused = await session.expectError("renderView", {"view": view, "shading": "layout", "normalMaps": True})
    return plain, mapped["normalMaps"], mappedPixel, again, refused

  plain, lit, mappedPixel, again, refused = stageBlenderServer.session(steps)
  # The client lights an exported material by its vertex normal, so the floor is lit as if it had no normal map, before and after a
  # view asked for the maps.
  for measured, expected in zip(plain, clientPixel((0, 0, 1))):
    assert abs(measured - expected) <= 1.5 / 255
  assert again == plain
  assert lit == {"materials": ["stone"]}
  tilted = [2 * channel / 255 - 1 for channel in tilt]
  length = math.sqrt(sum(component * component for component in tilted))
  for measured, expected in zip(mappedPixel, clientPixel(tuple(component / length for component in tilted))):
    assert abs(measured - expected) <= 1.5 / 255
  assert "Normal maps draw only in client shading, not 'layout'" in refused



flipCode = """
import bmesh
for name in flipped:
  plane = bpy.data.objects[name]
  editor = bmesh.new()
  editor.from_mesh(plane.data)
  bmesh.ops.reverse_faces(editor, faces=list(editor.faces))
  editor.to_mesh(plane.data)
  editor.free()
  plane.data.update()
result = sorted({tuple(polygon.normal) for name in flipped for polygon in bpy.data.objects[name].data.polygons})
"""


def downwardSquare(x):
  """A 64-unit square at height 0 centered on [x, 0], wound to face down, as positions, normals, texture coordinates, and triangles."""
  positions = [[x - 32, -32, 0], [x + 32, -32, 0], [x + 32, 32, 0], [x - 32, 32, 0]]
  return positions, [[0, 0, -1]] * 4, [[position[0] / 64, position[1] / 64] for position in positions], [[0, 2, 1], [0, 3, 2]]


def testAFaceSeenFromBehindIsNotDrawnButATerrainLiquidsIs(stageBlenderServer, tmp_path):
  texturePath = writePNG(tmp_path / "stone.png", 8, 8, texture)
  leafPath = writePNG(tmp_path / "leaf.png", 8, 8, (60, 160, 40, 255))
  waterPaths = {
    "diffuseTexture": str(writePNG(tmp_path / "water_c.png", 4, 4, (40, 90, 110, 255))), "normalTexture": str(writePNG(tmp_path / "water_n.png", 4, 4, (128, 128, 255, 255))),
    "environmentTexture": str(writeCubeDDS(tmp_path / "water_e.dds", 4, [(200, 150, 100, 255)] * 6)),
  }
  planes = {"floor": ("stone", 0), "flipped": ("stone", 100), "leaf": ("leaf", 200), "water": ("water", 300), "falls": ("falls", 400)}
  # A zone file whose terrain and one placed model each hold a waterfall square facing down.
  falls = {"name": "falls", "diffuseTexture": "falls_c.dds", "normalTexture": None, "cutout": False, "liquid": {"liquid": "waterfall", "values": {"slides": [0, 0, 0, 0]}}}
  imported = {"terrainFalls": ("ter_sides.ter", 500), "objectFalls": ("obj_sides.mod", 600)}
  placements = [{"model": model, "name": model.split(".")[0].upper(), "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0), "scale": 1.0} for model, _ in imported.values()]
  entries = {model: eqgWriter.modelBytes(model[-3:], [falls], *downwardSquare(x), [0, 0], [0, 0]) for model, x in imported.values()}
  entries["sides.zon"] = eqgWriter.zoneBytes([model for model, _ in imported.values()], placements, [], [])
  entries["falls_c.dds"] = eqgWriter.ddsBytes(numpy.full((4, 4, 4), (40, 90, 110, 255), dtype=numpy.uint8))
  archivePath = tmp_path / "sides.eqg"
  archivePath.write_bytes(eqgWriter.archiveBytes(entries))

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("createMaterial", {"name": "leaf", "diffuseTexture": str(leafPath), "cutout": True, "alphaThreshold": 0.5})
    await session.expectSuccess("createLiquidMaterial", {"name": "water", "liquid": "water"} | waterPaths)
    await session.expectSuccess("createLiquidMaterial", {"name": "falls", "liquid": "waterfall", "diffuseTexture": waterPaths["diffuseTexture"]})
    for name, (material, x) in planes.items():
      await session.expectSuccess("createPrimitive", {"kind": "plane", "name": name, "size": [64, 64, 0], "location": [x, 0, 0]})
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": material})
    normals = (await session.expectSuccess("runPython", {"code": f"flipped = {sorted(set(planes) - {'floor'})!r}" + flipCode}))["result"]
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    places = {name: x for name, (_, x) in planes.items()} | {name: x for name, (_, x) in imported.items()}
    return normals, {name: await renderedPixel(session, {"eye": [x, 0, 20], "target": [x, 0.001, 0]}) for name, x in places.items()}

  normals, pixels = stageBlenderServer.session(steps)
  # Every square but the floor faces down and is seen from above, from behind. The client draws a face from its front only, so the
  # flipped stone and the leaf card are not drawn and the fog color shows through, nor is a liquid on a placed model (SModelWaterFall),
  # as the scene's own water and falls export; it draws a liquid on the zone's terrain (RegionWaterFall) from both sides. The floor,
  # seen from its front, is lit as the client lights it.
  assert normals == [[0.0, 0.0, -1.0]]
  for measured, expected in zip(pixels["floor"], clientPixel((0, 0, 1))):
    assert abs(measured - expected) <= 1.5 / 255
  for name in ("flipped", "leaf", "water", "falls", "objectFalls"):
    assert all(abs(measured - fog) <= 1.5 / 255 for measured, fog in zip(pixels[name], environment["fogColor"])), (name, pixels[name])
  assert max(abs(measured - fog) for measured, fog in zip(pixels["terrainFalls"], environment["fogColor"])) > 20 / 255, pixels["terrainFalls"]


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
    "objectArchives": ["poknowledge_obj.s3d"], "missingModels": [], "missingTextures": [], "environmentMapsNotCube": [], "droppedTriangles": 0,
    "particleCloudsNotDrawn": None,
    "placementColorsIgnoredBySkeletalActors": 0, "placementColorsShort": [], "placementsLitAtLoad": 883, "placementsParkedBelowTheWorld": {},
    "drawnOtherwise": {},
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


@pytest.mark.clientData("clientFiles")
def testObjectsAZoneParksBelowTheWorldStandThereWithoutStretchingLayoutBands(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZone", {"zone": "cauldron"})
    await session.expectSuccess("setZoneProperties", environment)
    _, layout = await session.expectImage("renderView", {"view": {"map": {"center": [0, 0], "width": 6000}}, "shading": "layout", "bandHeight": 50})
    return imported, layout

  imported, layout = stageBlenderServer.session(steps)
  # The Cauldron parks eight rocks at -32767.998, as the RoF2 client draws them: the zone object reaches down to them, while its
  # layout bands run over the zone itself, from its lowest point at -468.469.
  assert imported["source"]["placementsParkedBelowTheWorld"] == {"CAULROCK1_ACTORDEF": 3, "CAULROCK3_ACTORDEF": 5}
  assert imported["dimensions"][2] > 33000
  assert layout["heightRange"] == [-468.469, 727.938]


@pytest.mark.clientData("clientFiles")
def testAViewsCharacterSetsTheSpecialAmbientOfTheFloorItStandsOn(stageBlenderServer):
  view = {"eye": [-2061.1, -665.3, 95.5], "target": [-2000, -665.3, 92]}

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZone", {"zone": "cauldron"})
    await session.expectSuccess("setZoneProperties", environment)
    bare, _ = await session.expectImage("renderView", {"view": view})
    standing, described = await session.expectImage("renderView", {"view": view, "carriedLight": {"lightType": 0, "at": [-2061.1, -665.3, 90.0], "headingDegrees": 0}})
    return bare, standing, described

  bare, standing, described = stageBlenderServer.session(steps)
  # The Cauldron's floor there carries alpha about 11 at its corners, below the least share of scene light, 0.1: a character standing on
  # it takes the special ambient 0.08 times 0.9, 18/255 in each channel, in place of the zone's own 0.05, 0, 0, and carries no light.
  assert described["viewer"] == {"share": 0.1, "specialAmbientColor": [18 / 255] * 3, "character": "HUM of height 6"} and "carriedLight" not in described
  bareMean, standingMean = (numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=float).mean(axis=(0, 1)) for image in (bare, standing))
  assert standingMean[1] > bareMean[1] + 1 and standingMean[2] > bareMean[2] + 1


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
  # baked light file does not fit its model. No quad is a hole and every map and baked light file the zone names is in its archive;
  # one MPL coverage map its objects name, coverage_base2.dds, is in no linked archive, so those faces read an empty sampler.
  assert source["placedObjects"] == 5732
  assert source["litFilesNotMatchingModels"] == ["zoneout_obj_zone_out.lit"]
  assert source["missingModels"] == [] and source["missingTextures"] == ["coverage_base2.dds"]
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
