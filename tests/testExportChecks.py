import io

import numpy
from PIL import Image

import structurePlots
from conftest import writePNG

environment = {
  "ambientColor": [0.3, 0.3, 0.35], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.15], "sunColor": [0.6, 0.5, 0.4],
  "sunAzimuthDegrees": 40, "sunElevationDegrees": 35, "fogColor": [0.5, 0.55, 0.6], "fogStart": 0, "fogEnd": 2000, "fogDensity": 0, "fogOn": False, "maxClip": 4000,
  "newEngineZone": False,
}
flipSlabTop = """
import bmesh
mesh = bpy.data.objects['slab'].data
editor = bmesh.new()
editor.from_mesh(mesh)
max(editor.faces, key=lambda face: face.calc_center_median().z).normal_flip()
editor.to_mesh(mesh)
editor.free()
mesh.update()
"""
coverageMap = {"view": {"map": {"center": [0, 0], "width": 120}}, "shading": "coverage"}
# A terrain's ground rising along x at `degrees` from 16 before its middle to 16 past it, flat beyond.
gradeAcross = """
import math
mesh = bpy.data.objects[objectName].data
for vertex in mesh.vertices:
  vertex.co.z = (min(max(vertex.co.x, -16.0), 16.0) + 16.0) * math.tan(math.radians(degrees))
mesh.update()
"""
# Flat ground facing up in the coverage light (from the northwest, 45 degrees up): half ambient plus half of the light.
upShade = 0.5 + 0.5 * 0.7071
colors = {"orange": (1.0, 0.5, 0.0), "brown": (0.45, 0.28, 0.12), "magenta": (0.95, 0.1, 0.85), "grey": (0.62, 0.62, 0.6)}
# Map pixels (8 to a unit, the origin at the middle, north (+X) up and east (-Y) right) of the base patch, the blockout patch, a sand
# face on the border, plain grass, and the tops of the slab and the post.
mapPoints = {"base": (-8, -8), "blockout": (-24, 24), "border": (20, 20), "grass": (-28, -4), "slabTop": (0, -24), "postTop": (24, -24)}
pixels = {name: (480 - 8 * y, 270 - 8 * x) for name, (x, y) in mapPoints.items()}


async def faultyPlot(session, folder):
  """A 64 x 64 terrain of 8-unit cells surfaced with grass over a dirt base: a 2 x 2 cell patch left unpainted at the middle's
  southeast (-x, -y), a sand band along the north (+x) edge meeting the grass with no transition, a blockout patch; a slab textured at four times
  grass's repeat with its top wound backwards, and a post projected from above so its sides take no texture; planned as one region
  players play in."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  for name, color, blockout in (("dirt", (120, 90, 60, 255), False), ("grass", (70, 120, 50, 255), False), ("sand", (200, 180, 120, 255), False), ("grey", (128, 128, 128, 255), True), ("edge", (150, 150, 90, 255), False)):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(folder / f"{name}.png", 4, 4, color)), "blockout": blockout})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "dirt"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 16, "direction": [0, 0, 1]})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "grass", "selector": {"all": True}})
  await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "ground", "selector": {"box": {"minimum": [-16, -16, -1], "maximum": [0, 0, 1]}}})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"box": {"minimum": [16, -32, -1], "maximum": [32, 32, 1]}}})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "grey", "selector": {"box": {"minimum": [-32, 16, -1], "maximum": [-16, 32, 1]}}})
  for name, location in (("slab", [0, -24, 0]), ("post", [24, -24, 0])):
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [8, 8, 8], "location": location})
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "grass"})
  await session.expectSuccess("projectUVs", {"objectName": "slab", "method": "box", "worldUnitsPerRepeat": 64})
  await session.expectSuccess("projectUVs", {"objectName": "post", "method": "planar", "worldUnitsPerRepeat": 16, "direction": [0, 0, 1]})
  await session.expectSuccess("runPython", {"code": flipSlabTop})
  await session.expectSuccess("createRegion", {"name": "plot", "outline": [[-32, -32], [32, -32], [32, 32], [-32, 32]], "bottom": -10, "top": 20, "intent": "the plot", "access": "play"})
  await session.expectSuccess("setZoneProperties", environment)
  await session.expectSuccess("saveFile", {"path": str(folder / "plot.blend")})


def colorAt(image, name):
  return numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)[pixels[name][1], pixels[name][0]]


def shaded(name):
  return numpy.round(numpy.array(colors[name]) * upShade * 255)


def testCoverageFindingsNameWhatThePicturesShowAndClearAsEachIsFixed(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "coverplot.eqg"
  target = {"path": str(archivePath)}

  async def steps(session):
    await faultyPlot(session, tmp_path)
    test = await session.expectSuccess("checkExport", target | {"purpose": "test"})
    game = await session.expectSuccess("checkExport", target | {"purpose": "game"})
    before, beforeView = await session.expectImage("renderView", coverageMap)
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "grass", "selector": {"box": {"minimum": [-16, -16, -1], "maximum": [0, 0, 1]}}})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "edges"})
    transition = await session.expectSuccess("paintTransition", {"objectName": "ground", "layer": "edges", "material": "edge", "selector": {"material": "grass"}, "toward": {"material": "sand"}, "width": 8, "worldUnitsPerRepeat": 16})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "grass", "selector": {"box": {"minimum": [-32, 16, -1], "maximum": [-16, 32, 1]}}})
    for name in ("slab", "post"):
      await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 16})
    await session.expectSuccess("cleanupMesh", {"objectName": "slab"})
    await session.expectSuccess("saveFile", {})
    fixed = await session.expectSuccess("checkExport", target | {"purpose": "test"})
    fixedGame = await session.expectSuccess("checkExport", target | {"purpose": "game"})
    after, _ = await session.expectImage("renderView", coverageMap)
    await session.expectSuccess("setZoneProperties", {"minClip": 100, "sky": {"type": "coverplot", "hour": 12, "minute": 0}, "safePoint": [8, 8, 2, 0], "underworld": -50})
    await session.expectSuccess("saveFile", {})
    viewed = await session.expectSuccess("checkExport", target | {"purpose": "game"})
    refused = await session.expectError("exportZone", target | {"purpose": "game"})
    written = await session.expectSuccess("exportZone", target | {"purpose": "test"})
    return test, game, before, beforeView, transition, fixed, fixedGame, after, viewed, refused, written

  test, game, before, beforeView, transition, fixed, fixedGame, after, viewed, refused, written = stageBlenderServer.session(steps)
  assert test["failures"] == []
  assert [(finding["finding"], finding.get("object")) for finding in test["findings"]] == [
    ("back faces", "slab"), ("base material showing", "ground"), ("border without a transition", "ground"),
    ("texture stretched or squeezed", "slab"), ("zero texture area", "post"), ("blockout", "ground"),
    ("view values missing", None), ("safe point or underworld missing", None),
  ]
  findings = {finding["finding"]: finding for finding in test["findings"]}
  assert findings["back faces"]["at"] == [{"center": [0.0, -24.0, 8.0], "faces": 1}]
  assert findings["base material showing"] == {"finding": "base material showing", "object": "ground", "material": "dirt", "faces": 4, "at": [{"center": [-8.0, -8.0, 0.0], "faces": 4}], "pieces": 1}
  border = findings["border without a transition"]
  assert border["materials"] == ["grass", "sand"] and border["length"] == 64.0 and border["stretches"] == [{"center": [16.0, 0.0, 0.0], "minimum": [16.0, -32.0, 0.0], "maximum": [16.0, 32.0, 0.0], "length": 64.0}]
  stretched = findings["texture stretched or squeezed"]
  assert stretched["material"] == "grass" and stretched["faces"] == 6 and stretched["worst"] == 4.0 and stretched["usualRepeat"] == 16.0
  assert findings["zero texture area"]["faces"] == 4 and findings["zero texture area"]["at"] == [{"center": [24.0, -24.0, 4.0], "faces": 4}]
  assert findings["blockout"]["material"] == "grey" and findings["blockout"]["faces"] == 4
  assert test["coverage"] == {"error": 0, "zeroTexture": 4, "blockout": 4, "stretch": 6, "base": 4, "border": 16, "ok": 42, "back": 1}
  # A game export refuses the blockout, the zone row's values it lacks, and, until reach mapping exists, any zone.
  assert [(failure["failure"], failure.get("missing")) for failure in game["failures"]] == [
    ("blockout", None), ("view values missing", ["minClip", "sky"]), ("safe point or underworld missing", ["safePoint", "underworld"]), ("containment not checked", None),
  ]
  assert "blockout" not in [finding["finding"] for finding in game["findings"]]
  # The coverage map shows each finding where the list puts it.
  assert beforeView["coverage"]["faces"] == test["coverage"]
  for name, color in (("base", "orange"), ("blockout", "brown"), ("border", "magenta"), ("grass", "grey"), ("postTop", "grey")):
    assert numpy.abs(colorAt(before, name) - shaded(color)).max() <= 2, (name, colorAt(before, name))
  red, green, blue = colorAt(before, "slabTop")
  assert blue > 2 * green and green > 2 * red, (red, green, blue)
  # Fixed one by one, every finding clears, and the map turns grey where each was.
  assert transition["painted"] == 8 and transition["straddlingFaces"] == 0
  assert fixed["failures"] == [] and [finding["finding"] for finding in fixed["findings"]] == ["view values missing", "safe point or underworld missing"]
  assert fixed["coverage"] == {"error": 0, "zeroTexture": 0, "blockout": 0, "stretch": 0, "base": 0, "border": 0, "ok": 76, "back": 0}
  assert [failure["failure"] for failure in fixedGame["failures"]] == ["view values missing", "safe point or underworld missing", "containment not checked"]
  for name in ("base", "blockout", "border", "slabTop"):
    assert numpy.abs(colorAt(after, name) - shaded("grey")).max() <= 2, (name, colorAt(after, name))
  assert [failure["failure"] for failure in viewed["failures"]] == ["containment not checked"]
  assert "exportZone (game) refused, nothing written: 1 failure(s)" in refused and "reach mapping" in refused
  assert written["failures"] == [] and written["findings"] == [] and archivePath.is_file()


def testABorderWantsATransitionOnlyWhereItsGroundReadsAsGroundNotCliff(stageBlenderServer, tmp_path):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    for name, color in (("dirt", (120, 90, 60, 255)), ("grass", (70, 120, 50, 255)), ("rock", (110, 110, 105, 255))):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
    # Grass meets rock across the middle of each slope, the faces on both sides of the border sloping alike.
    for objectName, degrees, y in (("cliff", 65, 0), ("bank", 45, 100)):
      await session.expectSuccess("createTerrainGrid", {"name": objectName, "size": [64, 64], "spacing": 8, "location": [0, y, 0], "collection": "terrain"})
      await session.expectSuccess("runPython", {"code": f"objectName = {objectName!r}\ndegrees = {degrees}\n" + gradeAcross})
      await session.expectSuccess("assignMaterial", {"objectName": objectName, "materialName": "dirt"})
      await session.expectSuccess("projectUVs", {"objectName": objectName, "method": "box", "worldUnitsPerRepeat": 16})
      await session.expectSuccess("addSurfaceLayer", {"objectName": objectName, "name": "ground"})
      await session.expectSuccess("paintSurface", {"objectName": objectName, "layer": "ground", "material": "grass", "selector": {"all": True}})
      await session.expectSuccess("paintSurface", {"objectName": objectName, "layer": "ground", "material": "rock", "selector": {"box": {"minimum": [0, y - 40, -1], "maximum": [40, y + 40, 1000]}}})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "slopes.blend")})
    return await session.expectSuccess("checkExport", {"path": str(tmp_path / "slopes.eqg"), "purpose": "test"})

  checked = stageBlenderServer.session(steps)
  borders = {finding["object"]: finding for finding in checked["findings"] if finding["finding"] == "border without a transition"}
  # At 65 degrees, which a player walks (up to 71.9), the faces still read as a cliff, where grass meets rock without a strip; at 45 they
  # read as ground, which wants one.
  assert list(borders) == ["bank"]
  assert borders["bank"]["materials"] == ["grass", "rock"] and borders["bank"]["length"] == 64.0


def testTwoKitsOfOneFileNameAreRefusedForTheNamesTheirModelsAndMaterialsWouldShare(stageBlenderServer, tmp_path):
  async def steps(session):
    kits = []
    for folder in ("first", "second"):
      (tmp_path / folder).mkdir()
      kits.append(str(await structurePlots.testKit(session, tmp_path / folder)))
    await structurePlots.testPlot(session, tmp_path)
    for index, kitPath in enumerate(kits):
      await session.expectSuccess("placeKitPiece", {"name": f"plazaWall{index}", "kitPath": kitPath, "piece": "testKitWall25", "location": [30 + 40 * index, -14, 0], "facingDegrees": 0})
    await session.expectSuccess("saveFile", {})
    return await session.expectSuccess("checkExport", {"path": str(tmp_path / "twokits.eqg"), "purpose": "test"})

  checked = stageBlenderServer.session(steps)
  failures = {}
  for failure in checked["failures"]:
    failures.setdefault(failure["failure"], []).append(failure)
  assert sorted(failures) == ["images share a DDS name", "material names collide", "model names collide"]
  # Each kit's wall is a model of its own, so the two would be written under one name.
  assert [failure["models"] for failure in failures["model names collide"]] == [[
    "testKitWall25 (collection in //first\\testKit.blend)", "testKitWall25 (collection in //second\\testKit.blend)",
  ]]
  collided = {failure["materials"][0].split(" ")[0]: failure for failure in failures["material names collide"]}
  assert sorted(collided) == ["testKitStone", "testKitTrim"]
  assert all(len(set(failure["materials"])) == 2 for failure in collided.values())
  assert "would all be named 'testKitStone_testKit'" in collided["testKitStone"]["message"]
