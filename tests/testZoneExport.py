import io
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqgWriter

environment = {
  "ambientColor": [0.3, 0.3, 0.35], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.15], "sunColor": [0.6, 0.5, 0.4],
  "sunAzimuthDegrees": 40, "sunElevationDegrees": 35, "fogColor": [0.5, 0.55, 0.6], "fogStart": 0, "fogEnd": 2000, "fogDensity": 0, "fogOn": False, "maxClip": 4000,
  "newEngineZone": False,
}
view = {"eye": [-70, -80, 55], "target": [10, 0, 5]}


def patternedRGBA(side, seed):
  """A texture whose every texel differs, so a flipped or shifted texture coordinate changes the render."""
  rows, columns = numpy.mgrid[0:side, 0:side]
  rng = numpy.random.default_rng(seed)
  return numpy.stack([rows * (255 // side), columns * (255 // side), rng.integers(0, 256, (side, side)), numpy.full((side, side), 255)], axis=-1).astype(numpy.uint8)


async def buildPlot(session, groundTexture, crateTexture):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [128, 128], "spacing": 16, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [20, 20, 0], "radius": 40, "strength": 12})
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [8, 8, 8], "location": [-20, 10, 0]})
  await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "pillar", "size": [6, 6, 20], "location": [30, -30, 0], "segments": 12})
  await session.expectSuccess("createMaterial", {"name": "groundStone", "diffuseTexture": str(groundTexture)})
  await session.expectSuccess("createMaterial", {"name": "crateWood", "diffuseTexture": str(crateTexture)})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "groundStone"})
  for name in ("crate", "pillar"):
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "crateWood"})
  await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 48, "direction": [0, 0, 1]})
  await session.expectSuccess("projectUVs", {"objectName": "crate", "method": "box", "worldUnitsPerRepeat": 8})
  await session.expectSuccess("projectUVs", {"objectName": "pillar", "method": "box", "worldUnitsPerRepeat": 10})
  copies = await session.expectSuccess("duplicateObjects", {"names": ["crate"], "offset": [15, 5, 0], "linkData": True})
  await session.expectSuccess("transformObjects", {"names": [copies["crate"]], "rotateDegrees": [0, 0, 30], "scale": [1.5, 1.5, 1.5]})
  await session.expectSuccess("setZoneProperties", environment)


def testExportedZoneComesBackAsItWasBuilt(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.dds"
  groundTexture.write_bytes(eqgWriter.ddsBytes(patternedRGBA(16, 1)))
  crateTexture = tmp_path / "crate.png"
  Image.fromarray(patternedRGBA(8, 2)).save(crateTexture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, groundTexture, crateTexture)
    built, _ = await session.expectImage("renderView", {"view": view})
    scene = await session.expectSuccess("getSceneSummary", {})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath)})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    again, _ = await session.expectImage("renderView", {"view": view})
    detail = await session.expectSuccess("getObjectDetail", {"name": "testplot"})
    return built, scene, exported, imported, again, detail

  built, scene, exported, imported, again, detail = stageBlenderServer.session(steps)
  assert exported["zone"] == "testplot" and exported["placements"] == 3
  assert exported["modelTriangles"] == {"obj_crate.mod": 12, "obj_pillar.mod": 44}
  assert exported["textures"] == ["crate.dds", "ground.dds"] and exported["materials"] == ["crateWood", "groundStone"]
  sceneTriangles = {sceneObject["name"]: sceneObject["triangles"] for sceneObject in scene["objects"]}
  assert exported["terrainTriangles"] == sceneTriangles["ground"]

  archive = eqArchive.EQArchive(archivePath)
  assert sorted(archive.entries) == ["crate.dds", "ground.dds", "obj_crate.mod", "obj_pillar.mod", "ter_testplot.ter", "testplot.zon"]
  # A DDS texture is stored as it was; another image becomes an uncompressed DDS of its pixels.
  assert archive.read("ground.dds") == groundTexture.read_bytes()
  assert numpy.array_equal(numpy.asarray(Image.open(io.BytesIO(archive.read("crate.dds"))).convert("RGBA")), patternedRGBA(8, 2))
  zone = eqgFiles.parseZone(archive.read("testplot.zon"), "testplot.zon")
  assert zone["version"] == 1 and zone["placements"][0]["name"] == "TER_testplot"
  assert sorted(placement["model"] for placement in zone["placements"][1:]) == ["obj_crate.mod", "obj_crate.mod", "obj_pillar.mod"]
  turned = next(placement for placement in zone["placements"] if placement["scale"] != 1)
  assert turned["model"] == "obj_crate.mod" and turned["scale"] == 1.5
  assert abs(turned["rotation"][0] - numpy.radians(30)) < 1e-6 and turned["rotation"][1:] == (0.0, 0.0)

  source = imported["source"]
  assert {key: source[key] for key in ("zoneVersion", "placements", "placedObjects", "missingModels", "bakedLightNotFitting", "missingTextures", "droppedTriangles")} == {
    "zoneVersion": 1, "placements": 4, "placedObjects": 4, "missingModels": [], "bakedLightNotFitting": [], "missingTextures": [], "droppedTriangles": 0,
  }
  assert detail["triangles"] == sum(sceneTriangles.values())
  # The archive draws as the scene it came from: the same pixels within a level, outside a few antialiased edge pixels.
  difference = numpy.abs(numpy.asarray(Image.open(io.BytesIO(built)).convert("RGB"), dtype=numpy.int64) - numpy.asarray(Image.open(io.BytesIO(again)).convert("RGB"), dtype=numpy.int64))
  assert difference.mean() < 0.5, difference.mean()
  assert numpy.percentile(difference.max(axis=-1), 99) <= 2, numpy.percentile(difference.max(axis=-1), 99)


flipCode = """
import bmesh
crate = bpy.data.objects['crate']
editor = bmesh.new()
editor.from_mesh(crate.data)
flipped = [face for face in editor.faces if face.normal.y < -0.5 or face.normal.z > 0.5]
bmesh.ops.reverse_faces(editor, faces=flipped)
editor.to_mesh(crate.data)
editor.free()
crate.data.update()
result = len(flipped)
"""


def testFacesTurnedInsideOutDrawAlikeBuiltAndExported(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.dds"
  groundTexture.write_bytes(eqgWriter.ddsBytes(patternedRGBA(16, 1)))
  crateTexture = tmp_path / "crate.png"
  Image.fromarray(patternedRGBA(8, 2)).save(crateTexture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, groundTexture, crateTexture)
    flipped = (await session.expectSuccess("runPython", {"code": flipCode}))["result"]
    built, _ = await session.expectImage("renderView", {"view": crateView})
    await session.expectSuccess("exportZone", {"path": str(archivePath)})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    again, _ = await session.expectImage("renderView", {"view": crateView})
    return flipped, built, again

  crateView = {"eye": [-20, -12, 12], "target": [-20, 10, 4]}
  flipped, built, again = stageBlenderServer.session(steps)
  # The crate's faces toward the camera and the sky, which fill most of the view, are turned inside out. Seen from behind, each is lit
  # by its own normal, built as the zone file's stored normal lights it once exported, so the two draw alike.
  assert flipped == 2
  difference = numpy.abs(numpy.asarray(Image.open(io.BytesIO(built)).convert("RGB"), dtype=numpy.int64) - numpy.asarray(Image.open(io.BytesIO(again)).convert("RGB"), dtype=numpy.int64))
  assert difference.mean() < 0.5, difference.mean()
  assert numpy.percentile(difference.max(axis=-1), 99) <= 2, numpy.percentile(difference.max(axis=-1), 99)


def testExportRefusesWhatAZoneFileCannotHold(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 3)).save(groundTexture)

  async def steps(session):
    await buildPlot(session, groundTexture, groundTexture)
    uppercase = await session.expectError("exportZone", {"path": str(tmp_path / "TestPlot.eqg")})
    await session.expectSuccess("transformObjects", {"names": ["pillar"], "scale": [1, 2, 1]})
    stretched = await session.expectError("exportZone", {"path": str(tmp_path / "testplot.eqg")})
    await session.expectSuccess("transformObjects", {"names": ["pillar"], "scale": [1, 0.5, 1]})
    await session.expectSuccess("organize", {"collections": {"ground": "props"}})
    noTerrain = await session.expectError("exportZone", {"path": str(tmp_path / "testplot.eqg")})
    return uppercase, stretched, noTerrain

  uppercase, stretched, noTerrain = stageBlenderServer.session(steps)
  assert "lowercase letters and digits" in uppercase
  assert "'pillar' is scaled (1.0, 2.0, 1.0); a placed model takes one positive scale" in stretched
  assert "no 'terrain' collection with meshes" in noTerrain
  assert not (tmp_path / "testplot.eqg").exists()


def testExportLeavesOutWhatIsNotTheZonesOwnAndSaysWhy(stageBlenderServer, tmp_path):
  texture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 4)).save(texture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, texture, texture)
    await session.expectSuccess("createRegion", {"name": "yard", "outline": [[-30, -30], [30, -30], [30, 30], [-30, 30]], "bottom": -10, "top": 30, "intent": "the yard"})
    await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "visitor", "height": 5, "location": [0, 0, 20], "headingDegrees": 0})
    await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "kiln", "location": [10, 10, 0], "headingDegrees": 0})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['pillar'].hide_render = True"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mound"})
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "mound", "muted": True})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("setSurfaceLayer", {"objectName": "ground", "name": "path", "muted": True})
    return await session.expectSuccess("exportZone", {"path": str(archivePath)})

  exported = stageBlenderServer.session(steps)
  assert exported["excluded"] == [
    {"reason": "a client object: client models do not export yet", "count": 1, "objects": ["kiln"]},
    {"reason": "a client spawn: the server's data, not zone geometry", "count": 1, "objects": ["visitor"]},
    {"reason": "a region: the plan", "count": 1, "objects": ["yard"]},
    {"reason": "hidden from renders", "count": 1, "objects": ["pillar"]},
  ]
  assert exported["toConfirm"] == [{"object": "ground", "passesOff": ["mound"], "layersMuted": ["path"]}]
  assert exported["modelTriangles"] == {"obj_crate.mod": 12}
  assert sorted(eqArchive.EQArchive(archivePath).entries) == ["ground.dds", "obj_crate.mod", "ter_testplot.ter", "testplot.zon"]
