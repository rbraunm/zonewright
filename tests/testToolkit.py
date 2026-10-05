import itertools
import math

from conftest import writePNG
from testClientRendering import renderedPixel

upFacing = {"facing": {"direction": [0, 0, 1], "withinDegrees": 10}}


async def heightsAt(session, points):
  measured = await session.expectSuccess("measure", {"points": [[x, y, 100] for x, y in points], "snapToSurface": True})
  return [point[2] for point in measured["points"]]


async def freshScene(session):
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})


def testPrimitivesAreBuiltToExactSize(stageBlenderServer):
  shapes = [
    ("plane", {}, (4, 1, 2), [10.0, 12.0, 0.0]),
    ("grid", {"divisions": [4, 4]}, (25, 16, 32), [10.0, 12.0, 0.0]),
    ("cube", {}, (8, 6, 12), [10.0, 12.0, 20.0]),
    ("cylinder", {"segments": 12}, (24, 14, 44), [10.0, 12.0, 20.0]),
    ("cone", {"segments": 12}, (13, 13, 22), [10.0, 12.0, 20.0]),
    ("sphere", {"segments": 12}, (62, 72, 120), [10.0, 12.0, 20.0]),
  ]

  async def steps(session):
    await freshScene(session)
    created = {}
    for kind, extra, _, _ in shapes:
      created[kind] = await session.expectSuccess("createPrimitive", {"kind": kind, "name": kind, "size": [10, 12, 20], "location": [0, 0, 5]} | extra)
    cubeDetail = await session.expectSuccess("getObjectDetail", {"name": "cube"})
    duplicateName = await session.expectError("createPrimitive", {"kind": "cube", "name": "cube", "size": [1, 1, 1], "location": [0, 0, 0]})
    missingSegments = await session.expectError("createPrimitive", {"kind": "cylinder", "name": "pipe", "size": [1, 1, 1], "location": [0, 0, 0]})
    return created, cubeDetail, duplicateName, missingSegments

  created, cubeDetail, duplicateName, missingSegments = stageBlenderServer.session(steps)
  for kind, _, (vertices, faces, triangles), dimensions in shapes:
    assert (created[kind]["vertices"], created[kind]["faces"], created[kind]["triangles"]) == (vertices, faces, triangles), kind
    assert created[kind]["dimensions"] == dimensions, kind
  assert cubeDetail["worldMinimum"] == [-5.0, -6.0, 5.0]
  assert cubeDetail["worldMaximum"] == [5.0, 6.0, 25.0]
  assert "An object named 'cube' already exists" in duplicateName
  assert "A cylinder needs segments of at least 3" in missingSegments


def testTerrainGridAndFineVertexMoves(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    grid = await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    uneven = await session.expectError("createTerrainGrid", {"name": "rough", "size": [100, 95], "spacing": 10, "location": [0, 0, 0]})
    moved = await session.expectSuccess("moveVertices", {
      "objectName": "ground", "selector": {"sphere": {"center": [0, 0, 0], "radius": 25}}, "offset": [0, 0, 10],
      "falloff": {"center": [0, 0, 0], "radius": 25, "curve": "linear"},
    })
    heights = await heightsAt(session, [(0, 0), (10, 0), (20, 0), (30, 0)])
    emptySelection = await session.expectError("moveVertices", {"objectName": "ground", "selector": {"sphere": {"center": [500, 0, 0], "radius": 5}}, "offset": [0, 0, 1]})
    return grid, uneven, moved, heights, emptySelection

  grid, uneven, moved, heights, emptySelection = stageBlenderServer.session(steps)
  assert (grid["vertices"], grid["faces"], grid["dimensions"]) == (121, 100, [100.0, 100.0, 0.0])
  assert "size 100 x 95 is not a whole number of 10-unit cells" in uneven
  assert heights == [10.0, 6.0, 2.0, 0.0]
  assert moved["largestMove"] == 10.0
  assert "matches no vertices of 'ground'" in emptySelection


def testSculptRaiseCarveSmoothAndCrease(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "hill", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "raise", "center": [0, 0, 0], "radius": 30, "strength": 10})
    raised = await heightsAt(session, [(0, 0), (10, 0), (40, 0)])
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "smooth", "center": [0, 0, 0], "radius": 30, "strength": 1, "iterations": 4})
    smoothed = await heightsAt(session, [(0, 0)])
    await session.expectSuccess("sculptAlongPath", {
      "objectName": "hill", "mode": "carve", "path": [[-100, -60, -5], [100, -60, -5]], "radius": 20, "strength": 1,
      "profile": [[0, 0], [1, 10]],
    })
    carved = await heightsAt(session, [(-30, -60), (-30, -50), (-30, -40)])
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "crease", "center": [60, 60, 0], "radius": 20, "strength": 4})
    creased = await heightsAt(session, [(60, 60)])
    carveWithoutProfile = await session.expectError("sculptAlongPath", {"objectName": "hill", "mode": "carve", "path": [[0, 0, 0], [10, 0, 0]], "radius": 5, "strength": 1})
    fractionTooBig = await session.expectError("sculptAtPoint", {"objectName": "hill", "mode": "flatten", "center": [0, 0, 0], "radius": 5, "strength": 2})
    return raised, smoothed, carved, creased, carveWithoutProfile, fractionTooBig

  raised, smoothed, carved, creased, carveWithoutProfile, fractionTooBig = stageBlenderServer.session(steps)
  closeness = 1 - 10 / 30
  assert raised[0] == 10.0
  assert abs(raised[1] - 10 * closeness * closeness * (3 - 2 * closeness)) < 0.001
  assert raised[2] == 0.0
  assert smoothed[0] < 10.0
  assert carved == [-5.0, 0.0, 0.0]
  assert creased[0] == -4.0
  assert "carve and fill need a profile" in carveWithoutProfile
  assert "flatten strength is a fraction in (0, 1]" in fractionTooBig


def testLowerFlattenAndEveryFalloffCurveShapeAsTheyPromise(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "field", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "field", "mode": "lower", "center": [-60, -60, 0], "radius": 30, "strength": 10, "falloff": "linear"})
    lowered = await heightsAt(session, [(-60, -60), (-50, -60), (-40, -60), (-30, -60)])
    curves = {}
    for curve, (x, y) in (("constant", (50, 50)), ("smooth", (50, -50)), ("sharp", (-50, 50))):
      await session.expectSuccess("moveVertices", {
        "objectName": "field", "selector": {"sphere": {"center": [x, y, 0], "radius": 30}}, "offset": [0, 0, 20],
        "falloff": {"center": [x, y, 0], "radius": 30, "curve": curve},
      })
      curves[curve] = await heightsAt(session, [(x + distance, y) for distance in (0, 10, 20, 40)])
    await session.expectSuccess("createTerrainGrid", {"name": "hill", "size": [200, 200], "spacing": 10, "location": [300, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "raise", "center": [300, 0, 0], "radius": 40, "strength": 30, "falloff": "sharp"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "hill", "mode": "flatten", "center": [300, 0, 30], "radius": 20, "strength": 1, "falloff": "constant"})
    flattened = await heightsAt(session, [(300, 0), (310, 0), (300, 10), (290, 0), (300, -10), (320, 0)])
    return lowered, curves, flattened

  lowered, curves, flattened = stageBlenderServer.session(steps)
  # Lower with a linear falloff: a cone 10 deep, a third shallower every 10 units out.
  assert [round(height, 3) for height in lowered] == [-10.0, -6.667, -3.333, 0.0]
  # 20 up within 30, by curve, at 0, 10, 20, and 40 units out: constant holds full height to the edge; smooth eases out (smoothstep);
  # sharp falls off as the square of the closeness.
  assert [round(height, 3) for height in curves["constant"]] == [20.0, 20.0, 20.0, 0.0]
  assert [round(height, 3) for height in curves["smooth"]] == [20.0, round(20 * 20 / 27, 3), round(20 * 7 / 27, 3), 0.0]
  assert [round(height, 3) for height in curves["sharp"]] == [20.0, round(20 * 4 / 9, 3), round(20 * 1 / 9, 3), 0.0]
  # The hill's peak, 30, and its four neighbors, 16.875, lie within 20 of the brush: pressed fully flat at their mean, 19.5; the
  # ground 20 out, 7.5 high, lies beyond it and keeps its height.
  assert all(abs(height - 19.5) < 1e-3 for height in flattened[:5]) and abs(flattened[5] - 7.5) < 1e-3


def testDeleteObjectsRemovesTheirOwnMeshesAndKeepsSharedOnes(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pillar", "size": [6, 6, 20], "location": [0, 0, 0]})
    linked = await session.expectSuccess("duplicateObjects", {"names": ["pillar"], "offset": [15, 0, 0], "linkData": True})
    owned = await session.expectSuccess("duplicateObjects", {"names": ["pillar"], "offset": [30, 0, 0]})
    deleted = await session.expectSuccess("deleteObjects", {"names": [linked["pillar"], owned["pillar"]]})
    pillar = await session.expectSuccess("getObjectDetail", {"name": "pillar"})
    gone = await session.expectError("getObjectDetail", {"name": linked["pillar"]})
    meshes = (await session.expectSuccess("runPython", {"code": "result = sorted(mesh.name for mesh in bpy.data.meshes)"}))["result"]
    return linked, owned, deleted, pillar, gone, meshes

  linked, owned, deleted, pillar, gone, meshes = stageBlenderServer.session(steps)
  # The linked copy shared the pillar's mesh, which stays with the pillar; the full copy's own mesh goes with it.
  assert deleted == {"deleted": [linked["pillar"], owned["pillar"]], "removedMeshes": [owned["pillar"]], "removedSprays": []}
  assert pillar["mesh"] == "pillar" and pillar["sharedMeshUsers"] == 1
  assert f"No object named '{linked['pillar']}'" in gone
  assert meshes == ["pillar"]


def testTopologyEdits(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "tower", "size": [10, 10, 10], "location": [0, 0, 0]})
    extruded = await session.expectSuccess("extrudeFaces", {"objectName": "tower", "selector": upFacing, "distance": 5})
    towerDetail = await session.expectSuccess("getObjectDetail", {"name": "tower"})
    inset = await session.expectSuccess("insetFaces", {"objectName": "tower", "selector": upFacing, "thickness": 1})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "block", "size": [10, 10, 10], "location": [30, 0, 0]})
    beveled = await session.expectSuccess("bevelEdges", {"objectName": "block", "selector": {"all": True}, "width": 0.5})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "tile", "size": [10, 10, 0], "location": [60, 0, 0]})
    subdivided = await session.expectSuccess("subdivide", {"objectName": "tile", "cuts": 3})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "doubled", "size": [10, 10, 0], "location": [90, 0, 0]})
    await session.expectSuccess("extrudeFaces", {"objectName": "doubled", "selector": {"all": True}, "distance": 0})
    cleaned = await session.expectSuccess("cleanupMesh", {"objectName": "doubled"})
    return extruded, towerDetail, inset, beveled, subdivided, cleaned

  extruded, towerDetail, inset, beveled, subdivided, cleaned = stageBlenderServer.session(steps)
  assert (extruded["vertices"], extruded["faces"]) == (12, 10)
  assert towerDetail["dimensions"] == [10.0, 10.0, 15.0]
  assert (inset["vertices"], inset["faces"]) == (16, 14)
  assert (beveled["beveledEdges"], beveled["vertices"], beveled["faces"]) == (12, 24, 26)
  assert (subdivided["vertices"], subdivided["faces"]) == (25, 16)
  assert cleaned["mergedVertices"] == 4
  assert (cleaned["after"]["vertices"], cleaned["after"]["faces"]) == (4, 1)


def testBooleanCutsAndRefusesToErase(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "wall", "size": [20, 20, 20], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "bore", "size": [8, 8, 40], "location": [0, 0, -10], "segments": 16})
    cut = await session.expectSuccess("booleanCut", {"objectName": "wall", "cutterName": "bore"})
    cutterGone = await session.expectError("getObjectDetail", {"name": "bore"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pebble", "size": [2, 2, 2], "location": [100, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "sphere", "name": "dome", "size": [10, 10, 10], "location": [100, 0, -4], "segments": 16})
    erased = await session.expectError("booleanCut", {"objectName": "pebble", "cutterName": "dome"})
    pebble = await session.expectSuccess("getObjectDetail", {"name": "pebble"})
    dome = await session.expectSuccess("getObjectDetail", {"name": "dome"})
    return cut, cutterGone, erased, pebble, dome

  cut, cutterGone, erased, pebble, dome = stageBlenderServer.session(steps)
  assert cut["before"]["faces"] == 6
  assert cut["after"]["faces"] > 6
  assert "No object named 'bore'" in cutterGone
  assert "'dome' does not cross the surface of 'pebble'" in erased and "nothing was changed" in erased
  assert pebble["faces"] == 6
  assert pebble["modifiers"] == []
  assert dome["type"] == "MESH"


async def texturedBlock(session, name, size, location, material, density):
  await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": size, "location": location})
  await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": material})
  await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": density})


def testExtrudedAndInsetFacesRepeatAsTheFacesTheyGrewFrom(stageBlenderServer, tmp_path):
  texture = writePNG(tmp_path / "rock.png", 8, 8, (100, 96, 90, 255))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createMaterial", {"name": "rock", "diffuseTexture": str(texture)})
    await texturedBlock(session, "tower", [30, 30, 30], [0, 0, 0], "rock", 15)
    extruded = await session.expectSuccess("extrudeFaces", {"objectName": "tower", "selector": upFacing, "distance": 30})
    tower = await session.expectSuccess("getObjectDetail", {"name": "tower"})
    await texturedBlock(session, "well", [30, 30, 30], [60, 0, 0], "rock", 15)
    inset = await session.expectSuccess("insetFaces", {"objectName": "well", "selector": upFacing, "thickness": 4, "depth": -6})
    well = await session.expectSuccess("getObjectDetail", {"name": "well"})
    await session.expectSuccess("projectUVs", {"objectName": "well", "method": "box", "worldUnitsPerRepeat": 15})
    wellReprojected = await session.expectSuccess("getObjectDetail", {"name": "well"})
    await session.expectSuccess("createTerrainGrid", {"name": "yard", "size": [160, 160], "spacing": 8, "location": [0, 120, 0]})
    await session.expectSuccess("assignMaterial", {"objectName": "yard", "materialName": "rock"})
    await session.expectSuccess("projectUVs", {"objectName": "yard", "method": "planar", "worldUnitsPerRepeat": 64, "direction": [0, 0, 1]})
    raised = await session.expectSuccess("extrudeFaces", {"objectName": "yard", "selector": {"box": {"minimum": [-30, 90, -1], "maximum": [30, 150, 1]}}, "distance": 12})
    yard = await session.expectSuccess("getObjectDetail", {"name": "yard"})
    return extruded, tower, inset, well, wellReprojected, raised, yard

  extruded, tower, inset, well, wellReprojected, raised, yard = stageBlenderServer.session(steps)
  # The cube was box-projected at 15 units a repeat; the walls extrusion adds repeat at 15 as well, where their UVs copied from the rim
  # stretched them to about 19.7 over the whole mesh.
  assert extruded["mappedFaces"] == [{"material": "rock", "faces": 4, "worldUnitsPerRepeat": 15.0}]
  assert tower["worldUnitsPerTextureRepeat"] == 15.0
  # The recess walls inset adds slope inward, so box projection at 15 spreads them a little (15.17 over the mesh): exactly what
  # projecting the whole mesh again gives.
  assert inset["mappedFaces"] == [{"material": "rock", "faces": 4, "worldUnitsPerRepeat": 15.0}]
  assert 15.0 < well["worldUnitsPerTextureRepeat"] == wellReprojected["worldUnitsPerTextureRepeat"] < 15.5
  # A patch of ground 8 cells square raised 12: its 32 sides repeat at the ground's 64.
  assert raised["mappedFaces"] == [{"material": "rock", "faces": 32, "worldUnitsPerRepeat": 64.0}]
  assert yard["worldUnitsPerTextureRepeat"] == 64.0
  assert extruded["unmappedFaces"] == inset["unmappedFaces"] == raised["unmappedFaces"] == []


seamCode = """
column = bpy.data.objects['column']
mesh = column.data
layer = mesh.uv_layers.active.data
seam = {}
for polygon in mesh.polygons:
  if abs(polygon.normal.z) > 0.5:
    continue
  side = (round(polygon.normal.x, 4), round(polygon.normal.y, 4))
  for index in polygon.loop_indices:
    vertex = mesh.loops[index].vertex_index
    if abs((column.matrix_world @ mesh.vertices[vertex].co).z - 10) < 1e-4:
      seam.setdefault(str((vertex, side)), []).append([round(value, 5) for value in layer[index].uv])
result = seam
"""


def testExtrudedFacesOfARoundMeshContinueItsBoxMapping(stageBlenderServer, tmp_path):
  texture = writePNG(tmp_path / "rock.png", 8, 8, (100, 96, 90, 255))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createMaterial", {"name": "rock", "diffuseTexture": str(texture)})
    for name, height, x in (("column", 10, 0), ("tallColumn", 30, 20)):
      await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": name, "size": [12, 12, height], "location": [x, 0, 0], "segments": 16})
      await session.expectSuccess("assignMaterial", {"objectName": name, "materialName": "rock"})
      await session.expectSuccess("projectUVs", {"objectName": name, "method": "box", "worldUnitsPerRepeat": 6})
    extruded = await session.expectSuccess("extrudeFaces", {"objectName": "column", "selector": upFacing, "distance": 20})
    column = await session.expectSuccess("getObjectDetail", {"name": "column"})
    tallColumn = await session.expectSuccess("getObjectDetail", {"name": "tallColumn"})
    seam = (await session.expectSuccess("runPython", {"code": seamCode}))["result"]
    return extruded, column, tallColumn, seam

  extruded, column, tallColumn, seam = stageBlenderServer.session(steps)
  # Box projection at 6 maps the column's slanted sides at more than 6 world units a repeat of their true area, so the column measures
  # 6.19 before the edit; the 16 new sides are mapped at the projection's own 6, not at that measure, so the column grown 20 maps
  # exactly as one projected 30 tall.
  assert extruded["mappedFaces"] == [{"material": "rock", "faces": 16, "worldUnitsPerRepeat": 6.0}] and extruded["unmappedFaces"] == []
  assert column["worldUnitsPerTextureRepeat"] == tallColumn["worldUnitsPerTextureRepeat"] > 6.1
  # At the old top edge each corner has one UV in the side below and the side grown above it: the texture runs on without a seam.
  assert len(seam) == 32 and all(len(uvs) == 2 and uvs[0] == uvs[1] for uvs in seam.values())


wallMappingCode = """
mesh = bpy.data.objects['ground'].data
uvs = mesh.uv_layers.active.data
result = [
  [mesh.materials[polygon.material_index].name, [list(uvs[index].uv) for index in polygon.loop_indices]] for polygon in mesh.polygons
  if abs(polygon.normal.z) < 0.1
]
"""


def testExtrudedFacesKeepTheirMappingWhenSurfacingLayersComposeAgain(stageBlenderServer, tmp_path):
  async def steps(session):
    await freshScene(session)
    for name, color in (("sand", (200, 180, 120, 255)), ("stone", (120, 80, 60, 255)), ("blend", (230, 120, 40, 255))):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [96, 96], "spacing": 8, "location": [0, 0, 0]})
    await session.expectSuccess("projectUVs", {"objectName": "ground", "method": "box", "worldUnitsPerRepeat": 16})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "ground"})
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"all": True}})
    block = {"box": {"minimum": [-17, -17, -1], "maximum": [17, 17, 1]}}
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "stone", "selector": block})
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "blend"})
    strip = await session.expectSuccess("paintTransition", {
      "objectName": "ground", "layer": "blend", "material": "blend", "selector": {"material": "stone"}, "toward": {"material": "sand"},
      "width": 8, "worldUnitsPerRepeat": 16,
    })
    extruded = await session.expectSuccess("extrudeFaces", {"objectName": "ground", "selector": block, "distance": 12, "direction": [0, 0, 1]})
    walls = (await session.expectSuccess("runPython", {"code": wallMappingCode}))["result"]
    await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "ground", "material": "sand", "selector": {"box": {"minimum": [40, 40, -1], "maximum": [48, 48, 1]}}})
    recomposed = (await session.expectSuccess("runPython", {"code": wallMappingCode}))["result"]
    return strip, extruded, walls, recomposed

  strip, extruded, walls, recomposed = stageBlenderServer.session(steps)

  def uvArea(corners):
    return abs(sum(u0 * v1 - u1 * v0 for (u0, v0), (u1, v1) in zip(corners, corners[1:] + corners[:1]))) / 2

  # The block's outer ring of stone carries a transition with its own mapping; the 16 walls the extrusion grows from it, 8 wide and 12
  # tall, are box-mapped at the density of the material each takes.
  densities = {entry["material"]: entry["worldUnitsPerRepeat"] for entry in extruded["mappedFaces"]}
  assert strip["painted"] > 0 and "blend" in densities and sum(entry["faces"] for entry in extruded["mappedFaces"]) == 16
  assert extruded["unmappedFaces"] == [] and len(walls) == 16
  assert all(abs(uvArea(corners) - 8 * 12 / densities[material] ** 2) < 1e-3 * uvArea(corners) for material, corners in walls)
  # Painting elsewhere composes every face's mapping from the layers again: the walls keep theirs, neither the corners' copies from the
  # faces they grew from nor the transition's.
  assert [material for material, _ in recomposed] == [material for material, _ in walls]
  assert all(
    abs(a - b) < 1e-6 for (_, before), (_, after) in zip(walls, recomposed) for cornerBefore, cornerAfter in zip(before, after)
    for a, b in zip(cornerBefore, cornerAfter)
  )


blankLayerCode = """
layer = bpy.data.objects['blank'].data.uv_layers.new(name='blank')
layer.data.foreach_set('uv', [0.0] * (2 * len(layer.data)))
result = layer.name
"""


def testFacesAnEditCannotMapAreListedWithWhy(stageBlenderServer, tmp_path):
  texture = writePNG(tmp_path / "stone.png", 8, 8, (120, 110, 100, 255))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "plain", "size": [10, 10, 10], "location": [0, 0, 0]})
    unmapped = await session.expectSuccess("extrudeFaces", {"objectName": "plain", "selector": upFacing, "distance": 5})
    await session.expectSuccess("createMaterial", {"name": "stone", "diffuseTexture": str(texture)})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "blank", "size": [10, 10, 10], "location": [30, 0, 0]})
    await session.expectSuccess("assignMaterial", {"objectName": "blank", "materialName": "stone"})
    await session.expectSuccess("runPython", {"code": blankLayerCode})
    blank = await session.expectSuccess("insetFaces", {"objectName": "blank", "selector": upFacing, "thickness": 2, "depth": -1})
    return unmapped, blank

  unmapped, blank = stageBlenderServer.session(steps)
  # A mesh with no UV layer has nothing to map its new faces on; a material whose faces have no UV area gives no density to map at.
  assert unmapped["mappedFaces"] == [] and unmapped["unmappedFaces"] == [{"material": None, "faces": 4, "reason": "the mesh has no UV layer"}]
  assert blank["mappedFaces"] == []
  assert blank["unmappedFaces"] == [{"material": "stone", "faces": 4, "reason": "the material's faces on the mesh had no UV area to take a density from"}]


caveFacesCode = """
import math
import numpy
plot = bpy.data.objects['plot']
mesh = plot.data
layer = mesh.uv_layers.active.data
rows = []
for polygon in mesh.polygons:
  center = plot.matrix_world @ polygon.center
  if center.x > -99.5 and abs(math.hypot(center.y - 105, center.z - 17) - 12) < 0.6:
    corners = [plot.matrix_world @ mesh.vertices[mesh.loops[index].vertex_index].co for index in polygon.loop_indices]
    uvs = [layer[index].uv for index in polygon.loop_indices]
    area = sum(((corners[0] - corners[index]).cross(corners[0] - corners[index + 1])).length / 2 for index in range(1, len(corners) - 1))
    uvArea = sum(abs((uvs[index] - uvs[0]).cross(uvs[index + 1] - uvs[0])) / 2 for index in range(1, len(uvs) - 1))
    rows.append([plot.material_slots[polygon.material_index].material.name, area, uvArea])
result = rows
"""


def testBooleanCutLinesTheOpeningWithTheMaterialItCutsThrough(stageBlenderServer, tmp_path):
  stone = writePNG(tmp_path / "stone.png", 8, 8, (120, 110, 100, 255))
  grass = writePNG(tmp_path / "grass.png", 8, 8, (60, 120, 40, 255))

  async def steps(session):
    await freshScene(session)
    for name, texture in (("stone", stone), ("grass", grass), ("cliff", stone)):
      await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(texture)})
    await texturedBlock(session, "wall", [40, 6, 20], [0, -100, 0], "stone", 20)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "doorCutter", "size": [10, 10, 14], "location": [0, -100, -1]})
    doorway = await session.expectSuccess("booleanCut", {"objectName": "wall", "cutterName": "doorCutter"})
    wall = await session.expectSuccess("getObjectDetail", {"name": "wall"})
    await session.expectSuccess("createTerrainGrid", {"name": "plot", "size": [320, 320], "spacing": 8, "location": [0, 0, 0]})
    await session.expectSuccess("sculptOutline", {"objectName": "plot", "mode": "fill", "outline": [[-100, 60], [0, 60], [0, 150], [-100, 150]], "base": 0, "profile": [[-4, 0], [0, 40], [300, 40]]})
    await session.expectSuccess("assignMaterial", {"objectName": "plot", "materialName": "grass"})
    await session.expectSuccess("assignMaterial", {"objectName": "plot", "materialName": "cliff", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
    await session.expectSuccess("projectUVs", {"objectName": "plot", "method": "box", "worldUnitsPerRepeat": 64})
    plotBefore = await session.expectSuccess("getObjectDetail", {"name": "plot"})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "caveCutter", "size": [24, 24, 50], "location": [-130, 105, 17], "rotationDegrees": [0, 90, 0], "segments": 16})
    cave = await session.expectSuccess("booleanCut", {"objectName": "plot", "cutterName": "caveCutter"})
    plotAfter = await session.expectSuccess("getObjectDetail", {"name": "plot"})
    lining = (await session.expectSuccess("runPython", {"code": caveFacesCode}))["result"]
    await session.expectSuccess("createTerrainGrid", {"name": "lawn", "size": [160, 160], "spacing": 8, "location": [0, 300, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "pitCutter", "size": [30, 30, 30], "location": [0, 300, -15], "segments": 16})
    pit = await session.expectSuccess("booleanCut", {"objectName": "lawn", "cutterName": "pitCutter"})
    return doorway, wall, plotBefore, cave, plotAfter, lining, pit

  doorway, wall, plotBefore, cave, plotAfter, lining, pit = stageBlenderServer.session(steps)
  # The doorway's jambs and lintel are stone at the wall's 20 a repeat, and the wall gains no empty material slot.
  assert doorway["madeFaces"] > 0 and doorway["mappedFaces"] == [{"material": "stone", "faces": doorway["madeFaces"], "worldUnitsPerRepeat": 20.0}]
  assert [entry["material"] for entry in wall["materials"]] == ["stone"]
  assert wall["worldUnitsPerTextureRepeat"] == 20.0
  # The cave cut into the cliff is lined with cliff, mapped, not grass at constant UVs; the grass is untouched and no slot is added.
  assert cave["madeFaces"] > 0 and [entry["material"] for entry in cave["mappedFaces"]] == ["cliff"]
  assert [entry["material"] for entry in plotAfter["materials"]] == ["grass", "cliff"]
  assert plotAfter["materials"][0]["faces"] == plotBefore["materials"][0]["faces"]
  assert len(lining) >= 16 and all(name == "cliff" for name, _, _ in lining)
  # The cliff was box-projected at 64, and the lining is too: each face along its nearest axis, at most 45 degrees off it round the
  # cave's axis, so its true area repeats between every 64 and 64 over the square root of cos 45.
  density = cave["mappedFaces"][0]["worldUnitsPerRepeat"]
  assert density == 64.0
  assert all(uvArea > 0 and density - 0.01 <= (area / uvArea) ** 0.5 <= density / 0.5 ** 0.25 + 0.01 for _, area, uvArea in lining)
  assert "warning" not in doorway and "warning" not in cave
  # A cylinder through flat open ground keeps none of its faces: the ground encloses nothing below it, so the opening is a bare hole
  # into the void, and the result says so.
  assert pit["madeFaces"] == 0 and pit["mappedFaces"] == [] and pit["after"]["faces"] != pit["before"]["faces"]
  assert pit["warning"].startswith("The cut left an opening in 'lawn' with nothing lining it: no face of 'pitCutter' was kept")
  assert "to dig a pit into open ground, shape the ground instead (sculptAtPoint lower, sculptAlongPath carve)" in pit["warning"]


def testDecimateHalvesTriangles(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "field", "size": [100, 100], "spacing": 5, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "field", "mode": "raise", "center": [0, 0, 0], "radius": 40, "strength": 15})
    return await session.expectSuccess("decimate", {"objectName": "field", "ratio": 0.5})

  decimated = stageBlenderServer.session(steps)
  assert decimated["before"]["triangles"] == 800
  assert 0.45 * 800 <= decimated["after"]["triangles"] <= 0.55 * 800


def testMaterialsAndWorldScaledUVs(stageBlenderServer, tmp_path):
  rockTexture = writePNG(tmp_path / "rock.png", 8, 8, (100, 96, 90, 255))
  leafTexture = writePNG(tmp_path / "leaf.png", 8, 8, (40, 120, 30, 128))

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [10, 10, 10], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "floor", "size": [64, 64, 0], "location": [0, 40, 0]})
    rock = await session.expectSuccess("createMaterial", {"name": "rockMaterial", "diffuseTexture": str(rockTexture)})
    leaf = await session.expectSuccess("createMaterial", {"name": "leafMaterial", "diffuseTexture": str(leafTexture), "cutout": True})
    missing = await session.expectError("createMaterial", {"name": "ghostMaterial", "diffuseTexture": str(tmp_path / "absent.png")})
    await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "rockMaterial"})
    topOnly = await session.expectSuccess("assignMaterial", {"objectName": "crate", "materialName": "leafMaterial", "selector": upFacing})
    await session.expectSuccess("projectUVs", {"objectName": "floor", "method": "planar", "worldUnitsPerRepeat": 32, "direction": [0, 0, 1]})
    await session.expectSuccess("projectUVs", {"objectName": "crate", "method": "box", "worldUnitsPerRepeat": 5})
    crate = await session.expectSuccess("getObjectDetail", {"name": "crate"})
    floor = await session.expectSuccess("getObjectDetail", {"name": "floor"})
    summary = await session.expectSuccess("getSceneSummary")
    return rock, leaf, missing, topOnly, crate, floor, summary

  rock, leaf, missing, topOnly, crate, floor, summary = stageBlenderServer.session(steps)
  assert rock == {"material": "rockMaterial", "diffuseTexture": "rock.png", "normalTexture": None, "cutout": False, "blockout": False}
  assert leaf["cutout"] is True
  assert "absent.png' is not an existing absolute path" in missing
  assert "ghostMaterial" not in [material["name"] for material in summary["materials"]]
  assert topOnly == {"object": "crate", "material": "leafMaterial", "faces": 1, "slot": 1}
  assert crate["materials"] == [{"material": "rockMaterial", "faces": 5}, {"material": "leafMaterial", "faces": 1}]
  assert crate["worldUnitsPerTextureRepeat"] == 5.0
  assert floor["worldUnitsPerTextureRepeat"] == 32.0
  assert {"name": "rockMaterial", "textures": ["rock.png"]} in summary["materials"]


def testTransformDuplicateOrganizeAndMeasure(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [2, 2, 2], "location": [0, 0, 0]})
    moved = await session.expectSuccess("transformObjects", {"names": ["crate"], "translate": [5, 0, 0], "rotateDegrees": [0, 0, 90]})
    conflicting = await session.expectError("transformObjects", {"names": ["crate"], "translate": [1, 0, 0], "location": [0, 0, 0]})
    copies = await session.expectSuccess("duplicateObjects", {"names": ["crate"], "offset": [10, 0, 0], "linkData": True})
    beforeParenting = await session.expectSuccess("getObjectDetail", {"name": copies["crate"]})
    await session.expectSuccess("organize", {"renames": {copies["crate"]: "lid"}, "parents": {"lid": "crate"}})
    await session.expectSuccess("organize", {"collections": {"lid": "props"}})
    lid = await session.expectSuccess("getObjectDetail", {"name": "lid"})
    measured = await session.expectSuccess("measure", {"points": [[0, 0, 0], [3, 4, 0], [7, 4, 3]]})
    return moved, conflicting, copies, beforeParenting, lid, measured

  moved, conflicting, copies, beforeParenting, lid, measured = stageBlenderServer.session(steps)
  assert moved["objects"][0]["location"] == [5.0, 0.0, 0.0]
  assert moved["objects"][0]["rotationDegrees"][2] == 90.0
  assert "Pass translate or location, not both" in conflicting
  assert copies == {"crate": "crate.001"}
  assert beforeParenting["sharedMeshUsers"] == 2
  assert lid["parent"] == "crate"
  assert lid["collections"] == ["props"]
  assert lid["worldMinimum"] == beforeParenting["worldMinimum"]
  assert lid["worldMaximum"] == beforeParenting["worldMaximum"]
  assert [segment["distance"] for segment in measured["segments"]] == [5.0, 5.0]
  assert measured["segments"][1] == {"distance": 5.0, "horizontalDistance": 4.0, "heightChange": 3.0, "slopeDegrees": 36.87}
  assert measured["totalDistance"] == 10.0


def testPlaceOnSurfaceAndScatter(stageBlenderServer):
  scatter = {
    "sourceObject": "shrub", "region": {"circle": {"center": [-60, -60], "radius": 30}}, "density": 40, "minimumSpacing": 4,
    "scaleRange": [0.8, 1.2], "maximumSlopeDegrees": 30, "seed": 11, "surfaceObjects": ["ground"],
  }

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 40, "strength": 20, "falloff": "linear"})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [2, 2, 2], "location": [0, 0, 100]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "barrel", "size": [2, 2, 2], "location": [0, 0, 0]})
    placed = await session.expectSuccess("placeOnSurface", {"objectNames": ["crate", "barrel"], "at": [[0, 0, 100], [20, 0, 100]], "alignToNormal": True, "surfaceObjects": ["ground"]})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "shrub", "size": [2, 2, 4], "location": [0, 0, -500], "segments": 6})
    first = await session.expectSuccess("scatterInRegion", scatter | {"collection": "shrubs"})
    second = await session.expectSuccess("scatterInRegion", scatter | {"collection": "shrubsAgain"})
    summary = await session.expectSuccess("getSceneSummary", {"objectLimit": 500})
    return placed, first, second, summary

  placements, first, second, summary = stageBlenderServer.session(steps)
  placed = placements["placements"]
  assert placed[0]["location"] == [0.0, 0.0, 20.0]
  assert placed[0]["surface"] == "ground"
  assert placed[1]["location"][2] == 10.0
  assert placed[1]["slopeDegrees"] > 0
  expectedCount = round(40 * math.pi * 30 * 30 / 10000)
  assert first["targetCount"] == expectedCount
  assert first["placed"] == expectedCount
  assert first["rejected"] == {"noSurface": 0, "tooSteep": 0, "nearAvoidedObject": 0}
  assert first["sharedMesh"] == "shrub"
  byCollection = {}
  for sceneObject in summary["objects"]:
    for collection in sceneObject["collections"]:
      byCollection.setdefault(collection, []).append(sceneObject["location"])
  shrubs, shrubsAgain = byCollection["shrubs"], byCollection["shrubsAgain"]
  assert len(shrubs) == expectedCount
  assert sorted(shrubs) == sorted(shrubsAgain)
  assert all(location[2] == 0.0 for location in shrubs)
  assert all(math.hypot(location[0] + 60, location[1] + 60) <= 30 for location in shrubs)
  assert min(math.dist(a[:2], b[:2]) for a, b in itertools.combinations(shrubs, 2)) >= 4


def testKitAssetsLinkAsRelativeLibraries(stageBlenderServer, tmp_path):
  kitPath = tmp_path / "kits" / "rocks.blend"
  kitPath.parent.mkdir()
  (tmp_path / "textures").mkdir()
  rockTexture = writePNG(tmp_path / "textures" / "rock.png", 8, 8, (100, 96, 90, 255))
  zonePath = tmp_path / "zones" / "zone.blend"
  zonePath.parent.mkdir()

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [4, 4, 3], "location": [0, 0, 0]})
    await session.expectSuccess("createMaterial", {"name": "rockMaterial", "diffuseTexture": str(rockTexture)})
    await session.expectSuccess("assignMaterial", {"objectName": "boulder", "materialName": "rockMaterial"})
    await session.expectSuccess("organize", {"collections": {"boulder": "boulderKit"}})
    marked = await session.expectSuccess("markAsset", {"collectionName": "boulderKit"})
    await session.expectSuccess("saveFile", {"path": str(kitPath)})
    await session.expectSuccess("newFile")
    linked = await session.expectSuccess("linkKitAsset", {"kitPath": str(kitPath), "assetName": "boulderKit", "instanceName": "boulderA", "location": [10, 0, 0]})
    unknown = await session.expectError("linkKitAsset", {"kitPath": str(kitPath), "assetName": "cliffKit", "instanceName": "cliffA", "location": [0, 0, 0]})
    instance = await session.expectSuccess("getObjectDetail", {"name": "boulderA"})
    await session.expectSuccess("saveFile", {"path": str(zonePath)})
    summary = await session.expectSuccess("getSceneSummary")
    return marked, linked, unknown, instance, summary

  marked, linked, unknown, instance, summary = stageBlenderServer.session(steps)
  assert marked == {"asset": "boulderKit", "objects": ["boulder"]}
  assert linked["location"] == [10.0, 0.0, 0.0]
  assert linked["asset"] == "boulderKit"
  assert "'cliffKit' is not a collection marked as an asset" in unknown
  assert "['boulderKit']" in unknown
  assert instance["instanceCollection"] == "boulderKit"
  assert summary["libraries"] == [{"name": "rocks.blend", "filePath": "//..\\kits\\rocks.blend"}]
  linkedImages = [image for image in summary["images"] if image["name"] == "rock.png"]
  assert [image["filePath"] for image in linkedImages] == ["//..\\textures\\rock.png"]


def testPlaceOnSurfaceCastsFromAboveTheObjectAndNeverLandsOnWhatItCarries(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [200, 200], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, 0, 0], "radius": 40, "strength": 20, "falloff": "linear"})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "barrel", "size": [4, 4, 16], "location": [20, 0, 0], "segments": 8})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [8, 8, 8], "location": [-60, 60, 30]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "lid", "size": [9, 9, 1], "location": [-60, 60, 38]})
    await session.expectSuccess("organize", {"parents": {"lid": "crate"}})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "overhang", "size": [30, 30, 2], "location": [60, -60, 20]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "chest", "size": [4, 4, 4], "location": [60, -60, 8]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pebble", "size": [2, 2, 2], "location": [0, 0, 0]})
    placed = await session.expectSuccess("placeOnSurface", {"objectNames": ["barrel", "chest"]})
    carried = await session.expectSuccess("placeOnSurface", {"objectNames": ["crate"], "at": [[-60, 60, 100]]})
    lid = await session.expectSuccess("getObjectDetail", {"name": "lid"})
    buried = await session.expectError("placeOnSurface", {"objectNames": ["pebble"]})
    return placed, carried, lid, buried

  placed, carried, lid, buried = stageBlenderServer.session(steps)
  # The barrel's origin is 10 under the hillside, its top 6 above it: cast from just over its top, it lands on the ground there.
  barrel, chest = placed["placements"]
  assert barrel["surface"] == "ground" and barrel["location"] == [20.0, 0.0, 10.0]
  # Under the overhang the cast starts below it, so the chest lands on the ground, not on the overhang's top.
  assert chest["surface"] == "ground" and chest["location"] == [60.0, -60.0, 0.0]
  # Dropped from above, the crate lands on the ground, not on its own lid, and carries the lid down with it.
  assert carried["placements"][0]["surface"] == "ground" and carried["placements"][0]["location"] == [-60.0, 60.0, 0.0]
  assert lid["worldMinimum"][2] == 8.0 and lid["worldMaximum"][2] == 9.0
  # A pebble wholly under the hilltop has no ground below its top.
  assert "No surface below" in buried and "cast from just above its top" in buried


wedgeCode = """
wedge = bpy.data.objects['quaternionWedge']
wedge.rotation_mode = 'QUATERNION'
result = [round(value, 4) for value in wedge.rotation_quaternion]
"""


def testObjectsReportTheRotationTheyHaveInAnyRotationMode(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "ramp", "size": [60, 60, 4], "location": [0, 0, 0], "rotationDegrees": [20, 0, 0]})
    for name, x in (("eulerWedge", -6), ("quaternionWedge", 6)):
      await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [2, 2, 2], "location": [x, 0, 40], "rotationDegrees": [0, 0, 30]})
    quaternion = (await session.expectSuccess("runPython", {"code": wedgeCode}))["result"]
    placed = await session.expectSuccess("placeOnSurface", {"objectNames": ["eulerWedge", "quaternionWedge"], "alignToNormal": True})
    details = [await session.expectSuccess("getObjectDetail", {"name": name}) for name in ("eulerWedge", "quaternionWedge")]
    return quaternion, placed, details

  quaternion, placed, (eulerWedge, quaternionWedge) = stageBlenderServer.session(steps)
  # The same turn of 30 held as a quaternion; both wedges tilt 20 onto the ramp keeping that heading: a turn of 20 about x after 30
  # about z, which as XYZ Euler angles is (x, y, z) below.
  assert quaternion == [round(math.cos(math.radians(15)), 4), 0.0, 0.0, round(math.sin(math.radians(15)), 4)]
  assert [placement["slopeDegrees"] for placement in placed["placements"]] == [20.0, 20.0]
  tilt, turn = math.radians(20), math.radians(30)
  expected = [
    math.degrees(math.atan2(math.sin(tilt) * math.cos(turn), math.cos(tilt))), math.degrees(math.asin(-math.sin(tilt) * math.sin(turn))),
    math.degrees(math.atan2(math.cos(tilt) * math.sin(turn), math.cos(turn))),
  ]
  for wedge in (eulerWedge, quaternionWedge):
    assert all(abs(reported - angle) <= 0.01 for reported, angle in zip(wedge["rotationDegrees"], expected)), (wedge["rotationDegrees"], expected)


emptyInstanceCode = """
instance = bpy.data.objects.new('emptyInstance', None)
instance.instance_type = 'COLLECTION'
instance.instance_collection = bpy.data.collections.new('emptyKit')
bpy.context.scene.collection.objects.link(instance)
result = instance.name
"""


def testAnInstanceOfNoMeshesHasNoSizeOrBounds(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("runPython", {"code": emptyInstanceCode})
    detail = await session.expectSuccess("getObjectDetail", {"name": "emptyInstance"})
    summary = await session.expectSuccess("getSceneSummary")
    return detail, summary

  detail, summary = stageBlenderServer.session(steps)
  assert detail["instanceCollection"] == "emptyKit"
  assert detail["dimensions"] is None and detail["worldMinimum"] is None and detail["worldMaximum"] is None
  assert [sceneObject["dimensions"] for sceneObject in summary["objects"] if sceneObject["name"] == "emptyInstance"] == [None]


def testScatteredCopiesScaleFromTheSourcesOwnScale(stageBlenderServer):
  scatter = {"sourceObject": "shrub", "region": {"circle": {"center": [0, 0], "radius": 30}}, "density": 20, "minimumSpacing": 6, "seed": 3}

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "shrub", "size": [2, 2, 4], "location": [0, 0, -50], "segments": 6})
    await session.expectSuccess("transformObjects", {"names": ["shrub"], "scale": [2, 2, 3]})
    await session.expectSuccess("scatterInRegion", scatter | {"collection": "kept", "scaleRange": [1, 1]})
    await session.expectSuccess("scatterInRegion", scatter | {"collection": "halved", "scaleRange": [0.5, 0.5]})
    return await session.expectSuccess("getSceneSummary", {"objectLimit": 100})

  summary = stageBlenderServer.session(steps)
  byCollection = {}
  for sceneObject in summary["objects"]:
    for collection in sceneObject["collections"]:
      byCollection.setdefault(collection, []).append(sceneObject)
  # The shrub, 2 x 2 x 4, stands scaled to 4 x 4 x 12: copies at scale 1 keep that, and copies at a half are half of it.
  assert len(byCollection["kept"]) == len(byCollection["halved"]) > 1
  assert all(sceneObject["dimensions"] == [4.0, 4.0, 12.0] for sceneObject in byCollection["kept"])
  assert all(sceneObject["dimensions"] == [2.0, 2.0, 6.0] for sceneObject in byCollection["halved"])


def testCopiesCarryTheirChildrenAndMeshesTakeTheirObjectsNames(stageBlenderServer):
  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "crate", "size": [8, 8, 8], "location": [0, 0, 0]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "lid", "size": [9, 9, 1], "location": [0, 0, 8]})
    await session.expectSuccess("organize", {"parents": {"lid": "crate"}})
    copies = await session.expectSuccess("duplicateObjects", {"names": ["crate"], "offset": [20, 0, 0]})
    lidCopy = await session.expectSuccess("getObjectDetail", {"name": copies["lid"]})
    linkedCopies = await session.expectSuccess("duplicateObjects", {"names": ["crate", "lid"], "offset": [40, 0, 0], "linkData": True})
    linkedLid = await session.expectSuccess("getObjectDetail", {"name": linkedCopies["lid"]})
    await session.expectSuccess("createPrimitive", {"kind": "cylinder", "name": "trunk", "size": [3, 3, 16], "location": [0, 40, 0], "segments": 8})
    await session.expectSuccess("createPrimitive", {"kind": "cone", "name": "canopy", "size": [16, 16, 26], "location": [0, 40, 10], "segments": 8})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['trunk'].data.name = 'Cylinder'\nresult = bpy.data.objects['trunk'].data.name"})
    await session.expectSuccess("joinObjects", {"names": ["trunk", "canopy"], "into": "trunk"})
    joined = await session.expectSuccess("getObjectDetail", {"name": "trunk"})
    sharing = await session.expectSuccess("duplicateObjects", {"names": ["trunk"], "offset": [20, 0, 0], "linkData": True})
    await session.expectSuccess("organize", {"renames": {"trunk": "tree", sharing["trunk"]: "treeCopy"}})
    tree = await session.expectSuccess("getObjectDetail", {"name": "tree"})
    await session.expectSuccess("deleteObjects", {"names": ["treeCopy"]})
    await session.expectSuccess("organize", {"renames": {"tree": "pine"}})
    pine = await session.expectSuccess("getObjectDetail", {"name": "pine"})
    return copies, lidCopy, linkedCopies, linkedLid, joined, tree, pine

  copies, lidCopy, linkedCopies, linkedLid, joined, tree, pine = stageBlenderServer.session(steps)
  # A copy of the crate brings its lid, parented to the copy where the original lid sits on the original, and named meshes of its own.
  assert copies == {"crate": "crate.001", "lid": "lid.001"}
  assert lidCopy["parent"] == "crate.001" and lidCopy["mesh"] == "lid.001"
  assert lidCopy["worldMinimum"] == [15.5, -4.5, 8.0] and lidCopy["worldMaximum"] == [24.5, 4.5, 9.0]
  # Naming the lid alongside the crate copies it once, with the crate; linked copies share the originals' meshes.
  assert linkedCopies == {"crate": "crate.002", "lid": "lid.002"}
  assert linkedLid["parent"] == "crate.002" and linkedLid["mesh"] == "lid" and linkedLid["sharedMeshUsers"] == 2
  assert linkedLid["worldMinimum"] == [35.5, -4.5, 8.0]
  # The joined tree's mesh takes the tree's name, which export writes as its model; a mesh linked copies share keeps its own name
  # through a rename, and takes the new one once it is the object's alone.
  assert joined["mesh"] == "trunk"
  assert tree["mesh"] == "trunk" and tree["sharedMeshUsers"] == 2
  assert pine["mesh"] == "pine" and pine["sharedMeshUsers"] == 1


def testKitInstancesAreMeasuredSizedAndLitAsTheFilesOwnMeshes(stageBlenderServer, tmp_path):
  kitPath = tmp_path / "kits" / "rocks.blend"
  kitPath.parent.mkdir()
  rockTexture = writePNG(tmp_path / "rock.png", 8, 8, (150, 120, 90, 255))
  environment = {
    "ambientColor": [0.2, 0.2, 0.25], "specialAmbientColor": [0, 0, 0], "bounceColor": [0.1, 0.1, 0.1], "sunColor": [0.6, 0.5, 0.4],
    "sunAzimuthDegrees": 180, "sunElevationDegrees": 30, "fogColor": [0.4, 0.5, 0.6], "fogStart": 0, "fogEnd": 1000, "fogDensity": 0, "fogOn": False,
    "maxClip": 2000, "newEngineZone": False,
  }

  async def steps(session):
    await freshScene(session)
    await session.expectSuccess("createMaterial", {"name": "rock", "diffuseTexture": str(rockTexture)})
    await texturedBlock(session, "boulder", [8, 8, 6], [0, 0, 0], "rock", 8)
    await session.expectSuccess("organize", {"collections": {"boulder": "boulderKit"}})
    await session.expectSuccess("markAsset", {"collectionName": "boulderKit"})
    await session.expectSuccess("saveFile", {"path": str(kitPath)})
    await session.expectSuccess("newFile")
    await session.expectSuccess("createMaterial", {"name": "rockHere", "diffuseTexture": str(rockTexture)})
    await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [100, 100], "spacing": 10, "location": [0, 0, 0]})
    await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "rockHere"})
    linked = await session.expectSuccess("linkKitAsset", {"kitPath": str(kitPath), "assetName": "boulderKit", "instanceName": "boulderA", "location": [-10, 0, 0], "scale": [1, 1, 1.5]})
    await texturedBlock(session, "boulderLocal", [8, 8, 6], [10, 0, 0], "rockHere", 8)
    instance = await session.expectSuccess("getObjectDetail", {"name": "boulderA"})
    await session.expectSuccess("setZoneProperties", environment)
    await session.expectSuccess("placeSpawn", {"zone": None, "model": "DAF", "name": "darkElf", "height": 5, "location": [0, 25, 0], "headingDegrees": 0})
    measured = await session.expectSuccess("measure", {"points": [[-10, 0, 100], [10, 0, 100], [0, 25, 100]], "snapToSurface": True})
    kitFace = await renderedPixel(session, {"eye": [-10, -20, 4], "target": [-10, 0, 4]})
    localFace = await renderedPixel(session, {"eye": [10, -20, 4], "target": [10, 0, 4]})
    summary = await session.expectSuccess("getSceneSummary")
    return linked, instance, measured, kitFace, localFace, summary

  linked, instance, measured, kitFace, localFace, summary = stageBlenderServer.session(steps)
  # The instance's size is its boulder's, scaled, where an empty's own size reads 0, in its detail and the scene's summary alike.
  assert linked["dimensions"] == [8.0, 8.0, 9.0] and instance["dimensions"] == [8.0, 8.0, 9.0]
  assert [sceneObject["dimensions"] for sceneObject in summary["objects"] if sceneObject["name"] == "boulderA"] == [[8.0, 8.0, 9.0]]
  assert instance["worldMinimum"] == [-14.0, -4.0, 0.0] and instance["worldMaximum"] == [-6.0, 4.0, 9.0]
  # Players stand on the instance, as walkRoute has them, and pass through the spawn to the ground.
  assert [point[2] for point in measured["points"]] == [9.0, 6.0, 0.0]
  # The kit's boulder face toward the sun draws as the file's own boulder's does, lit by the zone, not by the kit's saved light.
  assert all(abs(kit - local) <= 1.5 / 255 for kit, local in zip(kitFace, localFace)), (kitFace, localFace)


def testRunPythonCallsAreCounted(stageBlenderServer):
  async def steps(session):
    before = (await session.expectSuccess("getToolingStatus"))["runPython"]["calls"]
    await session.expectSuccess("runPython", {"code": "result = 1"})
    await session.expectSuccess("runPython", {"code": "result = 2"})
    after = await session.expectSuccess("getToolingStatus")
    return before, after["runPython"]

  before, after = stageBlenderServer.session(steps)
  assert after["calls"] == before + 2
  assert len(after["logFiles"]) >= 1
