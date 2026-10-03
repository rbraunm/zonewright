import math


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def testScaleFigureWalksAheadUntilAWall(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("setZoneProperties", {
      "ambientColor": [0.3, 0.3, 0.3], "specialAmbientColor": [0, 0, 0], "bounceColor": [0, 0, 0], "sunColor": [0.6, 0.6, 0.6],
      "sunAzimuthDegrees": 0, "sunElevationDegrees": 45, "fogColor": [0.5, 0.5, 0.5], "fogStart": 50, "fogEnd": 300, "fogDensity": 0.33,
      "newEngineZone": False,
    })
    _, open = await session.expectImage("renderView", {"view": {"standAt": [0, 0, 0], "headingDegrees": 0, "pitchDegrees": 0}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [40, 2, 20], "location": [0, 9, 0]})
    _, walled = await session.expectImage("renderView", {"view": {"standAt": [0, 0, 0], "headingDegrees": 0, "pitchDegrees": 0}})
    return open, walled

  open, walled = stageBlenderServer.session(steps)
  assert open["figure"] == [1.5, 15.0, 0.0]
  assert walled["figure"] == [1.5, 6.0, 0.0]


def testDeleteFacesInsideAnObject(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [25, 25, 20], "location": [0, 0, -10]})
    deleted = await session.expectSuccess("deleteFaces", {"objectName": "ground", "selector": {"insideObject": "boulder"}})
    everything = await session.expectError("deleteFaces", {"objectName": "boulder", "selector": {"all": True}})
    return deleted, everything

  deleted, everything = stageBlenderServer.session(steps)
  assert deleted["deletedFaces"] == 4
  assert deleted["faces"] == 96
  assert "matches every face of 'boulder'; delete the object instead" in everything


def testJoinObjectsMergesMeshesAndMaterials(stageBlenderServer, tmp_path):
  from conftest import writePNG
  barkTexture = writePNG(tmp_path / "bark.png", 4, 4, (90, 60, 30, 255))
  leafTexture = writePNG(tmp_path / "leaf.png", 4, 4, (30, 90, 30, 255))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createMaterial", {"name": "bark", "diffuseTexture": str(barkTexture)})
    await session.expectSuccess("createMaterial", {"name": "leaf", "diffuseTexture": str(leafTexture)})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "trunk", "size": [2, 2, 8], "location": [0, 0, 0], "segments": 6})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "canopy", "size": [10, 10, 20], "location": [0, 0, 6], "segments": 8})
    await session.expectSuccess("assignMaterial", {"objectName": "trunk", "materialName": "bark"})
    await session.expectSuccess("assignMaterial", {"objectName": "canopy", "materialName": "leaf"})
    joined = await session.expectSuccess("joinObjects", {"names": ["canopy", "trunk"], "into": "canopy"})
    trunkGone = await session.expectError("getObjectDetail", {"name": "trunk"})
    detail = await session.expectSuccess("getObjectDetail", {"name": "canopy"})
    return joined, trunkGone, detail

  joined, trunkGone, detail = stageBlenderServer.session(steps)
  assert joined["vertices"] == 9 + 12
  assert joined["materials"] == ["leaf", "bark"]
  assert detail["dimensions"] == [10.0, 10.0, 26.0]
  assert detail["materials"] == [{"material": "leaf", "faces": 9}, {"material": "bark", "faces": 8}]
  assert "No object named 'trunk'" in trunkGone


def testScatterAvoidsObjectsWithClearance(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "tower", "size": [20, 20, 40], "location": [0, 0, 0], "segments": 16})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "pine", "size": [4, 4, 10], "location": [0, 0, -500], "segments": 6})
    scattered = await session.expectSuccess("scatterInRegion", {
      "sourceObject": "pine", "region": {"circle": {"center": [0, 0], "radius": 50}}, "density": 40, "minimumSpacing": 5,
      "collection": "grove", "surfaceObjects": ["ground"], "avoidObjects": ["tower"], "avoidClearance": 8, "seed": 2,
    })
    summary = await session.expectSuccess("getSceneSummary", {"objectLimit": 500})
    return scattered, summary

  scattered, summary = stageBlenderServer.session(steps)
  grove = [sceneObject["location"] for sceneObject in summary["objects"] if "grove" in sceneObject["collections"]]
  assert scattered["rejected"]["nearAvoidedObject"] > 0
  assert scattered["placed"] == len(grove)
  assert scattered["placed"] + scattered["rejected"]["nearAvoidedObject"] == scattered["spacedCandidates"]
  assert all(math.hypot(x, y) > 10 + 8 - 0.5 for x, y, _ in grove)


def testCarveWidensAlongThePathWithRadii(stageBlenderServer):
  readVertices = "result = [list(vertex.co) for vertex in bpy.data.objects['ground'].data.vertices]"
  carve = {"objectName": "ground", "mode": "carve", "path": [[-80, 0, -20], [80, 0, -20]], "strength": 1, "profile": [[0, 0], [1, 30]], "conformRim": False}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 5, "location": [0, 0, 0]})
    both = await session.expectError("sculptAlongPath", carve | {"radius": 10, "radii": [10, 50]})
    miscounted = await session.expectError("sculptAlongPath", carve | {"radii": [10, 30, 50]})
    await session.expectSuccess("sculptAlongPath", carve | {"radii": [10, 50]})
    return both, miscounted, (await session.expectSuccess("runPython", {"code": readVertices}))["result"]

  both, miscounted, vertices = stageBlenderServer.session(steps)
  assert "Give either radius" in both and "one positive radius per path point (2)" in miscounted

  def carvedRow(x):
    return sorted(round(y, 3) for vx, y, z in vertices if abs(vx - x) < 1e-3 and z < -0.01)

  # The profile reaches the ground (height 0, 20 above the floor) two thirds of the way out, so the cut is 2/3 of the radius wide on each
  # side: radius 10 at the start, 30 halfway, 50 at the end.
  assert carvedRow(-80) == [-5, 0, 5]
  assert carvedRow(0) == [-15, -10, -5, 0, 5, 10, 15]
  assert carvedRow(80) == list(range(-30, 35, 5))
  assert round(min(z for x, y, z in vertices if abs(y) < 1e-3 and -80 <= x <= 80), 3) == -20


def testFillRaisesGroundToAProfileAndLeavesHigherGround(stageBlenderServer):
  readVertices = "result = [list(vertex.co) for vertex in bpy.data.objects['ground'].data.vertices]"
  fill = {"objectName": "ground", "mode": "fill", "path": [[0, 0, 0]], "radius": 40, "strength": 1, "profile": [[0, 30], [0.5, 30], [1, 0]]}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"sphere": {"center": [0, 0, 0], "radius": 1}}, "offset": [0, 0, 50]})
    conformed = await session.expectError("sculptAlongPath", fill | {"conformRim": True})
    atPoint = await session.expectError("sculptAtPoint", {"objectName": "ground", "mode": "fill", "center": [0, 0, 0], "radius": 40, "strength": 1})
    await session.expectSuccess("sculptAlongPath", fill)
    return conformed, atPoint, (await session.expectSuccess("runPython", {"code": readVertices}))["result"]

  conformed, atPoint, vertices = stageBlenderServer.session(steps)
  assert "only carve takes it" in conformed and "use sculptAlongPath, whose path can be a single point" in atPoint

  def heightAt(x, y):
    return next(round(z, 3) for vx, vy, z in vertices if abs(vx - x) < 1e-3 and abs(vy - y) < 1e-3)

  # The raised center stays above the fill; the cap is 30 out to radius 20, then falls evenly to the ground at radius 40.
  assert heightAt(0, 0) == 50 and heightAt(10, 0) == 30 and heightAt(20, 0) == 30 and heightAt(30, 0) == 15 and heightAt(40, 0) == 0
  assert heightAt(50, 0) == 0


def testCarveConformsVerticesOntoTheProfilesBreaks(stageBlenderServer):
  readVertices = "result = [list(vertex.co) for vertex in bpy.data.objects['ground'].data.vertices]"
  # A ledge: the floor at -20 out to 45% of the radius (18 units), then a wall up to the ground.
  carve = {"objectName": "ground", "mode": "carve", "path": [[-100, 0, -20], [100, 0, -20]], "radius": 40, "strength": 1, "profile": [[0, 0], [0.45, 2], [1, 25]], "conformRim": False}

  async def steps(session):
    results = {}
    for conform in (False, True):
      await freshScene(session)
      await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [96, 96], "spacing": 8, "location": [0, 0, 0]})
      await session.expectSuccess("sculptAlongPath", carve | {"conformBreaks": conform})
      results[conform] = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    return results

  results = stageBlenderServer.session(steps)
  # The grid lines at y = +-16 lie within half an edge (4 units) of the break at 18; with conformBreaks they move onto it at the
  # break's height, so the ledge's edge runs straight along the cut instead of falling between grid lines. The vertices on the
  # grid's open edge (x = +-48) stay, as the border never slides.
  assert not any(abs(abs(y) - 18) < 1e-3 for _, y, _ in results[False])
  snapped = [(y, z) for x, y, z in results[True] if abs(abs(y) - 18) < 1e-3]
  assert len(snapped) == 2 * 11 and all(abs(z - (-18)) < 1e-3 for _, z in snapped)
  assert not any(abs(abs(y) - 16) < 1e-3 and abs(x) < 48 for x, y, _ in results[True])


def testCarveConformSlidesRimVerticesOntoTheContour(stageBlenderServer):
  path = [[-100, -100, -20], [100, 100, -20]]
  profile = [[0, 0], [0.5, 10], [1, 30]]
  readVertices = "result = [list(vertex.co) for vertex in bpy.data.objects['ground'].data.vertices]"

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    before = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": path, "radius": 40, "strength": 1, "profile": profile})
    after = (await session.expectSuccess("runPython", {"code": readVertices}))["result"]
    nonRising = await session.expectError("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": path, "radius": 40, "strength": 1, "profile": [[0, 0], [0.5, 10], [1, 10]]})
    # A slanted cut running off the terrain: sliding toward it would push border vertices below it out past the border.
    await session.expectSuccess("createTerrainGrid", {"name": "crossed", "size": [200, 200], "spacing": 10, "location": [0, 300, 0]})
    await session.expectSuccess("sculptAlongPath", {"objectName": "crossed", "mode": "carve", "path": [[-160, 240, -20], [160, 360, -20]], "radius": 40, "strength": 1, "profile": profile})
    crossed = (await session.expectSuccess("runPython", {"code": readVertices.replace("'ground'", "'crossed'")}))["result"]
    return before, after, nonRising, crossed

  before, after, nonRising, crossed = stageBlenderServer.session(steps)
  rimLateral = 40 * (0.5 + 0.5 * (20 - 10) / (30 - 10))
  slid = [(old, new) for old, new in zip(before, after) if new[2] == 0 and (abs(new[0] - old[0]) > 1e-6 or abs(new[1] - old[1]) > 1e-6)]
  assert slid
  for _, new in slid:
    assert abs(abs(new[0] - new[1]) / math.sqrt(2) - rimLateral) < 1e-3
  assert "conformRim needs profile heights that rise" in nonRising
  assert max(abs(x) for x, _, _ in crossed) == 100 and max(abs(y) for _, y, _ in crossed) == 100


readShapedMesh = """
depsgraph = bpy.context.evaluated_depsgraph_get()
evaluated = bpy.data.objects[objectName].evaluated_get(depsgraph)
mesh = evaluated.to_mesh()
result = {"vertices": [list(vertex.co) for vertex in mesh.vertices], "faces": [list(polygon.vertices) for polygon in mesh.polygons]}
evaluated.to_mesh_clear()
"""


def planarArea(points):
  return abs(sum(x0 * y1 - x1 * y0 for (x0, y0, _), (x1, y1, _) in zip(points, points[1:] + points[:1]))) / 2


def testFillTriangulatesAlongItsBreaksAtAnyAngle(stageBlenderServer):
  # A mesa: a cap, a cliff, a ledge, and a slope, with breaks 18, 24, and 30 units from the path.
  profile = [[0, 30], [0.45, 28], [0.6, 12], [0.75, 10], [1, 0]]
  breakLaterals = [18, 24, 30]
  angles = [30, 45]

  async def steps(session):
    await freshScene(session)
    results = {}
    for index, angle in enumerate(angles):
      name = f"ground{angle}"
      await session.expectSuccess("createTerrainGrid", {"name": name, "size": [240, 240], "spacing": 8, "location": [300 * index, 0, 0]})
      await session.expectSuccess("addShapingPass", {"objectName": name, "name": "mesa"})
      direction = (math.cos(math.radians(angle)), math.sin(math.radians(angle)))
      path = [[300 * index - 150 * direction[0], -150 * direction[1], 0], [300 * index + 150 * direction[0], 150 * direction[1], 0]]
      stroke = await session.expectSuccess("sculptAlongPath", {
        "objectName": name, "mode": "fill", "path": path, "radius": 40, "strength": 1, "profile": profile, "conformBreaks": True,
      })
      shaped = (await session.expectSuccess("runPython", {"code": f"objectName = '{name}'" + readShapedMesh}))["result"]
      await session.expectSuccess("setShapingPass", {"objectName": name, "name": "mesa", "muted": True})
      muted = (await session.expectSuccess("runPython", {"code": f"objectName = '{name}'" + readShapedMesh}))["result"]
      results[angle] = (stroke, shaped, muted, direction)
    return results

  results = stageBlenderServer.session(steps)
  # Vertex coordinates are the grid's own, centered on the path.
  for angle, (stroke, shaped, muted, direction) in results.items():
    vertices = shaped["vertices"]

    def lateral(vertex):
      return abs(vertex[0] * direction[1] - vertex[1] * direction[0])

    def along(vertex):
      return vertex[0] * direction[0] + vertex[1] * direction[1]

    interior = [face for face in shaped["faces"] if all(abs(along(vertices[index])) < 100 for index in face)]
    straddling = [
      face for face in interior for breakLateral in breakLaterals
      if min(lateral(vertices[index]) for index in face) < breakLateral - 0.01 and max(lateral(vertices[index]) for index in face) > breakLateral + 0.01
    ]
    slivers = [face for face in shaped["faces"] if planarArea([vertices[index] for index in face]) < 0.1 * 8 * 8 / 2]
    assert stroke["splitCells"] > 0 and stroke["foldedFaces"] == 0, (angle, stroke)
    assert straddling == [], (angle, len(straddling), len(interior))
    assert slivers == [], (angle, len(slivers))
    assert max(abs(z) for _, _, z in muted["vertices"]) == 0 and len(muted["vertices"]) == 31 * 31


def testFollowContoursTurnsDiagonalsAlongAStep(stageBlenderServer):
  # A step across the grid's diagonal: 10 on one side, 0 on the other, 5 on the line.
  stepHeights = """
for vertex in bpy.data.objects[objectName].data.vertices:
  across = vertex.co.x - direction * vertex.co.y
  vertex.co.z = 10 if across > 1e-3 else (5 if across > -1e-3 else 0)
"""

  async def steps(session):
    await freshScene(session)
    results = {}
    for index, direction in enumerate([1, -1]):
      name = f"step{index}"
      await session.expectSuccess("createTerrainGrid", {"name": name, "size": [80, 80], "spacing": 8, "location": [100 * index, 0, 0]})
      await session.expectSuccess("runPython", {"code": f"objectName = '{name}'\ndirection = {direction}" + stepHeights})
      before = (await session.expectSuccess("runPython", {"code": f"objectName = '{name}'" + readShapedMesh}))["result"]
      followed = await session.expectSuccess("followContours", {"objectName": name})
      after = (await session.expectSuccess("runPython", {"code": f"objectName = '{name}'" + readShapedMesh}))["result"]
      results[direction] = (before, followed, after)
    return results

  results = stageBlenderServer.session(steps)

  def spanningStep(mesh):
    return [face for face in mesh["faces"] if {0, 10} <= {round(mesh["vertices"][index][2]) for index in face}]

  for before, followed, after in results.values():
    assert len(spanningStep(before)) == 10 and spanningStep(after) == []
    assert (followed["splitCells"], followed["faces"]) == (100, 200)
    assert after["vertices"] == before["vertices"]
  # The fixed split runs one way across every cell, so it already follows one grid's step; the other turns the 10 cells the step
  # crosses and the 9 on each side that touch its line.
  assert sorted(followed["turnedDiagonals"] for _, followed, _ in results.values()) == [0, 28]
