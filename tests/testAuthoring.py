from PIL import Image

readHeights = """
import numpy
target = bpy.data.objects['ground']
evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
mesh = evaluated.to_mesh()
result = [[round(v.co.x, 3), round(v.co.y, 3), round(v.co.z, 4)] for v in mesh.vertices]
evaluated.to_mesh_clear()
"""


def materialFaces(detail):
  return {entry["material"]: entry["faces"] for entry in detail["materials"] if entry["faces"]}


async def newGround(session, size, spacing):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [size, size], "spacing": spacing, "location": [0, 0, 0], "collection": "terrain"})


async def makeMaterials(session, folder, names):
  for index, name in enumerate(names):
    Image.new("RGBA", (8, 8), (40 * index + 60, 120, 80, 255)).save(folder / f"{name}.png")
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(folder / f"{name}.png")})


def testRegionsConfineToolsAndKeepTheirIntent(stageBlenderServer):
  async def steps(session):
    await newGround(session, 64, 8)
    created = await session.expectSuccess("createRegion", {"name": "plaza", "outline": [[-20, -20], [20, -20], [20, 20], [-20, 20]], "bottom": -10, "top": 10, "intent": "packed earth plaza"})
    moved = await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"region": "plaza"}, "offset": [0, 0, 5]})
    await session.expectSuccess("editRegion", {"name": "plaza", "top": 3})
    above = await session.expectError("moveVertices", {"objectName": "ground", "selector": {"region": "plaza"}, "offset": [0, 0, 1]})
    notRegion = await session.expectError("moveVertices", {"objectName": "ground", "selector": {"region": "ground"}, "offset": [0, 0, 1]})
    regions = await session.expectSuccess("getRegions")
    summary = await session.expectSuccess("getSceneSummary")
    return created, moved, above, notRegion, regions, summary

  created, moved, above, notRegion, regions, summary = stageBlenderServer.session(steps)
  assert created == {"name": "plaza", "intent": "packed earth plaza", "outline": [[-20, -20], [20, -20], [20, 20], [-20, 20]], "bottom": -10, "top": 10, "area": 1600}
  # The 5 by 5 grid vertices from -16 to 16 lie inside; raised to 5, they are above the edited top of 3.
  assert moved["movedVertices"] == 25 and "matches no vertices" in above and "is not a region" in notRegion
  assert regions["regions"] == [created | {"top": 3}]
  assert next(entry for entry in summary["objects"] if entry["name"] == "plaza")["hiddenInRender"] is True


def testSurfaceLayersComposeAndAreEditedAtTheirEdges(stageBlenderServer, tmp_path):
  async def steps(session):
    await newGround(session, 64, 8)
    await makeMaterials(session, tmp_path, ["sand", "rock", "path"])
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "path"})
    states = {}
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "rock", "selector": {"box": {"minimum": [-40, -40, -1], "maximum": [0, 40, 1]}}})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "path", "material": "path", "selector": {"nearPath": {"path": [[-40, 4, 0], [40, 4, 0]], "radius": 3}}})
    states["painted"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    states["refused"] = await session.expectError("assignMaterial", {"objectName": "ground", "materialName": "sand"})
    await session.expectSuccess("setSurfaceLayer", {"objectName": "ground", "name": "path", "muted": True})
    states["muted"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "ground", "operation": "grow"})
    states["grown"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "ground", "operation": "shrink"})
    states["shrunk"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "rock", "selector": {"sphere": {"center": [20, -20, 0], "radius": 2}}})
    states["island"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("editSurface", {"objectName": "ground", "layer": "ground", "operation": "smooth"})
    states["smoothed"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("eraseSurface", {"objectName": "ground", "layer": "ground", "selector": {"all": True}})
    states["erased"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("removeSurfaceLayer", {"objectName": "ground", "name": "path"})
    await session.expectSuccess("removeSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "rock"})
    states["unlayered"] = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return states

  states = stageBlenderServer.session(steps)
  # 64 faces: the left half rock, the path's row of 8 faces across both halves on top.
  assert materialFaces(states["painted"]) == {"sand": 28, "rock": 28, "path": 8}
  assert [layer["name"] for layer in states["painted"]["surfaceLayers"]] == ["ground", "path"]
  assert "paint into a layer with paintSurface instead" in states["refused"]
  assert materialFaces(states["muted"]) == {"sand": 32, "rock": 32}
  # Growing adds the column beside the rock; shrinking takes the rock's edge column back.
  assert materialFaces(states["grown"]) == {"sand": 24, "rock": 40} and materialFaces(states["shrunk"]) == {"sand": 32, "rock": 32}
  # A one-face island of rock is absorbed by smoothing, while the large rock area keeps its straight edge.
  assert materialFaces(states["island"]) == {"sand": 31, "rock": 33} and materialFaces(states["smoothed"]) == {"sand": 32, "rock": 32}
  assert materialFaces(states["erased"]) == {"sand": 64}
  assert materialFaces(states["unlayered"]) == {"rock": 64} and states["unlayered"]["surfaceLayers"] == []


def testPaintedEdgesWanderWithEdgeNoise(stageBlenderServer, tmp_path):
  readPainted = """
import numpy
mesh = bpy.data.objects['ground'].data
result = [[round(p.center.x, 2), round(p.center.y, 2), p.material_index] for p in mesh.polygons]
"""

  async def steps(session):
    await newGround(session, 256, 8)
    await makeMaterials(session, tmp_path, ["sand", "rock"])
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "rock", "selector": {"box": {"minimum": [-200, -200, -1], "maximum": [0, 200, 1]}}, "edgeNoise": {"featureSize": 32, "amplitude": 12, "seed": 5}})
    return (await session.expectSuccess("runPython", {"code": readPainted}))["result"]

  faces = stageBlenderServer.session(steps)
  rock = 1
  # Far from the edge nothing changes; near it the painted edge crosses x = 0 both ways along its length.
  assert all(material == rock for x, _, material in faces if x < -24) and all(material != rock for x, _, material in faces if x > 24)
  assert any(material == rock for x, _, material in faces if x > 0) and any(material != rock for x, _, material in faces if x < 0)


def testAreasAreTakenBackRebuiltAndCleared(stageBlenderServer, tmp_path):
  tiltCode = """
mesh = bpy.data.objects['ground'].data
for vertex in mesh.vertices:
  vertex.co.z = 0.5 * vertex.co.x
mesh.update()
"""

  async def steps(session):
    await newGround(session, 128, 8)
    await session.expectSuccess("createRegion", {"name": "patch", "outline": [[-30, -30], [30, -30], [30, 30], [-30, 30]], "bottom": -500, "top": 500, "intent": "test patch"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "hill"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 56, "strength": 20, "direction": [0, 0, 1]})
    hill = (await session.expectSuccess("runPython", {"code": readHeights}))["result"]
    reset = await session.expectSuccess("resetRegion", {"objectName": "ground", "selector": {"region": "patch"}})
    afterReset = (await session.expectSuccess("runPython", {"code": readHeights}))["result"]
    await session.expectSuccess("collapseShapingPasses", {"objectName": "ground"})
    await session.expectSuccess("runPython", {"code": tiltCode})
    await session.expectSuccess("moveVertices", {"objectName": "ground", "selector": {"region": "patch"}, "offset": [0, 0, 30]})
    rebuilt = await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "patch"}, "mode": "surroundings"})
    afterRebuild = (await session.expectSuccess("runPython", {"code": readHeights}))["result"]
    await session.expectSuccess("rebuildRegion", {"objectName": "ground", "selector": {"region": "patch"}, "mode": "height", "height": 10})
    leveled = (await session.expectSuccess("runPython", {"code": readHeights}))["result"]
    await makeMaterials(session, tmp_path, ["sand", "rock"])
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "sand"})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "rock", "selector": {"all": True}})
    await session.expectSuccess("placeLights", {"lights": [{"name": "inside", "position": [0, 0, 12], "color": [1, 1, 1], "radius": 20}, {"name": "outside", "position": [50, 50, 30], "color": [1, 1, 1], "radius": 20}]})
    cleared = await session.expectSuccess("clearRegion", {"region": "patch", "terrainObject": "ground", "surfacing": True, "objects": True})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return hill, reset, afterReset, rebuilt, afterRebuild, leveled, cleared, detail

  hill, reset, afterReset, rebuilt, afterRebuild, leveled, cleared, detail = stageBlenderServer.session(steps)
  inside = [index for index, (x, y, _) in enumerate(hill) if abs(x) < 28 and abs(y) < 28]
  outside = [index for index, (x, y, _) in enumerate(hill) if abs(x) > 28 or abs(y) > 28]
  # Without a fade the hill is taken back to flat ground inside the patch and kept outside it.
  assert reset["passes"] == ["hill"] and all(afterReset[index][2] == 0 for index in inside)
  assert all(afterReset[index][2] == hill[index][2] for index in outside) and max(hill[index][2] for index in outside) > 5
  # Spanning from the surroundings restores the tilted plane the raised patch was cut from.
  assert rebuilt["affectedVertices"] == len(inside) and max(abs(afterRebuild[index][2] - 0.5 * afterRebuild[index][0]) for index in inside) < 0.05
  assert all(leveled[index][2] == 10 for index in inside)
  # The rebuild split the patch's 8 by 8 cells into triangles along the new ground; the 128 triangles, whose centers all lie inside the
  # patch, lose their paint; the light inside it goes, the one outside stays.
  assert rebuilt["turnedDiagonals"] == 0 and cleared["objects"] == {"deleted": ["inside"]} and cleared["surfacing"]["erasedFaces"] == 128
  assert materialFaces(detail) == {"sand": 128, "rock": 256 - 64}
