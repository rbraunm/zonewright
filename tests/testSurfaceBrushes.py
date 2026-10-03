import math

from conftest import writePNG
from testModelsAndDressing import freshScene, readShapedMesh

readSurface = """
import numpy
sceneObject = bpy.data.objects[objectName]
mesh = sceneObject.data
values = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
mesh.attributes['zonewrightSurface:' + layerName].data.foreach_get('value', values)
uvs = numpy.zeros(len(mesh.loops) * 2)
if 'UVMap' in mesh.uv_layers:
  mesh.uv_layers['UVMap'].data.foreach_get('uv', uvs)
loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
mesh.loops.foreach_get('vertex_index', loopVertices)
result = {'values': values.tolist(), 'uvs': uvs.reshape(-1, 2).tolist(), 'loopVertices': loopVertices.tolist(),
  'faces': [list(polygon.vertices) for polygon in mesh.polygons], 'loopStarts': [polygon.loop_start for polygon in mesh.polygons]}
"""


def surface(objectName, layerName):
  return {"code": f"objectName = '{objectName}'\nlayerName = '{layerName}'\n" + readSurface}


def shaped(objectName):
  return {"code": f"objectName = '{objectName}'" + readShapedMesh}


def borderLength(vertices, faces, values):
  edgeFaces = {}
  for index, face in enumerate(faces):
    for first, second in zip(face, face[1:] + face[:1]):
      edgeFaces.setdefault((min(first, second), max(first, second)), []).append(index)
  return sum(math.dist(vertices[a][:2], vertices[b][:2]) for (a, b), owners in edgeFaces.items() if len(owners) == 2 and values[owners[0]] != values[owners[1]])


async def stripedGround(session, name, size, spacing):
  await session.expectSuccess("createTerrainGrid", {"name": name, "size": [size, size], "spacing": spacing, "location": [0, 0, 0], "collection": "terrain"})
  await session.expectSuccess("followContours", {"objectName": name, "selector": {"all": True}})


async def twoMaterials(session, folder):
  await session.expectSuccess("createMaterial", {"name": "sand", "diffuseTexture": str(writePNG(folder / "sand.png", 4, 4, (200, 180, 120, 255)))})
  await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(writePNG(folder / "stone.png", 4, 4, (120, 80, 60, 255)))})


def testConformSurfaceEdgesRunsTheBorderOnSmoothEdgesInEveryPass(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 256, 8)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "bump"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 120, "strength": 20})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "stone", "selector": {"nearPath": {"path": [[-128, -40, 0], [128, 30, 0]], "radius": 30}}})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "planar", "direction": [0, 0, 1], "worldUnitsPerRepeat": 32})
    before = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    beforeSurface = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    conformed = await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": "path", "smoothing": 12})
    after = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    afterSurface = (await session.expectSuccess("runPython", surface("ground", "path")))["result"]
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "bump", "strength": 0})
    flattened = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    nothing = await session.expectError("conformSurfaceEdges", {"objectName": "ground", "layer": "path", "smoothing": 0})
    return before, beforeSurface, conformed, after, afterSurface, flattened, nothing

  before, beforeSurface, conformed, after, afterSurface, flattened, nothing = stageBlenderServer.session(steps)
  moved = [index for index, (old, new) in enumerate(zip(before["vertices"], after["vertices"])) if math.dist(old, new) > 1e-6]
  assert conformed["movedVertices"] == len(moved) > 20 and conformed["keptInPlace"] == 0
  # The saw-toothed border of a painted stroke on a triangulated grid is evened: much shorter for the same stroke.
  assert borderLength(after["vertices"], after["faces"], afterSurface["values"]) < 0.8 * borderLength(before["vertices"], before["faces"], beforeSurface["values"])
  # Vertices slid in the base as in the pass: with the pass at 0 the ground is flat again everywhere.
  assert max(abs(z) for _, _, z in flattened["vertices"]) < 1e-5
  # UVs were carried with the move: on the flat ground beyond the bump, where every face around a moved vertex lies in one plane,
  # they still equal the planar projection, x and y over 32.
  movedSet = set(moved)
  checked = 0
  for face, start in zip(afterSurface["faces"], afterSurface["loopStarts"]):
    if movedSet.isdisjoint(face) or any(abs(after["vertices"][vertex][2]) > 1e-9 for vertex in face):
      continue
    checked += 1
    for corner, vertex in enumerate(face):
      x, y, _ = after["vertices"][vertex]
      assert abs(afterSurface["uvs"][start + corner][0] - x / 32) < 1e-4 and abs(afterSurface["uvs"][start + corner][1] - y / 32) < 1e-4
  assert checked >= 10
  assert "smoothing must be positive" in nothing


def testConformSurfaceEdgesLeavesBordersOnCreasesAlone(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 256, 8)
    await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-60, -60], [60, -60], [60, 60], [-60, 60]], "base": 0, "profile": [[-1, 0], [0, 100], [10, 100]], "conformBreaks": False})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "rock"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "rock", "material": "stone", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
    return await session.expectSuccess("conformSurfaceEdges", {"objectName": "ground", "layer": "rock", "smoothing": 12})

  conformed = stageBlenderServer.session(steps)
  # Rock painted by slope ends at the cliff's top and foot, both creases: nothing moves and no face changes.
  assert conformed["movedVertices"] == 0 and conformed["changedFaces"] == 0


def testEditSurfaceCleanTakesOverSmallPieces(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 128, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "patch"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patch", "material": "stone", "selector": {"box": {"minimum": [-64, -64, -1], "maximum": [0, 64, 1]}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patch", "material": "sand", "selector": {"sphere": {"center": [-30, 0, 0], "radius": 7}}})
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "patch", "selector": {"sphere": {"center": [-36, 36, 0], "radius": 7}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patch", "material": "stone", "selector": {"sphere": {"center": [36, -36, 0], "radius": 7}}})
    dirty = (await session.expectSuccess("runPython", surface("ground", "patch")))["result"]
    cleaned = await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "patch", "operation": "clean", "minimumArea": 500})
    clean = (await session.expectSuccess("runPython", surface("ground", "patch")))["result"]
    missing = await session.expectError("editSurface", {"objectName": "ground", "layer": "patch", "operation": "clean"})
    stray = await session.expectError("editSurface", {"objectName": "ground", "layer": "patch", "operation": "grow", "minimumArea": 10})
    return dirty, cleaned, clean, missing, stray

  dirty, cleaned, clean, missing, stray = stageBlenderServer.session(steps)
  # Material slots follow the order materials were first painted: stone, then sand.
  stone, sand = 0, 1
  assert set(dirty["values"]) == {stone, sand, -1}
  # The stone half (256 of the 512 triangles) stays whole: the sand speck and the hole in it are stone again, and the stone speck on the
  # uncovered half is gone.
  assert set(clean["values"]) == {stone, -1} and clean["values"].count(stone) == 256
  assert cleaned["changed"] == sum(1 for old, new in zip(dirty["values"], clean["values"]) if old != new) > 0
  assert "clean needs a minimumArea" in missing and "only clean takes one" in stray


def testNoiseSelectorPaintsSeededPatches(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 512, 8)
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "patches"})
    first = await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patches", "material": "stone", "selector": {"noise": {"featureSize": 30, "share": 0.3, "seed": 4}}})
    again = await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patches", "material": "stone", "selector": {"noise": {"featureSize": 30, "share": 0.3, "seed": 4}}})
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "patches", "selector": {"all": True}})
    other = await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "patches", "material": "stone", "selector": {"noise": {"featureSize": 30, "share": 0.3, "seed": 5}}})
    otherSurface = (await session.expectSuccess("runPython", surface("ground", "patches")))["result"]
    wide = await session.expectError("paintSurface", {"objectName": "ground", "layer": "patches", "material": "stone", "selector": {"noise": {"featureSize": 60, "share": 1.5, "seed": 4}}})
    unbounded = await session.expectError("paintSurface", {"objectName": "ground", "layer": "patches", "material": "stone", "selector": {"height": {"maximum": 4}}})
    return first, again, other, otherSurface, wide, unbounded

  first, again, other, otherSurface, wide, unbounded = stageBlenderServer.session(steps)
  faces = len(otherSurface["values"])
  # About 30% of the surface; the same seed paints the same patches, another seed others.
  assert first["painted"] == again["painted"] and 0.15 * faces < first["painted"] < 0.45 * faces and other["painted"] != first["painted"]
  assert "share is a fraction between 0 and 1" in wide
  assert "The height selector is {\"height\": {minimum, maximum}}" in unbounded


def testCutContoursSplitsAlongLevelsInEveryPassAndTransitionsEndOnTheCut(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    await twoMaterials(session, tmp_path)
    await stripedGround(session, "ground", 128, 16)
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "ramp"})
    await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"box": {"minimum": [0, -100, -1], "maximum": [100, 100, 1]}}, "offset": [0, 0, 40]})
    cut = await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [10, 30], "selector": {"all": True}})
    ramp = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "ramp", "strength": 0})
    flat = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    await session.expectSuccess("setShapingPass", {"objectName": "ground", "name": "ramp", "strength": 1})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"all": True}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": {"box": {"minimum": [-100, -100, -1], "maximum": [-32, 100, 1]}}})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "blend"})
    uncut = await session.expectSuccess("paintTransition", {"objectName": "ground", "layer": "blend", "material": "stone", "selector": {"material": "sand"}, "toward": {"material": "stone"}, "width": 20, "worldUnitsPerRepeat": 32})
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "blend", "selector": {"all": True}})
    await session.expectSuccess("cutContours", {"objectName": "ground", "levels": [20], "distanceFrom": {"material": "stone"}, "selector": {"material": "sand"}})
    strip = await session.expectSuccess("paintTransition", {"objectName": "ground", "layer": "blend", "material": "stone", "selector": {"material": "sand"}, "toward": {"material": "stone"}, "width": 20, "worldUnitsPerRepeat": 32})
    stripSurface = (await session.expectSuccess("runPython", surface("ground", "blend")))["result"]
    stripShape = (await session.expectSuccess("runPython", shaped("ground")))["result"]
    return cut, ramp, flat, uncut, strip, stripSurface, stripShape

  cut, ramp, flat, uncut, strip, stripSurface, stripShape = stageBlenderServer.session(steps)
  assert cut["splitEdges"] > 0 and cut["triangles"] == cut["faces"]
  # The ramp climbs 40 over the 16 units between x = -16 and 0, crossing levels 10 and 30 on every row: every face now lies wholly on
  # one side of each level, and each row has a vertex on it.
  for face in ramp["faces"]:
    heights = [ramp["vertices"][index][2] for index in face]
    for level in (10, 30):
      assert min(heights) >= level - 1e-4 or max(heights) <= level + 1e-4
  assert sum(1 for _, _, z in ramp["vertices"] if abs(z - 10) < 1e-4) >= 9
  assert max(abs(z) for _, _, z in flat["vertices"]) < 1e-5
  # Stone ends at x = -32. Before the cut the ramp's faces straddle 20 from that border; after it the strip ends on the cut, which
  # crosses the ramp about x = -13 (18.8 across and 6.9 up).
  assert uncut["straddlingFaces"] > 0 and strip["straddlingFaces"] == 0 and strip["painted"] > uncut["painted"]
  stripFaces = [index for index, value in enumerate(stripSurface["values"]) if value != -1]
  vValues = [stripSurface["uvs"][stripSurface["loopStarts"][face] + corner][1] for face in stripFaces for corner in range(3)]
  xs = [stripShape["vertices"][vertex][0] for face in stripFaces for vertex in stripSurface["faces"][face]]
  assert min(vValues) == 0 and abs(max(vValues) - 1) < 1e-4 and min(xs) >= -32 - 1e-4 and -14 < max(xs) < -12


def testMeasureSnapsOntoRenderedGroundThroughRegions(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 5], "collection": "terrain"})
    await session.expectSuccess("createRegion", {"name": "plot", "outline": [[-20, -20], [20, -20], [20, 20], [-20, 20]], "bottom": 0, "top": 50, "intent": "a test plot"})
    return await session.expectSuccess("measure", {"points": [[0, 0, 100]], "snapToSurface": True})

  measured = stageBlenderServer.session(steps)
  assert measured["points"] == [[0.0, 0.0, 5.0]]
