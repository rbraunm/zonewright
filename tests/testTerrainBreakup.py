import numpy
from PIL import Image

vertexProbe = """
import numpy
target = bpy.data.objects['{name}']
evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
mesh = evaluated.to_mesh()
coordinates = numpy.empty(len(mesh.vertices) * 3)
mesh.vertices.foreach_get('co', coordinates)
evaluated.to_mesh_clear()
matrix = numpy.array(target.matrix_world)
result = (coordinates.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3]).tolist()
"""


async def worldVertices(session, name):
  return numpy.array((await session.expectSuccess("runPython", {"code": vertexProbe.format(name=name)}))["result"])


async def newGrid(session, name, size=128, spacing=8):
  await session.expectSuccess("createTerrainGrid", {"name": name, "size": [size, size], "spacing": spacing, "location": [0, 0, 0]})


def testMaskSelectorsPickBySlopeHeightRouteAndMaterial(stageBlenderServer, tmp_path):
  texturePath = tmp_path / "path.png"
  Image.new("RGBA", (8, 8), (120, 100, 70, 255)).save(texturePath)

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await newGrid(session, "ground", 64, 8)
    await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"box": {"minimum": [8, -100, -1], "maximum": [100, 100, 1]}}, "offset": [0, 0, 40]})
    await session.expectSuccess("createMaterial", {"name": "grass", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("createMaterial", {"name": "path", "diffuseTexture": str(texturePath)})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "path", "selector": {"box": {"minimum": [-32, -100, -1], "maximum": [-24, 100, 1]}}})
    flat = await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"slope": {"minimumDegrees": 0, "maximumDegrees": 1}}, "offset": [0, 0, 0]})
    high = await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"height": {"minimum": 39, "maximum": 41}}, "offset": [0, 0, 0]})
    route = await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"nearPath": {"path": [[-32, -32, 0], [-32, 32, 0]], "radius": 1}}, "offset": [0, 0, 0]})
    paved = await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"material": "path"}, "offset": [0, 0, 0]})
    return flat, high, route, paved

  flat, high, route, paved = stageBlenderServer.session(steps)
  # A 9 x 9 grid with the four columns from x = 8 raised 40: the step between x = 0 and x = 8 tilts the normals of the two columns
  # beside it, leaving 63 flat vertices; the route along x = -32 passes one column; the path material covers the quads between the
  # columns at x = -32 and x = -24.
  assert high["movedVertices"] == 36 and flat["movedVertices"] == 63 and route["movedVertices"] == 9 and paved["movedVertices"] == 18


def testRoughenFollowsTheSurfaceStaysInItsMaskAndRepeatsBySeed(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await newGrid(session, "first")
    await newGrid(session, "second")
    await newGrid(session, "third")
    before = await worldVertices(session, "first")
    masked = {"cylinder": {"center": [0, 0], "radius": 40, "bottom": -10, "top": 10}}
    first = await session.expectSuccess("roughen", {"objectName": "first", "featureSize": 32, "amplitude": 6, "seed": 7, "selector": masked})
    await session.expectSuccess("roughen", {"objectName": "second", "featureSize": 32, "amplitude": 6, "seed": 7, "selector": masked})
    await session.expectSuccess("roughen", {"objectName": "third", "featureSize": 32, "amplitude": 6, "seed": 8, "selector": masked, "fadeDistance": 24})
    await session.expectSuccess("createPrimitive", {"kind": "grid", "name": "wall", "size": [64, 64, 0], "location": [0, 200, 0], "divisions": [16, 16], "rotationDegrees": [90, 0, 0]})
    wallBefore = await worldVertices(session, "wall")
    await session.expectSuccess("roughen", {"objectName": "wall", "featureSize": 16, "amplitude": 3, "seed": 1})
    return before, first, await worldVertices(session, "first"), await worldVertices(session, "second"), await worldVertices(session, "third"), wallBefore, await worldVertices(session, "wall")

  before, summary, first, second, third, wallBefore, wall = stageBlenderServer.session(steps)
  inside = numpy.hypot(before[:, 0], before[:, 1]) <= 40
  moves = first - before
  assert numpy.array_equal(first, second) and not numpy.allclose(first, third)
  # On flat ground the normal is up: only heights change, only inside the mask, and within the amplitude.
  assert numpy.abs(moves[:, :2]).max() < 1e-9 and numpy.abs(moves[~inside]).max() < 1e-9
  assert 0 < numpy.abs(moves[inside, 2]).max() <= 6 and summary["affectedVertices"] == int((numpy.abs(moves[:, 2]) > 1e-9).sum())
  # With a fade, vertices on the mask's edge stay put and the change grows inward.
  edgeRing = inside & (numpy.hypot(before[:, 0], before[:, 1]) > 32)
  assert numpy.abs((third - before)[edgeRing, 2]).max() < numpy.abs((third - before)[inside & ~edgeRing, 2]).max()
  # On a vertical wall the normal is horizontal: roughening moves the wall in and out, never up, down, or along it (to the precision
  # of a turned object's 32-bit coordinates).
  wallMoves = wall - wallBefore
  assert numpy.abs(wallMoves[:, 2]).max() < 1e-5 and numpy.abs(wallMoves[:, 0]).max() < 1e-5 and numpy.abs(wallMoves[:, 1]).max() > 0.5


def testWarpBendsShapesSidewaysAndCanBeTakenBack(stageBlenderServer):
  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await newGrid(session, "ground")
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 48, "strength": 30, "direction": [0, 0, 1]})
    cone = await worldVertices(session, "ground")
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "warp"})
    await session.expectSuccess("warp", {"objectName": "ground", "featureSize": 40, "amplitude": 10, "seed": 3})
    warped = await worldVertices(session, "ground")
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "warp", "muted": True})
    return cone, warped, await worldVertices(session, "ground")

  cone, warped, muted = stageBlenderServer.session(steps)
  moves = warped - cone
  # Horizontal warp keeps every height and moves vertices sideways, up to the amplitude on each axis.
  assert numpy.abs(moves[:, 2]).max() < 1e-9
  assert 1 < numpy.abs(moves[:, :2]).max() <= 10

  def radiusSpreads(vertices):
    """For each height the raised cone gives several vertices, how far apart their distances from the center lie."""
    raised = vertices[vertices[:, 2] > 1]
    heights = numpy.round(raised[:, 2], 4)
    radii = numpy.hypot(raised[:, 0], raised[:, 1])
    return numpy.array([numpy.ptp(radii[heights == height]) for height in numpy.unique(heights) if (heights == height).sum() > 1])

  # The sculpted cone is round, every height at one distance from its center; warped, the same heights lie at different distances.
  assert radiusSpreads(cone).max() < 1e-4
  assert numpy.median(radiusSpreads(warped)) > 1
  # Muting the warp pass gives the round cone back.
  assert numpy.allclose(muted, cone)
