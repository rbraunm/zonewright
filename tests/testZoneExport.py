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
from testHousing import decision, flatGround
from testModelsAndDressing import freshScene

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
  keys = ("zoneVersion", "placements", "placedObjects", "missingModels", "bakedLightNotFitting", "bakedLightPastFileEnd", "animatedModels", "missingTextures", "droppedTriangles")
  assert {key: source[key] for key in keys} == {
    "zoneVersion": 1, "placements": 4, "placedObjects": 4, "missingModels": [], "bakedLightNotFitting": {}, "bakedLightPastFileEnd": {}, "animatedModels": {},
    "missingTextures": [], "droppedTriangles": 0,
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


olderGroupCode = """
tree = bpy.data.node_groups['eqClientLight']
tree.interface.remove(next(item for item in tree.interface.items_tree if item.item_type == 'SOCKET' and item.name == 'Stored'))
tree['eqVersion'] = 6
result = sum(1 for material in bpy.data.materials if material.node_tree for node in material.node_tree.nodes if node.type == 'GROUP' and node.node_tree == tree)
"""
storedCode = """
rows = set()
for material in bpy.data.materials:
  nodes = material.node_tree.nodes if material.node_tree else []
  storedNormal = any(node.type == 'ATTRIBUTE' and node.attribute_name == 'eqNormal' for node in nodes)
  for node in nodes:
    if node.type == 'GROUP' and node.node_tree.name == 'eqClientLight':
      rows.add((storedNormal, node.inputs['Stored'].default_value))
result = sorted(rows)
"""


def testFacesTurnedInsideOutDrawAlikeBuiltAndExported(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.dds"
  groundTexture.write_bytes(eqgWriter.ddsBytes(patternedRGBA(16, 1)))
  crateTexture = tmp_path / "crate.png"
  Image.fromarray(patternedRGBA(8, 2)).save(crateTexture)
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await buildPlot(session, groundTexture, crateTexture, tmp_path / "plot.blend")
    flipped = (await session.expectSuccess("runPython", {"code": flipCode}))["result"]
    await session.expectSuccess("saveFile", {})
    built, _ = await session.expectImage("renderView", {"view": crateView})
    await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    await session.expectSuccess("setZoneProperties", environment)
    again, _ = await session.expectImage("renderView", {"view": crateView})
    olderUses = (await session.expectSuccess("runPython", {"code": olderGroupCode}))["result"]
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "older.blend")})
    await session.expectSuccess("openFile", {"path": str(tmp_path / "older.blend")})
    reopened, _ = await session.expectImage("renderView", {"view": crateView})
    stored = (await session.expectSuccess("runPython", {"code": storedCode}))["result"]
    return flipped, built, again, olderUses, reopened, stored

  def pixels(image):
    return numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.int64)

  crateView = {"eye": [-20, -12, 12], "target": [-20, 10, 4]}
  flipped, built, again, olderUses, reopened, stored = stageBlenderServer.session(steps)
  # The crate's faces toward the camera and the sky, which fill most of the view, are turned inside out. Seen from behind, each is lit
  # by its own normal, built as the zone file's stored normal lights it once exported, so the two draw alike.
  assert flipped == 2
  difference = numpy.abs(pixels(built) - pixels(again))
  assert difference.mean() < 0.5, difference.mean()
  assert numpy.percentile(difference.max(axis=-1), 99) <= 2, numpy.percentile(difference.max(axis=-1), 99)
  # A file saved before the lighting group took Stored has the group's older build and no Stored on its materials. Opened again, the
  # group is rebuilt and each material's Stored is read from its own nodes: the imported meshes light by their stored normals and the
  # crate draws as it did before it was saved.
  assert olderUses > 0
  assert stored == [[True, 1.0]]
  assert numpy.abs(pixels(reopened) - pixels(again)).max() == 0


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
  # An image made in memory cannot be saved, so only an unsaved file can hold one; its check names it by its image name.
  assert [(failure["failure"], failure["material"], failure["image"], failure["faces"]) for failure in unsaved["failures"] if failure.get("object") == "paintedBox"] == [
    ("image not a file on disk (its source is generated)", "painted", "painted", 6),
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


packImage = """
bpy.data.materials['packed'].node_tree.nodes['zonewrightDiffuse'].image.pack()
"""
foreignMaterial = """
bpy.data.materials.new('plain')
"""
kitWithAMarker = """
kit = bpy.data.collections.new('kit')
kit.objects.link(bpy.data.objects.new('kitBox', bpy.data.objects['crate'].data))
kit.objects.link(bpy.data.objects.new('kitMarker', None))
placed = bpy.data.objects.new('kitPlaced', None)
placed.instance_type = 'COLLECTION'
placed.instance_collection = kit
placed.location = (40, 40, 0)
bpy.context.scene.collection.objects.link(placed)
"""
subdivideLayered = """
bpy.data.objects['layered'].modifiers.new('rounder', 'SUBSURF')
"""
ghostWall = """
import json
ghost = bpy.data.objects.new('ghostWall', None)
ghost['zonewrightBoundary'] = json.dumps({'kind': 'wall'})
ghost.location = (0, -50, 0)
bpy.context.scene.collection.objects.link(ghost)
bpy.data.objects['bareLid'].data.clear_geometry()
"""


def testExportNamesEveryOtherHardErrorWithItsObject(stageBlenderServer, tmp_path):
  groundTexture = tmp_path / "ground.png"
  Image.fromarray(patternedRGBA(16, 3)).save(groundTexture)
  packed = writePNG(tmp_path / "packed.png", 4, 4, (200, 200, 30, 255))
  far = writePNG(tmp_path / "far.png", 4, 4, (30, 30, 200, 255))
  drive, rest = str(far).split(":", 1)
  farOnAShare = f"\\\\localhost\\{drive.lower()}${rest}"
  fake = tmp_path / "fake.png"
  fake.write_bytes(eqgWriter.ddsBytes(patternedRGBA(4, 5)))
  archivePath = tmp_path / "testplot.eqg"

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [64, 64], "spacing": 16, "location": [0, 0, 0], "collection": "terrain"})
    neverSaved = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "test"})
    await buildPlot(session, groundTexture, groundTexture, tmp_path / "plot.blend")
    for name, texture in (("packed", packed), ("far", farOnAShare), ("fakeDDS", fake)):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(texture)})
    await session.expectSuccess("runPython", {"code": packImage + foreignMaterial})
    boxes = (
      ("packedBox", "packed", [-40, 40, 0]), ("farBox", "far", [40, 40, 0]), ("fakeBox", "fakeDDS", [40, -40, 0]), ("foreignBox", "plain", [-40, -40, 0]),
      ("Twin", "crateWood", [0, 40, 0]), ("twin", "crateWood", [0, -40, 0]), ("layered", "crateWood", [50, 0, 0]),
    )
    for name, material, location in boxes:
      await boxWith(session, name, location, material)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "layered", "name": "paint"})
    await session.expectSuccess("paintSurface", {"objectName": "layered", "layer": "paint", "material": "groundStone", "selector": {"all": True}})
    await session.expectSuccess("placeBoundaryPlane", {"name": "bareLid", "kind": "lid", "outline": [[-60, 50], [-50, 50], [-50, 60], [-60, 60]], "height": 30})
    await session.expectSuccess("placeZoneLine", {"number": 1, "label": "gate", "minimum": [56, -10, 0], "maximum": [64, 10, 30], "target": {"zone": "highpasshold", "x": 0, "y": 0, "z": 0, "headingDegrees": 0}})
    await session.expectSuccess("placeSwimVolume", {"name": "pond", "liquid": "water", "minimum": [-30, -10, -5], "maximum": [-10, 10, 0]})
    await session.expectSuccess("transformObjects", {"names": ["ATP_1_gate", "AWT_pond"], "rotateDegrees": [0, 0, 10]})
    await session.expectSuccess("runPython", {"code": kitWithAMarker + subdivideLayered + ghostWall})
    return neverSaved, await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "test"})

  neverSaved, checked = stageBlenderServer.session(steps)
  assert neverSaved["failures"][0] == {"failure": "never saved", "message": "Save the file (saveFile with a path): an export writes the zone as saved"}
  assert checked["failures"][0]["failure"] == "unsaved changes"
  assert sorted((failure["failure"], failure.get("object") or "") for failure in checked["failures"][1:]) == sorted([
    ("boundary", "bareLid"), ("boundary", "ghostWall"), ("collection holds more than meshes", "kitPlaced"),
    ("DDS data under another extension", "fakeBox"), ("image on another drive than the .blend", "farBox"), ("image packed into the .blend", "packedBox"),
    ("material not made by createMaterial or createLiquidMaterial", "foreignBox"), ("model names collide", ""),
    ("surfacing layers unlike the exported faces", "layered"), ("swim volume", "AWT_pond"), ("zone line", "ATP_1_gate"),
  ])
  failures = {failure.get("object"): failure for failure in checked["failures"][1:]}
  assert failures["packedBox"] == {
    "failure": "image packed into the .blend", "object": "packedBox", "material": "packed", "image": str(packed), "faces": 6, "at": [{"center": [-40.0, 40.0, 2.0], "faces": 6}], "pieces": 1,
  }
  assert failures["farBox"]["image"].lower() == farOnAShare.lower() and failures["farBox"]["faces"] == 6
  assert failures["fakeBox"]["image"] == str(fake) and failures["fakeBox"]["at"] == [{"center": [40.0, -40.0, 2.0], "faces": 6}]
  assert failures["foreignBox"]["material"] == "plain" and failures["foreignBox"]["faces"] == 6
  assert failures[None]["models"] == ["Twin", "twin"]
  assert failures["kitPlaced"] == {"failure": "collection holds more than meshes", "object": "kitPlaced", "at": [40.0, 40.0, 0.0], "collection": "kit", "members": ["kitMarker"]}
  assert "'layered' has modifiers that change its faces" in failures["layered"]["message"]
  assert failures["ghostWall"] == {"failure": "boundary", "object": "ghostWall", "at": [0.0, -50.0, 0.0], "message": "'ghostWall' is a boundary but a EMPTY; boundaries are meshes"}
  assert failures["bareLid"] == {"failure": "boundary", "object": "bareLid", "at": [0.0, 0.0, 0.0], "message": "Boundary 'bareLid' has no faces"}
  assert failures["ATP_1_gate"] == {"failure": "zone line", "object": "ATP_1_gate", "at": [60.0, 0.0, 15.0], "message": "'ATP_1_gate' is turned; zone lines stay square to the axes"}
  assert failures["AWT_pond"] == {"failure": "swim volume", "object": "AWT_pond", "at": [-20.0, 0.0, -2.5], "message": "'AWT_pond' is turned; swim volumes stay square to the axes"}


replaceLaterEmitters = """
bpy.data.objects['campfire01'].location = (-30, 40, 1)
"""


def testAFailedExportPutsBackTheLastArchiveAndItsSideFiles(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "teststreet.eqg"
  sideFiles = [tmp_path / f"teststreet{suffix}" for suffix in ("_EnvironmentEmitters.txt", "_housing.json", "_assets.txt")]

  def files():
    return {path.name: path.read_bytes() for path in [archivePath, *sideFiles]}

  async def steps(session):
    await freshScene(session)
    await flatGround(session, tmp_path)
    await session.expectSuccess("setZoneHousing", decision)
    laid = await session.expectSuccess("layOutPlots", {"street": "Main Street", "path": [[-300, 0], [300, 0]], "side": "both"})
    await session.expectSuccess("placeEmitters", {"emitters": [{"name": "campfire01", "position": [5, 5, 1], "definition": 259, "lifespan": 4000000}]})
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "teststreet.blend")})
    await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    before = files()
    await session.expectSuccess("runPython", {"code": replaceLaterEmitters})
    await session.expectSuccess("placeLights", {"lights": [{"name": "LIT_lamp01", "position": [0, -40, 8], "color": [1, 0.8, 0.5], "radius": 40}]})
    await session.expectSuccess("removePlot", {"address": laid["placed"][0]["address"]})
    await session.expectSuccess("saveFile", {})
    with archivePath.open("rb"):
      held = await session.expectError("exportZone", {"path": str(archivePath), "purpose": "test"})
    after = files()
    left = sorted(path.name for path in tmp_path.iterdir() if path.suffix in (".partial", ".previous"))
    await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    return before, held, after, left, files()

  before, held, after, left, released = stageBlenderServer.session(steps)
  # The archive is replaced last, after every side file took its new place: held open, it fails there, and every file goes back.
  assert "PermissionError" in held
  assert after == before and left == []
  assert all(released[name] != before[name] for name in ("teststreet.eqg", "teststreet_EnvironmentEmitters.txt", "teststreet_housing.json"))


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
  assert exported["toConfirm"] == [{"object": "ground", "passesOff": ["mound"], "layersMuted": ["path"], "staleCaves": [], "staleDefinedPasses": []}]
  assert exported["modelTriangles"] == {"obj_crate.mod": 12}
  assert sorted(eqArchive.EQArchive(archivePath).entries) == ["ground.dds", "obj_crate.mod", "ter_testplot.ter", "testplot.zon"]
