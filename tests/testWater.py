import io
import math
import sys
import time
from pathlib import Path

import numpy
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import eqArchive
import eqgFiles
from conftest import writePNG
from testModelsAndDressing import freshScene, readShapedMesh

environment = {
  "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
  "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 3000, "fogDensity": 0.1, "fogOn": True, "maxClip": 6000,
  "newEngineZone": False,
}

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
    halved = await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "remove", "area": {"polygon": [[12, -200], [200, -200], [200, 200], [12, 200]]}})
    westOnly = await meshOf(session, "pool")
    dry = await session.expectError("floodWater", {"name": "dryPool", "seed": [90, 0], "level": -5, "material": "water"})
    wrongLiquid = await session.expectError("floodWater", {"name": "fallPool", "seed": [0, 0], "level": -5, "material": "falls"})
    everywhere = await session.expectSuccess("floodWater", {"name": "sea", "seed": [150, 150], "level": 1, "material": "water"})
    return flooded, atFive, banks, lowered, atTen, halved, westOnly, dry, wrongLiquid, everywhere

  flooded, atFive, banks, lowered, atTen, halved, westOnly, dry, wrongLiquid, everywhere = stageBlenderServer.session(steps)
  # The cone stands below -5 within 75 of its middle: the surface lies flat at the level over all of that, reaching at most a few cells
  # past it so its edge lies under the banks.
  five = numpy.array(atFive["vertices"])
  assert numpy.allclose(five[:, 2], -5) and five[:, 0].max() >= 75 and numpy.linalg.norm(five[:, :2], axis=1).max() <= 75 + 3 * 8 * math.sqrt(2)
  assert all(ground is not None and ground >= level - 1e-6 for level, ground in zip(banks["levels"], banks["ground"]))
  assert flooded["built"]["deepest"] == 15 and flooded["built"]["reachesGroundEnd"] == []
  ten = numpy.array(atTen["vertices"])
  assert numpy.allclose(ten[:, 2], -10) and ten[:, 0].max() < five[:, 0].max()
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
    plunging = await session.expectSuccess("editWater", {"name": "fall", "spread": 1, "throw": 0})
    thrown = await session.expectSuccess("editWater", {"name": "fall", "throw": 10})
    thrownShape = await meshOf(session, "fall")
    widerLip = await session.expectSuccess("editWater", {"name": "fall", "lip": [[-30, 0, 41], [30, 0, 41]]})
    widerLipShape = await meshOf(session, "fall")
    return fall, shape, backwards, belowLip, wider, widerShape, plunging, thrown, thrownShape, widerLip, widerLipShape

  fall, shape, backwards, belowLip, wider, widerShape, plunging, thrown, thrownShape, widerLip, widerLipShape = stageBlenderServer.session(steps)
  vertices = numpy.array(shape["vertices"])
  top, bottom = vertices[numpy.isclose(vertices[:, 2], 41)], vertices[numpy.isclose(vertices[:, 2], 0)]
  # It starts lipLeadIn behind the lip, turns over at the lip, and lands `throw` out in front, as wide as the lip.
  assert sorted({round(y, 3) for y in top[:, 1]}) == [0.0, 4.0]
  assert numpy.allclose(bottom[:, 1], -6) and numpy.isclose(bottom[:, 0].min(), -20) and numpy.isclose(bottom[:, 0].max(), 20)
  assert fall["built"]["drop"] == 41 and fall["built"]["rows"] == 6 and fall["built"]["insideRock"]["vertices"] == 0
  assert "reverse it" in backwards and "must lie below every point of its lip" in belowLip
  widerBottom = numpy.array(widerShape["vertices"])[numpy.isclose(numpy.array(widerShape["vertices"])[:, 2], 0)]
  assert numpy.isclose(widerBottom[:, 0].max(), 30) and wider["definition"]["spread"] == 1.5
  # Dropping straight down the cliff face, the sheet runs inside the rock under the lip; thrown 10 out it clears the rock, landing 10
  # in front of the lip.
  assert plunging["built"]["insideRock"]["vertices"] > 0 and thrown["built"]["insideRock"]["vertices"] == 0
  thrownVertices = numpy.array(thrownShape["vertices"])
  assert numpy.allclose(thrownVertices[numpy.isclose(thrownVertices[:, 2], 0), 1], -10)
  # A wider lip widens the sheet from its top, where it turns over the lip, to its foot.
  lipVertices = numpy.array(widerLipShape["vertices"])
  for row in (lipVertices[numpy.isclose(lipVertices[:, 2], 41) & numpy.isclose(lipVertices[:, 1], 0)], lipVertices[numpy.isclose(lipVertices[:, 2], 0)]):
    assert numpy.isclose(row[:, 0].min(), -30) and numpy.isclose(row[:, 0].max(), 30)
  assert widerLip["definition"]["lip"] == [[-30.0, 0.0, 41.0], [30.0, 0.0, 41.0]]


shoreRadii = """
import math, mathutils
bodies = [sceneObject for sceneObject in bpy.context.scene.objects if 'zonewrightWater' in sceneObject]
for body in bodies:
  body.hide_viewport = True
depsgraph = bpy.context.evaluated_depsgraph_get()

def height(x, y):
  return bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((x, y, 1000.0)), mathutils.Vector((0.0, 0.0, -1.0)))[1].z

radii = []
for index in range(72):
  angle = 2 * math.pi * index / 72
  low, high = 40.0, 99.0
  for _ in range(40):
    middle = (low + high) / 2
    low, high = (middle, high) if height(middle * math.cos(angle), middle * math.sin(angle)) < level else (low, middle)
  radii.append(low)
for body in bodies:
  body.hide_viewport = False
result = radii
"""

readFaces = """
mesh = bpy.data.objects[objectName].data
result = {
  "vertices": [list(vertex.co) for vertex in mesh.vertices], "faces": [list(polygon.vertices) for polygon in mesh.polygons],
  "materials": [mesh.materials[polygon.material_index].name for polygon in mesh.polygons],
}
"""


async def shoreOf(session, level):
  """Where the ground crosses `level` going out from the basin's middle, at 72 headings evenly round it, with the water hidden."""
  return (await session.expectSuccess("runPython", {"code": f"level = {level!r}" + shoreRadii}))["result"]


async def facesOf(session, name):
  return (await session.expectSuccess("runPython", {"code": f"objectName = {name!r}" + readFaces}))["result"]


async def surfaceMaterials(session, tmp_path):
  for name, color in (("stone", (120, 120, 120, 255)), ("sand", (200, 180, 120, 255)), ("mud", (60, 45, 30, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})


def faceCorners(painted, level):
  """For each face: whether every corner lies at or under `level`, whether any does, and the face's middle in plan."""
  vertices = numpy.array(painted["vertices"])
  under = vertices[:, 2] <= level + 1e-3
  return (
    numpy.array([under[face].all() for face in painted["faces"]]), numpy.array([under[face].any() for face in painted["faces"]]),
    numpy.array([vertices[face, :2].mean(axis=0) for face in painted["faces"]]),
  )


def outerRadii(points, sectors):
  """The farthest of [x, y] points from the basin's middle in each of `sectors` equal sectors round it."""
  farthest = numpy.zeros(sectors)
  angles = numpy.arctan2(points[:, 1], points[:, 0]) % (2 * math.pi)
  numpy.maximum.at(farthest, (angles / (2 * math.pi) * sectors).astype(int) % sectors, numpy.linalg.norm(points, axis=1))
  return farthest


def testCarvingKeepsTheWaterlineWhereItWasAndTheBedMeetsIt(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    await surfaceMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    before = await meshOf(session, "ground")
    shoreBefore = await shoreOf(session, -5)
    carved = await session.expectSuccess("carveWaterBed", {"name": "pool", "objectName": "ground", "depth": 6, "shoreWidth": 20})
    after = await meshOf(session, "ground")
    shoreAfter = await shoreOf(session, -5)
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand", "selector": {"underWater": "pool"}})
    banks = await session.expectError("assignMaterial", {"objectName": "ground", "materialName": "mud", "selector": {"nearWater": {"water": "pool", "distance": 0}}})
    painted = await facesOf(session, "ground")
    return before, shoreBefore, carved, after, shoreAfter, banks, painted

  before, shoreBefore, carved, after, shoreAfter, banks, painted = stageBlenderServer.session(steps)
  old, new = numpy.array(before["vertices"]), numpy.array(after["vertices"])
  count = len(old)
  # The carve first cuts the ground along the waterline, then lowers only what lies under it, so the shore stays where it was all round.
  assert max(abs(first - second) for first, second in zip(shoreBefore, shoreAfter)) < 0.01
  assert carved["waterlineCut"]["splitEdges"] > 72 and len(new) == count + carved["waterlineCut"]["splitEdges"]
  assert numpy.allclose(new[count:, 2], -5, atol=1e-3)
  atOrAbove = old[:, 2] >= -5
  assert numpy.array_equal(new[:count][atOrAbove], old[atOrAbove]) and (new[:count, 2] <= old[:, 2] + 1e-6).all()
  # 60 out the cone stands at -8, 15 in from the waterline at 75: lowered toward -11 by most of the depth; the deepest ground stays.
  radius = numpy.linalg.norm(old[:, :2], axis=1)
  ring = (radius > 58) & (radius < 62)
  assert (new[:count][ring, 2] < -9.5).all() and (old[ring, 2] > -8.5).all()
  assert numpy.allclose(new[:count][radius < 20, 2], old[radius < 20, 2]) and carved["deepestBed"] == -20
  # Along the cut the bed is exactly the faces under the water, and it reaches the waterline in every direction.
  allUnder, _, _ = faceCorners(painted, -5)
  sand = numpy.array([material == "sand" for material in painted["materials"]])
  assert numpy.array_equal(sand, allUnder)
  vertices = numpy.array(painted["vertices"])
  bedCorners = vertices[numpy.unique(numpy.concatenate([face for face, isSand in zip(painted["faces"], sand) if isSand]))][:, :2]
  shoreBySector = numpy.array(shoreAfter).reshape(36, 2).max(axis=1)
  assert numpy.abs(outerRadii(bedCorners, 36) - shoreBySector).max() < 0.5
  assert "distance must be positive" in banks


def shoreAt(shore, points):
  """The basin's shore radius (shoreOf, 72 headings) at the heading of each [x, y] point."""
  headings = 2 * math.pi * numpy.arange(72) / 72
  return numpy.interp(numpy.arctan2(points[:, 1], points[:, 0]) % (2 * math.pi), headings, shore, period=2 * math.pi)


def testBedAndBankSelectorsFollowTheWaterlineAndEndOnItsCuts(stageBlenderServer, tmp_path):
  async def paint(session, distance):
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand", "selector": {"underWater": "pool"}})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "mud", "selector": {"nearWater": {"water": "pool", "distance": distance}}})
    return await facesOf(session, "ground")

  async def steps(session):
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    await surfaceMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    shore = await shoreOf(session, -5)
    painted = {distance: await paint(session, distance) for distance in (6, 1)}
    cut = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [0, 6], "waterline": "pool"})
    again = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [0, 6], "waterline": "pool"})
    painted["cut"] = await paint(session, 6)
    refusals = [
      await session.expectError("cutContours", {"objectName": "ground", "levels": [-2], "waterline": "pool"}),
      await session.expectError("cutContours", {"objectName": "ground", "levels": [3], "waterline": "pool", "distanceFrom": {"material": "sand"}}),
    ]
    return shore, painted, cut, again, refusals

  shore, painted, cut, again, refusals = stageBlenderServer.session(steps)
  shoreBySector = numpy.array(shore).reshape(36, 2).max(axis=1)
  for key, distance in ((6, 6), (1, 1), ("cut", 6)):
    allUnder, _, _ = faceCorners(painted[key], -5)
    vertices = numpy.array(painted[key]["vertices"])
    materials = numpy.array(painted[key]["materials"])
    # The bed is the faces wholly under the water: none reaches above the waterline, none under it is left out.
    assert numpy.array_equal(materials == "sand", allUnder)
    # The banks are every face the waterline crosses or meets, so no ground shows between bed and bank, and every face out of the water
    # all of whose corners lie within the distance of the waterline (here, of the basin's shore round its middle), and no other.
    outward = numpy.linalg.norm(vertices[:, :2], axis=1) - shoreAt(shore, vertices)
    farthest = numpy.array([outward[face].max() for face in painted[key]["faces"]])
    mud = materials == "mud"
    meets = numpy.array([(vertices[face, 2] <= -5 + 1e-3).any() for face in painted[key]["faces"]]) & ~allUnder
    assert meets.any() and (mud | ~meets).all()
    assert (farthest[mud & ~meets] <= distance + 0.4).all() and (mud | allUnder | (farthest > distance - 0.4)).all()
  # Uncut, the band's outer edge is whole faces; cut along the waterline and 6 out from it first, the bed reaches the waterline and the
  # band ends 6 out from it in every direction, and cutting again finds nothing more to cut.
  vertices = numpy.array(painted["cut"]["vertices"])
  allUnder, _, _ = faceCorners(painted["cut"], -5)
  bedCorners = vertices[numpy.unique(numpy.concatenate([face for face, under in zip(painted["cut"]["faces"], allUnder) if under]))][:, :2]
  assert numpy.abs(outerRadii(bedCorners, 36) - shoreBySector).max() < 0.5
  mud = numpy.array(painted["cut"]["materials"]) == "mud"
  bankCorners = vertices[numpy.unique(numpy.concatenate([face for face, isMud in zip(painted["cut"]["faces"], mud) if isMud]))][:, :2]
  assert numpy.abs(outerRadii(bankCorners, 36) - (shoreBySector + 6)).max() < 0.5
  assert cut["splitEdges"] > 144 and again["splitEdges"] == 0
  assert "0 (the waterline) or more" in refusals[0] and "give one of them" in refusals[1]


readShapedFacesAndShores = """
import mathutils
ground = bpy.data.objects['ground']
depsgraph = bpy.context.evaluated_depsgraph_get()
evaluated = ground.evaluated_get(depsgraph)
mesh = evaluated.to_mesh()
vertices = [list(vertex.co) for vertex in mesh.vertices]
faces = [list(polygon.vertices) for polygon in mesh.polygons]
materials = [ground.data.materials[polygon.material_index].name for polygon in mesh.polygons]
evaluated.to_mesh_clear()
bpy.data.objects['river'].hide_viewport = True
depsgraph = bpy.context.evaluated_depsgraph_get()

def height(x, y):
  return bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((x, y, 1000.0)), mathutils.Vector((0.0, 0.0, -1.0)))[1].z

shores = []
for x in range(-130, 131, 2):
  level = -6 - x / 75
  for side in (1, -1):
    low, high = 15.0, 34.0
    for _ in range(40):
      middle = (low + high) / 2
      low, high = (middle, high) if height(x, side * middle) < level else (low, middle)
    shores.append([x, side * low])
bpy.data.objects['river'].hide_viewport = False
result = {"vertices": vertices, "faces": faces, "materials": materials, "shores": shores}
"""


def distancesToPolyline(points, polyline):
  starts, spans = polyline[:-1], polyline[1:] - polyline[:-1]
  offsets = points[:, None, :] - starts[None]
  along = numpy.clip((offsets * spans[None]).sum(axis=2) / (spans * spans).sum(axis=1)[None], 0, 1)
  return numpy.linalg.norm(offsets - along[:, :, None] * spans[None], axis=2).min(axis=1)


def testABankBandEndsOnItsCutAlongASlopingRiverAndTheCutKeepsShapingPasses(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 160], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "channel"})
    # A straight channel along x, flat at -10 out to 18.7 each side and rising to the plain at 34: a river falling from -4 to -8 along
    # it meets the banks along lines that close in on the middle as it falls.
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-200, 0, -10], [200, 0, -10]], "radius": 34, "strength": 1, "profile": [[0, 0], [0.55, 0], [1, 10]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    await surfaceMaterials(session, tmp_path)
    await session.expectSuccess("runWater", {"name": "river", "path": [[-150, 0, -4], [150, 0, -8]], "reach": 30, "material": "water"})
    cut = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [0, 3], "waterline": "river"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "stone"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand", "selector": {"underWater": "river"}})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "mud", "selector": {"nearWater": {"water": "river", "distance": 3}}})
    painted = (await session.expectSuccess("runPython", {"code": readShapedFacesAndShores}))["result"]
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "channel", "muted": True})
    flat = await meshOf(session, "ground")
    return cut, painted, flat

  cut, painted, flat = stageBlenderServer.session(steps)
  vertices = numpy.array(painted["vertices"])
  shores = numpy.array(painted["shores"])
  banks = [shores[shores[:, 1] > 0], shores[shores[:, 1] < 0]]
  fromShore = numpy.where(vertices[:, 1] > 0, distancesToPolyline(vertices[:, :2], banks[0]), distancesToPolyline(vertices[:, :2], banks[1]))
  under = vertices[:, 2] <= -6 - vertices[:, 0] / 75 + 1e-3
  faces = [numpy.array(face) for face in painted["faces"]]
  # Away from the river's ends, the bed is exactly the faces under the water, and the bank exactly the faces out of it lying within
  # 3 of the waterline: both end on the lines cut along the waterline and 3 out from it, on either bank as the river falls.
  middle = numpy.array([numpy.abs(vertices[face, 0]).max() <= 120 for face in faces])
  materials = numpy.array(painted["materials"])
  allUnder = numpy.array([under[face].all() for face in faces])
  farthest = numpy.array([fromShore[face].max() for face in faces])
  assert numpy.array_equal((materials == "sand")[middle], allUnder[middle])
  mud = (materials == "mud") & middle
  assert mud.sum() > 60 and (farthest[mud] <= 3 + 0.15).all()
  assert ((materials == "mud") | allUnder | (farthest > 3 - 0.15))[middle].all()
  for bank in (1, -1):
    for start in range(-120, 120, 16):
      corners = numpy.unique(numpy.concatenate([face for face, isMud in zip(faces, mud) if isMud and start <= vertices[face, 0].mean() < start + 16 and vertices[face, 1].mean() * bank > 0]))
      assert abs(fromShore[corners].max() - 3) < 0.15
  # The cut put each new vertex alike in the channel's shaping pass: with the pass muted the ground is the flat grid it was.
  assert cut["splitEdges"] > 100 and numpy.abs(numpy.array(flat["vertices"])[:, 2]).max() < 1e-4


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
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "testwater.blend")})
    undecided = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    refusedForGame = await session.expectSuccess("checkExport", {"path": str(archivePath), "purpose": "game"})
    boxes = await session.expectSuccess("buildSwimVolumes", {"body": "pool"})
    await session.expectSuccess("saveFile", {})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    return missing, stray, undecided, refusedForGame, boxes, exported

  missing, stray, undecided, refusedForGame, boxes, exported = stageBlenderServer.session(steps)
  assert "missing ['environment']" in missing and "does not take ['fresnelBias']" in stray
  archive = eqArchive.EQArchive(archivePath)
  zone = eqgFiles.parseZone(archive.read("testwater.zon"), "testwater.zon")
  # Export derives no swim volumes: without boxes the pool is listed as undecided and the .zon gets no regions; with them, exactly those.
  assert undecided["regions"] == [] and undecided["swim"] == {"undecided": ["pool"], "changed": []}
  assert [finding for finding in undecided["findings"] if finding["finding"].startswith("swim")] == [{"finding": "swim undecided", "body": "pool", "at": [0.0, 0.0, -5.0]}]
  # A game export refuses a pool whose swimming is undecided.
  assert {"failure": "swim undecided", "body": "pool", "at": [0.0, 0.0, -5.0]} in refusedForGame["failures"]
  assert exported["regions"] == [region["name"] for region in zone["regions"]] == boxes["built"] and exported["swim"] == {"undecided": [], "changed": []}
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
    await session.expectSuccess("setZoneProperties", environment)
    view = {"eye": [-150, -150, 80], "target": [0, 0, -10]}
    current, _ = await session.expectImage("renderView", {"view": view})
    older = (await session.expectSuccess("runPython", {"code": simulateOlderGroup}))["result"]
    rebuilt, _ = await session.expectImage("renderView", {"view": view})
    return current, older, rebuilt

  current, older, rebuilt = stageBlenderServer.session(steps)
  # A group saved before the added light input existed is rebuilt with it, and what it draws does not change.
  assert "Added" not in older and rebuilt == current


def boundaryVertices(shape):
  """The vertices on a mesh's open edge: on an edge only one face uses."""
  uses = {}
  for face in shape["faces"]:
    for first, second in zip(face, face[1:] + face[:1]):
      edge = (min(first, second), max(first, second))
      uses[edge] = uses.get(edge, 0) + 1
  return sorted({vertex for edge, count in uses.items() if count == 1 for vertex in edge})


def alongAndAcross(points, path):
  """For points: how far along a straight path from its start each lies, and how far across it."""
  start, end = numpy.array(path[0][:2], dtype=numpy.float64), numpy.array(path[-1][:2], dtype=numpy.float64)
  direction = (end - start) / numpy.linalg.norm(end - start)
  offsets = points[:, :2] - start
  return offsets @ direction, offsets @ numpy.array([-direction[1], direction[0]])


def testRiverSidesRunStraightAtReachAndItsEndsSquare(stageBlenderServer, tmp_path):
  path = [[-150, -75, -4], [150, 75, -6]]
  shorter = [[-50, -25, -4 - 2 / 3], [150, 75, -6]]

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 304], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    # A channel across the grid's diagonal, flat at -10 for 18 each side of its middle and rising to the ground 30 out: the river's
    # level stands over 25 each side of the path, wider than its reach.
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-200, -100, -10], [200, 100, -10]], "radius": 30, "strength": 1, "profile": [[0, 0], [0.6, 0], [1, 10]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("runWater", {"name": "river", "path": path, "reach": 10, "material": "water"})
    full = await facesOf(session, "river")
    await session.expectSuccess("editWater", {"name": "river", "path": shorter})
    short = await facesOf(session, "river")
    return full, short

  full, short = stageBlenderServer.session(steps)
  length = math.dist(path[0][:2], path[1][:2])
  for shape, start in ((full, 0.0), (short, math.dist(path[0][:2], shorter[0][:2]))):
    vertices = numpy.array(shape["vertices"])
    along, across = alongAndAcross(vertices, path)
    edge = numpy.array(boundaryVertices(shape))
    # The river spreads 10 from its path and no farther, its sides cut straight along that reach rather than stepping cell by cell,
    # its ends square across the path where it starts and ends; shortened, it starts at its new first point, the stretch above gone.
    assert numpy.abs(across).max() <= 10 + 1e-3
    sides = edge[(along[edge] > start + 1e-2) & (along[edge] < length - 1e-2)]
    assert len(sides) > 40 and numpy.allclose(numpy.abs(across[sides]), 10, atol=1e-3)
    assert along.min() >= start - 1e-3 and along.max() <= length + 1e-3
    for end in (start, length):
      atEnd = numpy.abs(along - end) < 1e-3
      assert numpy.isclose(across[atEnd].min(), -10, atol=1e-3) and numpy.isclose(across[atEnd].max(), 10, atol=1e-3)


def roundedVertices(shape):
  return sorted(map(tuple, numpy.round(numpy.array(shape["vertices"]), 3).tolist()))


def testPoolWithinSeedAndStrokesShapeWhereItSpreads(stageBlenderServer, tmp_path):
  within = [[-200, -200], [220, -200], [-200, 220]]

  async def steps(session):
    await freshScene(session)
    await basin(session)
    # A second basin 184 from the first, apart from it: -12 at its middle, rising to the ground at 50 out.
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[130, 130, -12]], "radius": 50, "strength": 1, "profile": [[0, 0], [1, 12]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    whole = await meshOf(session, "pool")
    await session.expectSuccess("editWater", {"name": "pool", "within": within})
    cut = await meshOf(session, "pool")
    added = await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "add", "area": {"circle": {"center": [20, 20], "radius": 20}}})
    cove = await meshOf(session, "pool")
    unchanged = [
      await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "add", "area": {"circle": {"center": [130, 130], "radius": 30}}}),
      await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "add", "area": {"circle": {"center": [-180, -180], "radius": 12}}}),
      await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "add", "area": {"circle": {"center": [-20, -20], "radius": 12}}}),
      await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "remove", "area": {"circle": {"center": [-180, 180], "radius": 12}}}),
    ]
    stillCove = await meshOf(session, "pool")
    kept = (await session.expectSuccess("getWater", {}))["bodies"][0]["definition"]["strokes"]
    await session.expectSuccess("editWater", {"name": "pool", "within": [], "strokes": []})
    restored = await meshOf(session, "pool")
    moved = await session.expectSuccess("editWater", {"name": "pool", "seed": [130, 130]})
    return whole, cut, added, cove, unchanged, stillCove, kept, restored, moved

  whole, cut, added, cove, unchanged, stillCove, kept, restored, moved = stageBlenderServer.session(steps)
  # Inside a within outline that crosses open water, the pool ends along the outline itself (x + y = 20), not a cell past it.
  cutVertices = numpy.array(cut["vertices"])
  sums = cutVertices[:, 0] + cutVertices[:, 1]
  assert (sums <= 20 + 1e-3).all() and (numpy.abs(sums - 20) < 1e-3).sum() >= 10
  # An added stroke floods the low ground it opens to the water, past the outline.
  coveVertices = numpy.array(cove["vertices"])
  inCove = numpy.linalg.norm(coveVertices[:, :2] - [20, 20], axis=1) <= 20 + 1e-3
  assert (inCove | (coveVertices[:, 0] + coveVertices[:, 1] <= 20 + 1e-3)).all() and (coveVertices[inCove, 0] + coveVertices[inCove, 1] > 40).any()
  # A stroke that changes nothing yet is kept for the edit it may be laid ahead of, the result saying so and why: low ground a ridge
  # cuts off, ground above the level, ground already under the water, a removed area holding no water. The water is as it was.
  assert added["strokeChanged"] is True and "whyUnchanged" not in added
  assert all(result["strokeChanged"] is False for result in unchanged)
  cutOff, dry, covered, empty = (result["whyUnchanged"] for result in unchanged)
  assert "nothing joins them to the water" in cutOff
  assert "at or above the water's level" in dry and "already covers" in covered and "none of the water lies inside the area yet" in empty
  assert roundedVertices(stillCove) == roundedVertices(cove)
  assert [stroke["area"]["circle"]["center"] for stroke in kept] == [[20, 20], [130, 130], [-180, -180], [-20, -20], [-180, 180]]
  # Without its outline and strokes the pool is whole again; seeded in the other basin it fills that one instead.
  assert roundedVertices(restored) == roundedVertices(whole)
  assert all(80 < value < 180 for value in moved["visibleExtent"]["minimum"] + moved["visibleExtent"]["maximum"])


def testAStrokeLaidAheadOfARaisedLevelHoldsTheWaterWhenItArrives(stageBlenderServer, tmp_path):
  stroke = {"polygon": [[100, -20], [108, -20], [108, 20], [100, 20]]}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [400, 200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    # A pit 12 deep at the middle, and a side channel running east from its rim with its floor at -3: dry while the pool stands at -5.
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[0, 0, -12]], "radius": 50, "strength": 1, "profile": [[0, 0], [1, 12]], "conformRim": False})
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[40, 0, -3], [180, 0, -3]], "radius": 12, "strength": 1, "profile": [[0, 0], [0.5, 0], [1, 3]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    ahead = await session.expectSuccess("shapeWaterExtent", {"name": "pool", "mode": "remove", "area": stroke})
    await session.expectSuccess("editWater", {"name": "pool", "level": -2})
    held = await meshOf(session, "pool")
    await session.expectSuccess("editWater", {"name": "pool", "strokes": []})
    unbounded = await meshOf(session, "pool")
    return ahead, held, unbounded

  ahead, held, unbounded = stageBlenderServer.session(steps)
  # Laid across the dry channel, the stroke changes nothing yet and is kept, saying so; raised to -2, the pool runs into the channel
  # and stops along the stroke's near edge, where without it the water runs on to the channel's end.
  assert ahead["strokeChanged"] is False and "none of the water lies inside the area yet" in ahead["whyUnchanged"]
  assert ahead["definition"]["strokes"] == [{"mode": "remove", "area": stroke}]
  heldX = numpy.array(held["vertices"])[:, 0]
  assert 99 < heldX.max() <= 100 + 1e-3
  assert numpy.array(unbounded["vertices"])[:, 0].max() > 170


def testGetWaterReportsEachBodyAndTheWaterPlayersSee(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await basin(session)
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-200, 150, -8], [200, 150, -8]], "radius": 20, "strength": 1, "profile": [[0, 0], [1, 8]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    made = [
      await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"}),
      await session.expectSuccess("runWater", {"name": "river", "path": [[-150, 150, -3], [150, 150, -4]], "reach": 12, "material": "water"}),
      await session.expectSuccess("pourWaterfall", {"name": "fall", "lip": [[-10, 60, 20], [10, 60, 20]], "bottom": -5, "throw": 0, "material": "falls"}),
    ]
    listed = await session.expectSuccess("getWater", {})
    pool = await meshOf(session, "pool")
    return made, listed, pool

  made, listed, pool = stageBlenderServer.session(steps)
  bodies = {body["name"]: body for body in listed["bodies"]}
  assert sorted(bodies) == ["fall", "pool", "river"]
  for body in made:
    assert bodies[body["name"]] == {key: value for key, value in body.items() if key != "built"}
  assert [bodies[name]["kind"] for name in ("pool", "river", "fall")] == ["pool", "river", "fall"]
  assert bodies["pool"]["levels"] == [-5, -5] and bodies["river"]["levels"] == [-4, -3] and bodies["fall"]["levels"] == [-5, 20]
  # The extent given is the water players see: the pool out to its waterline 75 from its middle, though its surface runs on a cell or
  # two under the banks; the river between its ends and within reach of its path.
  extent = bodies["pool"]["visibleExtent"]
  assert all(abs(abs(value) - 75) < 0.6 for value in extent["minimum"] + extent["maximum"])
  assert numpy.abs(numpy.array(pool["vertices"])[:, :2]).max() > 80
  river = bodies["river"]["visibleExtent"]
  assert river["minimum"][0] == -150 and river["maximum"][0] == 150 and 138 <= river["minimum"][1] < river["maximum"][1] <= 162


planWater = """
import bridgeSketch
result = bridgeSketch.planOverlays(None, ["water"], [])["water"]
"""
sectionWater = """
import bridgeSketch
result = bridgeSketch.sectionCuts([-130, 0], [130, 0], -30, 12, ["water"])["water"]
"""


def planPixel(image, point, center, width):
  """The mean color of the 3 x 3 pixels of a plan drawing at a world [x, y] point."""
  pixels = numpy.asarray(Image.open(io.BytesIO(image)).convert("RGB"), dtype=numpy.float64)
  height = width * pixels.shape[0] / pixels.shape[1]
  column = round((point[0] - center[0] + width / 2) / width * pixels.shape[1])
  row = round((center[1] + height / 2 - point[1]) / height * pixels.shape[0])
  return pixels[row - 1:row + 2, column - 1:column + 2].reshape(-1, 3).mean(axis=0)


def testPlansAndSectionsDrawTheWaterPlayersSeeAndTheTuckStopsWhereItHides(stageBlenderServer, tmp_path):
  plan = {"center": [0, 0], "width": 260, "spotHeights": False}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [240, 240], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    # A pit with a flat floor at -10 out to 45 and a wall up to the plain at 0 by 50: at level -2 its waterline lies 49 out, and the
    # plain stands exactly shoreTuck (2) over the level.
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[0, 0, -10]], "radius": 50, "strength": 1, "profile": [[0, 0], [0.9, 0], [1, 10]], "conformRim": False})
    await liquidMaterials(session, tmp_path)
    pit = await session.expectSuccess("floodWater", {"name": "pit", "seed": [0, 0], "level": -2, "material": "water"})
    pitShape = await meshOf(session, "pit")
    pitShore = await shoreOf(session, -2)
    await freshScene(session)
    await basin(session)
    await liquidMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "pool", "seed": [0, 0], "level": -5, "material": "water"})
    await session.expectSuccess("setZoneProperties", environment)
    overlaid = (await session.expectSuccess("runPython", {"code": planWater}))["result"]
    cut = (await session.expectSuccess("runPython", {"code": sectionWater}))["result"]
    drawn, _ = await session.expectImage("renderSketch", plan | {"layers": ["water"]})
    bare, _ = await session.expectImage("renderSketch", plan | {"layers": []})
    poolShape = await meshOf(session, "pool")
    return pit, pitShape, pitShore, overlaid, cut, drawn, bare, poolShape

  pit, pitShape, pitShore, overlaid, cut, drawn, bare, poolShape = stageBlenderServer.session(steps)
  # The plain standing shoreTuck over the level already hides the surface's edge: it stops within a cell of the waterline instead of
  # running on under the plain; the extent given reaches the waterline as the ground's 8-unit faces lay it, and no farther.
  shoreX = [radius * math.cos(2 * math.pi * index / 72) for index, radius in enumerate(pitShore)]
  assert numpy.linalg.norm(numpy.array(pitShape["vertices"])[:, :2], axis=1).max() <= max(pitShore) + 8 * math.sqrt(2) + 1
  assert abs(pit["visibleExtent"]["maximum"][0] - max(shoreX)) < 0.3 and abs(pit["visibleExtent"]["minimum"][0] - min(shoreX)) < 0.3
  # The plan holds the pool out to its waterline (75), not the surface tucked under the banks past it (more than 80 out).
  points = numpy.array([corner for x0, y0, x1, y1 in overlaid[0]["runs"] for corner in ((x0, y0), (x1, y1))] + [point for loop in overlaid[0]["loops"] for point in loop])
  assert 74.4 < numpy.linalg.norm(points, axis=1).max() < 75.6 and numpy.abs(numpy.array(poolShape["vertices"])[:, :2]).max() > 80
  # Drawn, the plan tints the water inside the waterline and leaves the bare relief between it and the tucked surface's edge.
  heading = math.radians(35)
  inside, tucked = ([radius * math.cos(heading), radius * math.sin(heading)] for radius in (70, 79))
  assert planPixel(drawn, inside, plan["center"], plan["width"])[2] - planPixel(bare, inside, plan["center"], plan["width"])[2] > 20
  assert numpy.abs(planPixel(drawn, tucked, plan["center"], plan["width"]) - planPixel(bare, tucked, plan["center"], plan["width"])).max() < 2
  # The section's water runs from shore to shore, 75 each side of the middle (130 along the section), not on into the banks.
  segments = numpy.array(cut[0]["segments"])
  assert abs(segments[:, [0, 2]].min() - 55) < 0.6 and abs(segments[:, [0, 2]].max() - 205) < 0.6


def testWaterToolsKeepUpWithALargeLake(stageBlenderServer, tmp_path):
  async def timed(session, tool, arguments):
    start = time.perf_counter()
    await session.expectSuccess(tool, arguments)
    return time.perf_counter() - start

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [1200, 1200], "spacing": 8, "location": [0, 0, 0], "collection": "terrain"})
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[0, 0, -20]], "radius": 420, "strength": 1, "profile": [[0, 0], [0.8, 10], [1, 20]], "conformRim": False})
    await session.expectSuccess("roughen", {"objectName": "ground", "featureSize": 80, "amplitude": 3, "seed": 3})
    await liquidMaterials(session, tmp_path)
    await surfaceMaterials(session, tmp_path)
    await session.expectSuccess("floodWater", {"name": "lake", "seed": [0, 0], "level": -5, "material": "water"})
    return {
      "getWater": await timed(session, "getWater", {}),
      "nearWater": await timed(session, "assignMaterial", {"objectName": "ground", "materialName": "mud", "selector": {"nearWater": {"water": "lake", "distance": 6}}}),
      "carve": await timed(session, "carveWaterBed", {"name": "lake", "objectName": "ground", "depth": 4, "shoreWidth": 16}),
    }

  seconds = stageBlenderServer.session(steps)
  # An 840-wide lake on a 150 x 150 grid: describing it, painting its banks, and carving its bed each take a moment (they took 1.7, 1.7,
  # and 2.5 seconds when its visible water was sampled at a quarter of its spacing and its waterline found point by point).
  assert seconds["getWater"] < 1.0 and seconds["nearWater"] < 1.0 and seconds["carve"] < 2.0, seconds
