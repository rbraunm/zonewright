import math
import sys
from pathlib import Path

import numpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
from conftest import writePNG
from testModelsAndDressing import freshScene, readShapedMesh

readGroundUnderBoundary = """
import bmesh, mathutils
water = bpy.data.objects[waterName]
editor = bmesh.new()
editor.from_mesh(water.data)
boundary = [vertex.co.copy() for vertex in editor.verts if vertex.is_boundary]
editor.free()
water.hide_viewport = True
depsgraph = bpy.context.evaluated_depsgraph_get()
heights = []
for point in boundary:
  hit = bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((point.x, point.y, 1000)), mathutils.Vector((0, 0, -1)))
  heights.append(None if not hit[0] else hit[1].z)
water.hide_viewport = False
result = {"levels": [point.z for point in boundary], "ground": heights}
"""


async def liquidMaterials(session, tmp_path):
  diffuse = writePNG(tmp_path / "water_c.png", 4, 4, (40, 90, 110, 255))
  normal = writePNG(tmp_path / "water_n.png", 4, 4, (128, 128, 255, 255))
  environment = writePNG(tmp_path / "water_e.png", 4, 4, (200, 150, 100, 255))
  fall = writePNG(tmp_path / "fall_c.png", 4, 4, (220, 230, 240, 160))
  await session.expectSuccess("createLiquidMaterial", {"name": "water", "liquid": "water", "diffuseTexture": str(diffuse), "normalTexture": str(normal), "environmentTexture": str(environment)})
  await session.expectSuccess("createLiquidMaterial", {"name": "falls", "liquid": "waterfall", "diffuseTexture": str(fall)})


async def basin(session):
  """A flat grid at 0 with a cone-shaped basin at the origin: -20 at its middle, rising evenly to 0 at 100 out."""
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 400], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[0, 0, -20]], "radius": 100, "strength": 1, "profile": [[0, 0], [1, 20]], "conformRim": False})


async def meshOf(session, name):
  return (await session.expectSuccess("runPython", {"code": f"objectName = {name!r}" + readShapedMesh}))["result"]


def testPoolFloodsItsBasinToItsLevelUnderItsBanks(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    flooded = await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    atFive = await meshOf(session, "pool")
    banks = (await session.expectSuccess("runPython", {"code": "waterName = 'pool'" + readGroundUnderBoundary}))["result"]
    lowered = await session.expectSuccess("editWater", {"name": "pool", "level": -10})
    atTen = await meshOf(session, "pool")
    volumes = await session.expectSuccess("getWaterVolumes", {"name": "pool"})
    halved = await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "remove", "area": {"polygon": [[12, -200], [200, -200], [200, 200], [12, 200]]}})
    westOnly = await meshOf(session, "pool")
    dry = await session.expectError("floodWater", {"name": "dryPool", "seed": [90, 0], "level": -5, "material": "water"})
    wrongLiquid = await session.expectError("floodWater", {"name": "fallPool", "seed": [0, 0], "level": -5, "material": "falls"})
    everywhere = await session.expectSuccess("floodWater", {"name": "sea", "seed": [150, 150], "level": 1, "material": "water"})
    return flooded, atFive, banks, lowered, atTen, volumes, halved, westOnly, dry, wrongLiquid, everywhere

  flooded, atFive, banks, lowered, atTen, volumes, halved, westOnly, dry, wrongLiquid, everywhere = stageBlenderServer.session(steps)
  # The cone stands below -5 within 75 of its middle: the surface lies flat at the level over all of that, reaching at most a few cells
  # past it so its edge lies under the banks.
  five = numpy.array(atFive["vertices"])
  assert numpy.allclose(five[:, 2], -5) and five[:, 0].max() >= 75 and numpy.linalg.norm(five[:, :2], axis=1).max() <= 75 + 3 * 8 * math.sqrt(2)
  assert all(ground is not None and ground >= level - 1e-6 for level, ground in zip(banks["levels"], banks["ground"]))
  assert flooded["built"]["deepest"] == 15 and flooded["built"]["reachesGroundEnd"] == []
  ten = numpy.array(atTen["vertices"])
  assert numpy.allclose(ten[:, 2], -10) and ten[:, 0].max() < five[:, 0].max()
  boxes = volumes["volumes"]
  assert all(box["name"].startswith("AWT_pool") for box in boxes)
  assert all(abs(box["center"][2] + box["halfExtents"][2] - -10) <= 1 for box in boxes)
  assert any(all(abs(box["center"][axis]) <= box["halfExtents"][axis] for axis in (0, 1)) for box in boxes)
  assert min(box["center"][2] - box["halfExtents"][2] for box in boxes) <= -20 - 4 + 0.01
  # The stroke takes the east of the pool away; the surface stops within a cell of its edge.
  assert numpy.array(westOnly["vertices"])[:, 0].max() <= 16 and halved["definition"]["strokes"][0]["mode"] == "remove"
  assert "the ground is at -2.0 and the level -5.0" in dry
  assert "A pool takes a liquid material" in wrongLiquid and "is waterfall" in wrongLiquid
  assert everywhere["built"]["groundEndPoints"] > 0


def testRiverRunsBetweenItsEndsFallingWithItsPath(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-200, 4, -10], [200, 4, -10]], "radius": 20, "strength": 1, "profile": [[0, 0], [1, 10]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    river = await session.expectSuccess("runWater", {"name": "river", "path": [[-100, 4, -4], [100, 4, -6]], "reach": 30, "material": "water"})
    shape = await meshOf(session, "river")
    return river, shape

  river, shape = stageBlenderServer.session(steps)
  vertices = numpy.array(shape["vertices"])
  # Cut square across at both ends of its path, and falling evenly along it from -4 to -6.
  assert vertices[:, 0].min() >= -100 - 1e-3 and vertices[:, 0].max() <= 100 + 1e-3
  assert numpy.isclose(vertices[:, 0].min(), -100, atol=1e-3) and numpy.isclose(vertices[:, 0].max(), 100, atol=1e-3)
  assert numpy.allclose(vertices[:, 2], -4 - (vertices[:, 0] + 100) / 200 * 2, atol=1e-3)
  # Within reach of the path only where the channel is below its level.
  assert numpy.abs(vertices[:, 1] - 4).max() <= 20 + 2 * 8
  assert river["built"]["deepest"] <= 6 + 1e-6


def testFallHangsFromItsLipAndRefusesOneRunningBackwards(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 4, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-100, 0], [100, 0], [100, 100], [-100, 100]], "base": 0, "profile": [[-2, 0], [0, 40], [200, 40]], "conformBreaks": False})
    await liquidMaterials(session, tmp_path)
    fall = await session.expectSuccess("pourWaterfall", {"name": "fall", "lip": [[-20, 0, 41], [20, 0, 41]], "bottom": 0, "throw": 6, "material": "falls"})
    shape = await meshOf(session, "fall")
    backwards = await session.expectError("pourWaterfall", {"name": "backwards", "lip": [[20, 0, 41], [-20, 0, 41]], "bottom": 0, "material": "falls"})
    belowLip = await session.expectError("pourWaterfall", {"name": "upwards", "lip": [[-20, 0, 41], [20, 0, 41]], "bottom": 45, "material": "falls"})
    wider = await session.expectSuccess("editWater", {"name": "fall", "spread": 1.5})
    widerShape = await meshOf(session, "fall")
    return fall, shape, backwards, belowLip, wider, widerShape

  fall, shape, backwards, belowLip, wider, widerShape = stageBlenderServer.session(steps)
  vertices = numpy.array(shape["vertices"])
  top, bottom = vertices[numpy.isclose(vertices[:, 2], 41)], vertices[numpy.isclose(vertices[:, 2], 0)]
  # It starts lipLeadIn behind the lip, turns over at the lip, and lands `throw` out in front, as wide as the lip.
  assert sorted({round(y, 3) for y in top[:, 1]}) == [0.0, 4.0]
  assert numpy.allclose(bottom[:, 1], -6) and numpy.isclose(bottom[:, 0].min(), -20) and numpy.isclose(bottom[:, 0].max(), 20)
  assert fall["built"]["drop"] == 41 and fall["built"]["rows"] == 6 and fall["built"]["insideRock"]["vertices"] == 0
  assert "reverse it" in backwards and "must lie below every point of its lip" in belowLip
  widerBottom = numpy.array(widerShape["vertices"])[numpy.isclose(numpy.array(widerShape["vertices"])[:, 2], 0)]
  assert numpy.isclose(widerBottom[:, 0].max(), 30) and wider["definition"]["spread"] == 1.5


def testWaterBedSinksUnderTheWaterAndTheSelectorsFindBedAndBanks(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    before = await meshOf(session, "ground")
    carved = await session.expectSuccess("carveWaterBed", {"name": "pool", "objectName": "ground", "depth": 6, "shoreWidth": 20})
    after = await meshOf(session, "ground")
    stone = writePNG(tmp_path / "stone.png", 4, 4, (120, 120, 120, 255))
    sand = writePNG(tmp_path / "sand.png", 4, 4, (200, 180, 120, 255))
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(stone)})
    await session.expectSuccess("createMaterial", {"name": "sand", "diffuseTexture": str(sand)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone"})
    bed = await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand", "selector": {"underWater": "pool"}})
    banks = await session.expectError("assignMaterial", {"objectName": "ground", "materialName": "sand", "selector": {"nearWater": {"water": "pool", "distance": 0}}})
    wet = await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone", "selector": {"nearWater": {"water": "pool", "distance": 10}}})
    return before, carved, after, bed, banks, wet

  before, carved, after, bed, banks, wet = stageBlenderServer.session(steps)
  old, new = numpy.array(before["vertices"]), numpy.array(after["vertices"])
  radius = numpy.linalg.norm(old[:, :2], axis=1)
  assert (new[:, 2] <= old[:, 2] + 1e-6).all()
  assert numpy.allclose(new[radius > 80, 2], old[radius > 80, 2])
  # 60 out the cone stands at -8, about 15 in from the shore: lowered toward -11 by most of the depth.
  ring = (radius > 58) & (radius < 62)
  assert (new[ring, 2] < -9.5).all() and (old[ring, 2] > -8.5).all()
  assert numpy.allclose(new[radius < 20, 2], old[radius < 20, 2]) and carved["deepestBed"] == -20
  assert bed["faces"] > 0 and "distance must be positive" in banks and wet["faces"] > 0


def testWaterExportsTheClientsShadersAndSwimVolumes(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "testwater.eqg"

  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    ground = writePNG(tmp_path / "ground.png", 4, 4, (90, 120, 60, 255))
    await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(ground)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    await session.expectSuccess("pourWaterfall", {"name": "fall", "lip": [[-10, 60, 20], [10, 60, 20]], "bottom": -5, "throw": 0, "material": "falls"})
    missing = await session.expectError("createLiquidMaterial", {"name": "flat", "liquid": "water", "diffuseTexture": str(tmp_path / "water_c.png"), "normalTexture": str(tmp_path / "water_n.png")})
    stray = await session.expectError("createLiquidMaterial", {"name": "fallBias", "liquid": "waterfall", "diffuseTexture": str(tmp_path / "fall_c.png"), "fresnelBias": 0.3})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath)})
    return missing, stray, exported

  missing, stray, exported = stageBlenderServer.session(steps)
  assert "missing ['environment']" in missing and "does not take ['fresnelBias']" in stray
  archive = eqArchive.EQArchive(archivePath)
  zone = eqgFiles.parseZone(archive.read("testwater.zon"), "testwater.zon")
  assert exported["regions"] == [region["name"] for region in zone["regions"]] and all(name.startswith("AWT_pool") for name in exported["regions"])
  assert all(abs(region["center"][2] + region["halfExtents"][2] - -5) <= 1 and region["rotation"] == (0.0, 0.0, 0.0) for region in zone["regions"])
  pool, fall = (eqgFiles.parseModel(archive.read(f"obj_{name}.mod"), name)["materials"][0] for name in ("pool", "fall"))
  assert pool["shader"] == "Opaque_MaxWater.fx" and fall["shader"] == "Opaque_MaxWaterFall.fx"
  assert pool["properties"]["e_TextureEnvironment0"] == "water_e.dds" and pool["properties"]["e_fWaterColor1"] == 0xFF000A1C
  assert exported["housing"] is None


simulateOlderGroup = """
tree = bpy.data.node_groups["eqClientLight"]
tree["eqVersion"] = 4
tree.interface.remove(tree.interface.items_tree["Added"])
result = [item.name for item in tree.interface.items_tree]
"""


def testMaterialsSavedBeforeTheAddedLightInputDrawUnchanged(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    grass = writePNG(tmp_path / "grass.png", 4, 4, (90, 120, 60, 255))
    await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(grass)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
    await session.expectSuccess("setZoneProperties", {
      "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
      "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000, "newEngineZone": False,
    })
    view = {"eye": [-150, -150, 80], "target": [0, 0, -10]}
    current, _ = await session.expectImage("renderView", {"view": view})
    older = (await session.expectSuccess("runPython", {"code": simulateOlderGroup}))["result"]
    rebuilt, _ = await session.expectImage("renderView", {"view": view})
    return current, older, rebuilt

  current, older, rebuilt = stageBlenderServer.session(steps)
  # A group saved before the added light input existed is rebuilt with it, and what it draws does not change.
  assert "Added" not in older and rebuilt == current
