import io
import sys
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
import eqgWriter
from conftest import writePNG

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


async def buildPlot(session, groundTexture, crateTexture, blendPath):
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
  await session.expectSuccess("saveFile", {"path": str(blendPath)})


def testExportedZoneComesBackAsItWasBuilt(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.dds"
  groundTexture.write_bytes(eqgWriter.ddsBytes(patternedRGBA(16, 1)))
  crateTexture = tmp_path / "crate.png"
  Image.fromarray(patternedRGBA(8, 2)).save(crateTexture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, groundTexture, crateTexture, tmp_path / "plot.blend")
    built, _ = await session.expectImage("renderView", {"view": view})
    scene = await session.expectSuccess("getSceneSummary", {})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    imported = await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    again, _ = await session.expectImage("renderView", {"view": view})
    detail = await session.expectSuccess("getObjectDetail", {"name": "testplot"})
    return built, scene, exported, imported, again, detail

  built, scene, exported, imported, again, detail = stageBlenderServer.session(steps)
  assert exported["zone"] == "testplot" and exported["placements"] == 3 and exported["purpose"] == "test" and exported["failures"] == []
  assert sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".partial") == []
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


async def boxWith(session, name, location, material, projected=True):
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [4, 4, 4], "location": location})
  if material is not None:
    await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": material})
  if projected:
    await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 4})


def testExportRefusesEveryHardErrorAtOnceAndKeepsTheLastArchive(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 3)).save(groundTexture)
  archivePath = tmp_path / "testplot.eqg"
  odd = writePNG(tmp_path / "odd.png", 6, 4, (200, 30, 30, 255))
  for folder in ("first", "second"):
    (tmp_path / folder).mkdir()
  stones = [writePNG(tmp_path / folder / "stone.png", 4, 4, (90, 90, 90, 255)) for folder in ("first", "second")]
  lost = writePNG(tmp_path / "lost.png", 4, 4, (10, 200, 10, 255))
  fall = writePNG(tmp_path / "fall.png", 4, 4, (220, 230, 240, 160))
  leafNormal = writePNG(tmp_path / "leaf_n.png", 4, 4, (128, 128, 255, 255))

  async def steps(session):
    await buildPlot(session, groundTexture, groundTexture, tmp_path / "plot.blend")
    await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    previous = archivePath.read_bytes()
    uppercase = await session.expectError("exportZone", {"path": str(tmp_path / "TestPlot.eqg"), "purpose": "test"})
    unknownPurpose = await session.expectError("checkExport", {"path": str(archivePath), "purpose": "final"})
    await session.expectSuccess("transformObjects", {"names": ["pillar"], "scale": [1, 2, 1]})
    for name, texture in (("odd", odd), ("stoneOne", stones[0]), ("stoneTwo", stones[1]), ("lost", lost), ("painted", fall)):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(texture)})
    await session.expectSuccess("createMaterial", {"name": "leaves", "diffuseTexture": str(fall), "normalTexture": str(leafNormal), "cutout": True})
    await session.expectSuccess("createLiquidMaterial", {"name": "falls", "liquid": "waterfall", "diffuseTexture": str(fall)})
    await boxWith(session, "bare", [-40, -40, 0], None, projected=False)
    boxes = (
      ("oddBox", "odd", [-40, 40, 0]), ("stoneBoxOne", "stoneOne", [40, 40, 0]), ("stoneBoxTwo", "stoneTwo", [40, 50, 0]), ("spill", "falls", [50, 40, 0]),
      ("lostBox", "lost", [0, 50, 0]), ("leafBox", "leaves", [-50, 0, 0]), ("paintedBox", "painted", [-50, 20, 0]),
    )
    for name, material, location in boxes:
      await boxWith(session, name, location, material)
    await session.expectSuccess("runPython", {"code": "bpy.data.materials['painted'].node_tree.nodes['zonewrightDiffuse'].image = bpy.data.images.new('painted', 4, 4)"})
    unsaved = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("deleteObjects", {"names": ["paintedBox"]})
    await session.expectSuccess("runPython", {"code": "bpy.data.materials.remove(bpy.data.materials['painted']); bpy.data.images.remove(bpy.data.images['painted'])"})
    await session.expectSuccess("saveFile", {})
    lost.unlink()
    checked = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "test"})
    refused = await session.expectError("exportZone", {"path": str(archivePath), "purpose": "test"})
    return previous, uppercase, unknownPurpose, unsaved, checked, refused

  previous, uppercase, unknownPurpose, unsaved, checked, refused = stageBlenderServer.session(steps)
  assert "lowercase letters and digits" in uppercase and "purpose is one of ['test', 'game'], got 'final'" in unknownPurpose
  assert unsaved["failures"][0] == {"failure": "unsaved changes", "message": "Save the file (saveFile): an export writes the zone as saved"}
  # An image made in memory cannot be saved, so only an unsaved file can hold one; its check names it all the same.
  assert [(failure["failure"], failure["material"], failure["image"], failure["faces"]) for failure in unsaved["failures"] if failure.get("object") == "paintedBox"] == [
    ("image not a file on disk (its source is generated)", "painted", None, 6),
  ]
  # Every hard error at once, each naming its object and where it lies.
  assert sorted((failure["failure"], failure["object"]) for failure in checked["failures"]) == [
    ("cutout with a normal map", "leafBox"), ("faces without a material", "bare"), ("image missing", "lostBox"), ("image sides not powers of two", "oddBox"),
    ("images share a DDS name", "stoneBoxOne"), ("images share a DDS name", "stoneBoxTwo"), ("liquid material off a water body", "spill"),
    ("no texture coordinates", "bare"), ("scale", "pillar"),
  ]
  clashes = [failure for failure in checked["failures"] if failure["failure"] == "images share a DDS name"]
  assert [(clash["material"], clash["image"], clash["ddsName"], clash["images"], clash["faces"]) for clash in clashes] == [
    (material, str(stone), "stone.dds", sorted(str(stone) for stone in stones), 6) for material, stone in zip(("stoneOne", "stoneTwo"), stones)
  ]
  failures = {failure["failure"]: failure for failure in checked["failures"]}
  assert failures["image missing"] == {"failure": "image missing", "object": "lostBox", "material": "lost", "image": str(lost), "faces": 6, "at": [{"center": [0.0, 50.0, 2.0], "faces": 6}], "pieces": 1}
  assert failures["image sides not powers of two"]["size"] == [6, 4] and failures["image sides not powers of two"]["image"] == str(odd)
  assert failures["faces without a material"]["faces"] == 6 and failures["liquid material off a water body"]["material"] == "falls"
  assert "'pillar' is scaled (1.0, 2.0, 1.0); a placed model takes one positive scale" in failures["scale"]["message"] and failures["scale"]["at"] == [30.0, -30.0, 0.0]
  assert "refused, nothing written: 9 failure(s)" in refused and all(label in refused for label in failures)
  # The archive the last good export wrote is still there, unchanged, and no partly written file is left.
  assert archivePath.read_bytes() == previous
  assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".partial")] == []


def testExportRefusesAMissingTerrain(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 3)).save(groundTexture)

  async def steps(session):
    await buildPlot(session, groundTexture, groundTexture, tmp_path / "plot.blend")
    await session.expectSuccess("organize", {"collections": {"ground": "props"}})
    await session.expectSuccess("saveFile", {})
    return await session.expectError("exportZone", {"path": str(tmp_path / "testplot.eqg"), "purpose": "test"})

  noTerrain = stageBlenderServer.session(steps)
  assert "no 'terrain' collection with meshes" in noTerrain and "1 failure(s)" in noTerrain
  assert not (tmp_path / "testplot.eqg").exists()


def testExportLeavesOutWhatIsNotTheZonesOwnAndSaysWhy(stageBlenderServer, tmp_path):
  texture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 4)).save(texture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, texture, texture, tmp_path / "plot.blend")
    await session.expectSuccess("createRegion", {"name": "yard", "outline": [[-30, -30], [30, -30], [30, 30], [-30, 30]], "bottom": -10, "top": 30, "intent": "the yard"})
    await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "visitor", "height": 5, "location": [0, 0, 20], "headingDegrees": 0})
    await session.expectSuccess("placeObject", {"zone": None, "model": "IT10800_ACTORDEF", "name": "kiln", "location": [10, 10, 0], "headingDegrees": 0})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['pillar'].hide_render = True"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mound"})
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "mound", "muted": True})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("setSurfaceLayer", {"objectName": "ground", "name": "path", "muted": True})
    await session.expectSuccess("saveFile", {})
    return await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})

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
