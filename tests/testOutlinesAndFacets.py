import math

from testModelsAndDressing import freshScene, readShapedMesh


def heightAt(vertices, x, y):
  return next(round(z, 3) for vx, vy, z in vertices if abs(vx - x) < 1e-3 and abs(vy - y) < 1e-3)


def testSculptOutlineFillsAnAngularMesaAndCarvesASlot(stageBlenderServer):
  # A mesa 84 across: a cliff from 0 to 30 over the 20 units outside its outline, which sits 2 units off the 8-unit grid lines.
  mesa = {"objectName": "ground", "mode": "fill", "outline": [[-42, -42], [42, -42], [42, 42], [-42, 42]], "base": 0, "profile": [[-20, 0], [0, 30], [12, 30]]}
  slot = {"objectName": "trench", "mode": "carve", "outline": [[-60, 196], [60, 196], [60, 236], [-60, 236]], "base": 0, "profile": [[0, 0], [6, -30], [20, -30]], "conformBreaks": False}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [160, 160], "spacing": 8, "location": [0, 0, 0]})
    await session.expectSuccess("createTerrainGrid", {"name": "trench", "size": [160, 80], "spacing": 4, "location": [0, 216, 0]})
    filled = await session.expectSuccess("sculptOutline", mesa)
    carved = await session.expectSuccess("sculptOutline", slot)
    shaped = (await session.expectSuccess("runPython", {"code": "objectName = 'ground'" + readShapedMesh}))["result"]
    trench = (await session.expectSuccess("runPython", {"code": "objectName = 'trench'" + readShapedMesh}))["result"]
    twoPoints = await session.expectError("sculptOutline", mesa | {"outline": [[0, 0], [10, 0]]})
    backwards = await session.expectError("sculptOutline", mesa | {"profile": [[0, 30], [-20, 0]]})
    return filled, carved, shaped, trench, twoPoints, backwards

  filled, carved, shaped, trench, twoPoints, backwards = stageBlenderServer.session(steps)
  vertices = shaped["vertices"]
  assert filled["foldedFaces"] == 0 and carved["foldedFaces"] == 0
  # Inside the outline the cap holds 30; 14 outside a side it is 30 x 6/20; 30 outside it the ground is untouched.
  assert heightAt(vertices, 0, 0) == 30 and heightAt(vertices, 56, 0) == 9 and heightAt(vertices, 72, 0) == 0
  # Outside a corner the cliff rounds: (48, 48) lies 6 x sqrt(2) from the corner.
  assert abs(heightAt(vertices, 48, 48) - 30 * (1 - 6 * math.sqrt(2) / 20)) < 1e-3
  # The grid line 2 inside each side snaps onto the outline, so the cap's edge runs on it.
  edge = [vertex for vertex in vertices if abs(max(abs(vertex[0]), abs(vertex[1])) - 42) < 1e-3]
  assert len(edge) >= 4 * 10 and all(abs(z - 30) < 1e-3 for _, _, z in edge)
  # The slot's floor is 30 down from 6 inside its outline (local y 12 is 8 inside, 16 is 4 inside and 20 down); along the outline the
  # ground stays at 0, and beyond it untouched.
  trenchVertices = trench["vertices"]
  assert heightAt(trenchVertices, 0, 0) == -30 and heightAt(trenchVertices, 0, 12) == -30 and heightAt(trenchVertices, 0, 16) == -20
  assert heightAt(trenchVertices, 0, 20) == 0 and heightAt(trenchVertices, -64, 0) == 0
  assert "at least three [x, y] points" in twoPoints and "distances rising" in backwards


def testFacetPressesRockIntoPlanes(stageBlenderServer):
  measureFacets = """
import numpy
mesh = bpy.data.objects['rock'].evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
normals = numpy.round(numpy.array([list(polygon.normal) for polygon in mesh.polygons]), 3)
_, inverse, counts = numpy.unique(normals, axis=0, return_inverse=True, return_counts=True)
bpy.data.objects['rock'].evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
result = float((counts[inverse.ravel()] >= 4).mean())
"""

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "sphere", "name": "rock", "size": [160, 70, 60], "location": [0, 0, 0], "segments": 64})
    rounded = (await session.expectSuccess("runPython", {"code": measureFacets}))["result"]
    faceted = await session.expectSuccess("facet", {"objectName": "rock", "cellSize": 30, "seed": 3})
    planar = (await session.expectSuccess("runPython", {"code": measureFacets}))["result"]
    nothing = await session.expectError("facet", {"objectName": "rock", "cellSize": 0})
    return rounded, faceted, planar, nothing

  rounded, faceted, planar, nothing = stageBlenderServer.session(steps)
  # A rounded rock has almost no faces sharing a normal; pressed into facets, most faces lie in a plane with three or more others.
  assert rounded < 0.1 and planar > 0.45
  # Where neighboring patches' planes meet, a face can turn over; the result counts them.
  assert faceted["facets"] > 10 and faceted["foldedFaces"] < 0.01 * faceted["affectedVertices"]
  assert "cellSize must be positive" in nothing
