import json
import re

import numpy

from conftest import writePNG

# A tunnel 40 wide and 45 tall from the cliff foot, rising to a room 120 wide and 70 tall that ends blind.
hall = {
  "objectName": "ground", "name": "hall", "path": [[0, -60, 2], [0, 30, 6], [0, 90, 8], [0, 130, 8], [0, 250, 8]],
  "widths": [40, 40, 40, 120, 120], "heights": [45, 45, 45, 70, 70], "wallMaterial": "caveRock", "floorMaterial": "caveFloor",
  "worldUnitsPerRepeat": 48, "breakup": {"featureSize": 40, "amplitude": 5, "seed": 11},
}
intoTheRoom = [[0, -60, 2], [0, 30, 6], [0, 90, 8], [0, 190, 8]]
# The grid's open edge: 60 by 70 cells.
borderEdges = 2 * (60 + 70)
# What the cave leaves in the mesh, read straight from its attributes and record: edges on three or more faces, open edges, plug ids,
# how far any ring vertex stands off its plug triangle in the mesh or any pass, and how far any pass moves the lining.
checkCave = r"""
import json, numpy
import bridgePasses
ground = bpy.data.objects['ground']
mesh = ground.data

def values(name, width=1, kind=numpy.int32):
  attribute = mesh.attributes[name]
  data = numpy.empty(len(attribute.data) * width, dtype=kind)
  attribute.data.foreach_get('vector' if width == 3 else 'value', data)
  return data.reshape(-1, width) if width > 1 else data

loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
mesh.loops.foreach_get('edge_index', loopEdges)
uses = numpy.bincount(loopEdges, minlength=len(mesh.edges))
tags = values('zonewrightCaveVertex:hall')
record = json.loads(ground['zonewrightCaves'])['hall']
identifiers = tags[tags > 0]
byIdentifier = {int(identifier): int(row) for row, identifier in zip(numpy.flatnonzero(tags > 0), identifiers)}
ring = numpy.flatnonzero(tags == -1)
weights = values('zonewrightCaveWeights:hall', 3, numpy.float64)[ring]
corners = numpy.array([[byIdentifier[identifier] for identifier in record['plug'][source]['vertices']] for source in values('zonewrightCaveSource:hall')[ring]])
coordinates = numpy.empty(len(mesh.vertices) * 3)
mesh.vertices.foreach_get('co', coordinates)
blocks = [coordinates.reshape(-1, 3)] + [bridgePasses.keyCoordinates(key) for key in mesh.shape_keys.key_blocks]
lining = tags == -2
result = {
  'edgesOnThreeOrMoreFaces': int((uses > 2).sum()), 'openEdges': int((uses == 1).sum()), 'plugIDs': len(identifiers), 'distinctPlugIDs': len(set(identifiers.tolist())),
  'largestRingMiss': max(float(numpy.abs(numpy.einsum('rk,rkj->rj', weights, block[corners]) - block[ring]).max()) for block in blocks),
  'largestLiningMove': max(float(numpy.abs(block[lining] - blocks[0][lining]).max()) for block in blocks), 'ringVertices': len(ring), 'liningVertices': int(lining.sum()),
}
"""
readSurfacing = r"""
import json, numpy
import bridgeMeshAccess
ground = bpy.data.objects['ground']
mesh = ground.data
faceTags = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
mesh.attributes['zonewrightCaveFace:hall'].data.foreach_get('value', faceTags)
_, normals, materials = bridgeMeshAccess.readFaceArrays(ground)
names = [slot.material.name for slot in ground.material_slots]
areas = bridgeMeshAccess.textureAreas(ground)
loopTriangleFaces = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
mesh.loop_triangles.foreach_get('polygon_index', loopTriangleFaces)
record = json.loads(ground['zonewrightCaves'])['hall']
lining = numpy.flatnonzero(faceTags == -1)
pieces = numpy.flatnonzero(faceTags > 0)
result = {
  'floor': sorted({names[materials[face]] for face in lining if normals[face][2] > 0.7}), 'walls': sorted({names[materials[face]] for face in lining if normals[face][2] <= 0.7}),
  'floorFaces': int(sum(normals[face][2] > 0.7 for face in lining)), 'wallFaces': int(sum(normals[face][2] <= 0.7 for face in lining)),
  'smallestLiningUVArea': float(areas.uv[numpy.isin(loopTriangleFaces, lining)].min()),
  'piecesOffTheirSource': int(sum(materials[face] != record['plug'][faceTags[face] - 1]['material'] for face in pieces)), 'pieces': len(pieces),
}
"""
readLiningMaterials = r"""
import numpy
import bridgeMeshAccess
ground = bpy.data.objects['ground']
lining = bridgeMeshAccess.evaluateSelector({'cave': 'hall'}, ground, 'faces')
_, normals, materials = bridgeMeshAccess.readFaceArrays(ground)
names = [slot.material.name for slot in ground.material_slots]
result = {
  'floor': sorted({names[material] for material, normal in zip(materials[lining], normals[lining]) if normal[2] > 0.7}),
  'walls': sorted({names[material] for material, normal in zip(materials[lining], normals[lining]) if normal[2] <= 0.7}),
}
"""
readLining = r"""
import bridgeMeshAccess
ground = bpy.data.objects['ground']
mask = bridgeMeshAccess.evaluateSelector({'cave': 'hall'}, ground, 'vertices')
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
result = shown[mask].tolist()
"""
readShown = r"""
import bridgeMeshAccess
result = {name: bridgeMeshAccess.readVertexArrays(bpy.data.objects[name])[0].tolist() for name in ('ground', 'control')}
"""
# A gallery up the fixture's cliff: 40 wide and 30 tall, four fifths of its width in the rock, rising from 2 to 80.
gallery = {"objectName": "ground", "start": [-120, -45], "end": [120, 10], "floorFrom": 2, "floorTo": 80, "width": 40, "height": 30, "side": "left", "insideShare": 0.8, "step": 20}
# The share of each point's width inside the uncut rock, a step over its floor across the line from start to end, at 201 points.
measureShares = r"""
import numpy, mathutils, mathutils.bvhtree
import bridgeMeshAccess
ground = bpy.data.objects['ground']
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
tree = mathutils.bvhtree.BVHTree.FromPolygons(shown.tolist(), bridgeMeshAccess.meshTriangles(ground).tolist())

def inside(point):
  location, normal, _, _ = tree.ray_cast(mathutils.Vector(point), mathutils.Vector((0, 0, 1)))
  return location is not None and normal.z > 0

result = [float(numpy.mean([inside(numpy.array(point) + [0, 0, 2] + share * width * numpy.array(toSide)) for share in numpy.linspace(-0.5, 0.5, 201)])) for point in path]
"""
# The cut floor's height 6 out from and 12 into the rock from each inner point of the path, across its bend's middle.
measureFloorAcross = r"""
import numpy, mathutils
depsgraph = bpy.context.evaluated_depsgraph_get()

def floorUnder(x, y, z):
  hit, location, _, _, _, _ = bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((x, y, z)), mathutils.Vector((0, 0, -1)))
  return location.z if hit else None

result = []
for before, point, after in zip(path, path[1:], path[2:]):
  incoming, outgoing = numpy.subtract(point[:2], before[:2]), numpy.subtract(after[:2], point[:2])
  along = incoming / numpy.linalg.norm(incoming) + outgoing / numpy.linalg.norm(outgoing)
  along /= numpy.linalg.norm(along)
  toRock = numpy.array([-along[1], along[0]])
  result.append([floorUnder(*(numpy.array(point[:2]) + offset * toRock), point[2] + 5) for offset in (-6, 12)])
"""


async def caveCanyon(session, tmp_path):
  """caveCanyon reaching far enough north to hold a room and its reach: a 480 x 560 grid of 8-unit cells, a cliff 120 high at about 80
  degrees along y = 0 with the river side to the south, warp and roughen over it, grass and rock on slopes of 40 or more, and the
  approach to the mouth graded level."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  for name, color in (("grass", (90, 120, 60, 255)), ("rock", (110, 105, 100, 255)), ("caveRock", (80, 76, 72, 255)), ("caveFloor", (120, 100, 80, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [480, 560], "spacing": 8, "location": [0, 120, 0], "collection": "terrain"})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "cliff"})
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[-300, 0], [300, 0], [300, 500], [-300, 500]], "base": 0, "profile": [[-21, 0], [0, 120], [500, 120]]})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "breakup"})
  await session.expectSuccess("warp", {"objectName": "ground", "featureSize": 80, "amplitude": 12, "seed": 4, "plane": "horizontal"})
  await session.expectSuccess("roughen", {"objectName": "ground", "featureSize": 20, "amplitude": 2.5, "octaves": 3, "roughness": 0.5, "seed": 5, "direction": "normal"})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "rock"})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "rock", "material": "rock", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
  await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "approach", "points": [[0, -140, 2], [0, -50, 2]], "width": 56})


def testACaveIsCutSealedWalkableAndSurfacedAndItsLiningStrokesComeBack(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", hall)
    checked = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    walk = await session.expectSuccess("walkRoute", {"path": intoTheRoom, "sampleSpacing": 4})
    surfacing = (await session.expectSuccess("runPython", {"code": readSurfacing}))["result"]
    await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "moss"})
    everywhere = await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "moss", "material": "grass", "selector": {"slope": {"minimumDegrees": 0, "maximumDegrees": 20}}})
    caveFloor = await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "moss", "material": "rock", "selector": {"and": [{"cave": "hall"}, {"slope": {"minimumDegrees": 0, "maximumDegrees": 20}}]}})
    recut = await session.expectSuccess("editCave", {"objectName": "ground", "name": "hall", "changes": {"heights": [45, 45, 45, 66, 66]}})
    repainted = (await session.expectSuccess("runPython", {"code": readLiningMaterials}))["result"]
    return cut, checked, walk, surfacing, everywhere, caveFloor, recut, repainted

  cut, checked, walk, surfacing, everywhere, caveFloor, recut, repainted = stageBlenderServer.session(steps)
  # Watertight: no edge on three or more faces and no open edge but the grid's border; the ring follows its plug triangles in the
  # mesh and every pass, the lining stands still in all of them, and every plug id is carried once.
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges
  assert checked["ringVertices"] > 0 and checked["largestRingMiss"] <= 1e-4 and checked["largestLiningMove"] == 0.0
  assert checked["plugIDs"] == checked["distinctPlugIDs"] == cut["plugVertices"]
  assert [(end["end"], end["kind"]) for end in cut["ends"]] == [("start", "open"), ("end", "blind")] and len(cut["openings"]) == 1
  assert [(stretch["points"], stretch["width"], stretch["length"], stretch["floor"]) for stretch in cut["levelStretches"]] == [([2, 3], 40.0, 40.0, 8.0), ([3, 4], 120.0, 120.0, 8.0)]
  # Walked from the mouth to the middle of the room: headroom no lower than the tunnel's height less the breakup and a unit, and no
  # face steeper than the floor's maximum.
  assert walk["walkable"] is True and walk["problems"] == []
  assert walk["lowestHeadroom"]["headroom"] >= 45 - 5 - 1 and walk["steepest"]["slopeDegrees"] <= 30
  # Floor faces carry the floor's material and the rest the wall's; every lining triangle is mapped; the ground the cut split keeps
  # its own material.
  assert surfacing["floor"] == ["caveFloor"] and surfacing["walls"] == ["caveRock"] and surfacing["floorFaces"] > 0 and surfacing["wallFaces"] > 0
  assert surfacing["smallestLiningUVArea"] > 0 and surfacing["pieces"] > 0 and surfacing["piecesOffTheirSource"] == 0
  # A stroke that does not name the cave leaves its lining alone; one that names it paints it, and is painted again on the next cut.
  assert everywhere["painted"] > 0 and caveFloor["painted"] > 0
  assert recut["cut"]["liningFaces"] > 0 and repainted == {"floor": ["rock"], "walls": ["caveRock"]}


def testFineCutsCloseWhereTheTubesEndStandsOnTheGround(stageBlenderServer, tmp_path):
  # Two faces meeting along a line from x 0 to 10, each with a corner of its own on it (the upper at 7, the lower at 4), and between them
  # the slit the exact boolean leaves where a tube's flat end stands on the ground: its outline back along the line by the upper face's
  # corners and out again by the lower's.
  slit = r"""
import numpy
import bridgeCaves
positions = numpy.array([[0, 0, 0], [10, 0, 0], [10, 5, 0], [0, 5, 0], [4, 0, 0], [10, -5, 0], [0, -5, 0], [7, 0, 0]], dtype=float)
faces = [[0, 7, 1, 2, 3], [6, 5, 1, 4, 0], [1, 7, 0, 4]]
kept, sources, _ = bridgeCaves.withoutSlivers(positions, faces, [0, 1, -2], [[0, 0, 1]] * 3)
result = {'faces': kept, 'sources': sources}
"""
  fine = hall | {"path": [[0, -60, 2], [0, 10, 2], [0, 60, 2], [0, 90, 2], [0, 250, 2]], "breakup": None}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    zipped = (await session.expectSuccess("runPython", {"code": slit}))["result"]
    checks = []
    for edgeLength, breakup in ((8, None), (8, {"featureSize": 40, "amplitude": 3, "seed": 11}), (12, None), (16, None), (16, None)):
      await session.expectSuccess("cutCave", fine | {"edgeLength": edgeLength, "breakup": breakup})
      checks.append((await session.expectSuccess("runPython", {"code": checkCave}))["result"])
      await session.expectSuccess("removeCave", {"objectName": "ground", "name": "hall"})
    return zipped, checks

  zipped, checks = stageBlenderServer.session(steps)
  # The slit goes, and each face takes the other's corner on the line, so the two meet edge to edge.
  assert zipped == {"faces": [[0, 4, 7, 1, 2, 3], [6, 5, 1, 7, 4, 0]], "sources": [0, 1]}
  # At every edgeLength, and cut again after being taken back, the cut is sealed where the tube's end stands on the ground in front of
  # the mouth.
  for checked in checks:
    assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges


def testPiecesOfGroundAtTheMouthStayInTheirTrianglesPlanes(stageBlenderServer, tmp_path):
  # Each piece of ground the cut left at the mouth against the plane of the ground triangle it is a piece of: the angle between them.
  readTilts = r"""
import numpy
import bridgeCaveData, bridgeMeshAccess
ground = bpy.data.objects['ground']
mesh = ground.data
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
tags = bridgeCaveData.faceTags(ground, 'hall')
vertexTags = bridgeCaveData.attributeValues(mesh, 'zonewrightCaveVertex:hall')
record = bridgeCaveData.caves(ground)['hall']
byIdentifier = {int(tag): index for index, tag in enumerate(vertexTags.tolist()) if tag > 0}
tilts = []
for polygon in mesh.polygons:
  tag = int(tags[polygon.index])
  if tag <= 0:
    continue
  corners = shown[list(polygon.vertices)]
  normal = numpy.cross(corners[1] - corners[0], corners[2] - corners[0])
  plug = shown[[byIdentifier[identifier] for identifier in record['plug'][tag - 1]['vertices']]]
  plugNormal = numpy.cross(plug[1] - plug[0], plug[2] - plug[0])
  tilts.append(float(numpy.degrees(numpy.arccos(numpy.clip(abs(normal @ plugNormal) / (numpy.linalg.norm(normal) * numpy.linalg.norm(plugNormal)), 0, 1)))))
result = {'pieces': len(tilts), 'worstTilt': max(tilts)}
"""

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    return (await session.expectSuccess("runPython", {"code": readTilts}))["result"]

  tilts = stageBlenderServer.session(steps)
  # The welds at the seam move no piece out of its triangle's plane, so none stands out of the cliff as a blade.
  assert tilts["pieces"] > 20 and tilts["worstTilt"] <= 0.5


def testLiningWallsAreMappedRoundTheirBendsWithoutSeams(stageBlenderServer, tmp_path):
  # A tunnel into the cliff turning 90 degrees east at y 100 on an arc of radius 40; its walls and vault in the bend, away from its ends.
  bent = hall | {"path": [[0, -60, 2], [0, 10, 2], [0, 140, 2], [120, 140, 2]], "widths": [40] * 4, "heights": [45] * 4, "breakup": None}
  readMapping = r"""
import numpy
import bridgeCaveData, bridgeMeshAccess, bridgeSurfacing
ground = bpy.data.objects['ground']
mesh = ground.data
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
tags = bridgeCaveData.faceTags(ground, 'hall')
uvs = numpy.empty(len(mesh.loops) * 2)
mesh.uv_layers[bridgeSurfacing.uvLayerName].data.foreach_get('uv', uvs)
uvs = uvs.reshape(-1, 2)
byVertex, stretch = {}, []
for polygon in mesh.polygons:
  corners = shown[list(polygon.vertices)]
  middle = corners.mean(axis=0)
  e1, e2 = corners[1] - corners[0], corners[2] - corners[0]
  normal = numpy.cross(e1, e2)
  if tags[polygon.index] != -1 or normal[2] > 0.7 * numpy.linalg.norm(normal) or not (60 < middle[1] < 180 and middle[0] < 80) or middle[2] < 3:
    continue
  texture = uvs[list(polygon.loop_indices)]
  axisU = e1 / numpy.linalg.norm(e1)
  axisV = numpy.cross(normal / numpy.linalg.norm(normal), axisU)
  world = numpy.array([[e1 @ axisU, e2 @ axisU], [e1 @ axisV, e2 @ axisV]])
  singular = numpy.linalg.svd(numpy.column_stack([texture[1] - texture[0], texture[2] - texture[0]]) @ numpy.linalg.inv(world), compute_uv=False)
  stretch.append([float(singular.max() / singular.min()), bool(middle[1] > 100 - 16 and middle[0] < 40 + 16)])
  for vertex, uv in zip(polygon.vertices, texture.tolist()):
    byVertex.setdefault(vertex, []).append(uv)
spreads = [float(numpy.ptp(numpy.array(values), axis=0).max()) for values in byVertex.values() if len(values) > 1]
result = {'faces': len(stretch), 'worstInTheBend': max(value for value, inBend in stretch if inBend), 'worstOnTheStraights': max(value for value, inBend in stretch if not inBend), 'worstSeam': max(spreads)}
"""

  # Every wall and vault face of the hall, whose tunnel flares out into its room between y 90 and 130: how far its mapping draws its
  # texture out at worst.
  readFlare = readMapping.replace("not (60 < middle[1] < 180 and middle[0] < 80)", "False").replace("result = {", "result = {'worst': max(value for value, _ in stretch)} or {")

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", bent)
    mapping = (await session.expectSuccess("runPython", {"code": readMapping}))["result"]
    await session.expectSuccess("removeCave", {"objectName": "ground", "name": "hall"})
    await session.expectSuccess("cutCave", hall)
    flare = (await session.expectSuccess("runPython", {"code": readFlare}))["result"]
    return mapping, flare

  mapping, flare = stageBlenderServer.session(steps)
  # Round the bend the walls and vault carry one mapping, every corner the same place in it from each face that meets there. On the
  # straights either side (a row clear of it) a texel is square; in the bend, its walls on radii 20 and 60, each takes the texture
  # sqrt(3) closer or wider along it than up it (mapped as at the radius between them), inside the export's limit of 2.
  assert mapping["faces"] > 50 and mapping["worstSeam"] <= 1e-4
  assert mapping["worstOnTheStraights"] <= 1.05 and 1.6 <= mapping["worstInTheBend"] <= 1.8
  # Where the tunnel flares into the room its faces look along the run, which the tube's own mapping would draw out; they are mapped
  # from the side instead, so no face of the lining is drawn out past the export's limit.
  assert flare["worst"] <= 2.0


def testTakingACaveBackLeavesTheGroundAsAnUncutCopyGivenTheSameChange(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    copies = await session.expectSuccess("duplicateObjects", {"names": ["ground"], "offset": [0, 0, 0]})
    await session.expectSuccess("organize", {"renames": {copies["ground"]: "control"}})
    await session.expectSuccess("runPython", {"code": "bpy.data.objects['control'].hide_render = True"})
    await session.expectSuccess("cutCave", hall)
    for name in ("ground", "control"):
      await session.expectSuccess("setShapingPass", {"objectName": name, "name": "cliff", "strength": 1.15})
    removed = await session.expectSuccess("removeCave", {"objectName": "ground", "name": "hall"})
    shown = (await session.expectSuccess("runPython", {"code": readShown}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return removed, shown, detail

  removed, shown, detail = stageBlenderServer.session(steps)
  ground, control = numpy.array(shown["ground"]), numpy.array(shown["control"])
  assert ground.shape == control.shape and numpy.abs(ground - control).max() <= 1e-5
  assert removed["definition"]["path"] == hall["path"] and removed["definition"]["widths"] == hall["widths"] and removed["restoredFaces"] > 0
  assert detail["caves"] == []


def testAStaleCaveIsFlaggedAndRefittedWhileTheGuardsHoldAroundIt(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    fresh = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mouthEdit"})
    nudged = await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [0, -30, 30], "radius": 30, "strength": 5, "direction": [0, -1, 0]})
    stale = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    checkedStale = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "cavecheck.blend")})
    game = await session.expectSuccess("checkExport", {"path": str(tmp_path / "cavecheck.eqg"), "purpose": "game"})
    test = await session.expectSuccess("checkExport", {"path": str(tmp_path / "cavecheck.eqg"), "purpose": "test"})
    refitted = await session.expectSuccess("editCave", {"objectName": "ground", "name": "hall"})
    fitted = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    liningBefore = (await session.expectSuccess("runPython", {"code": readLining}))["result"]
    carved = await session.expectSuccess("sculptAlongPath", {"objectName": "ground", "mode": "carve", "path": [[-40, 190, 60], [40, 190, 60]], "radius": 30, "strength": 1, "profile": [[0, 0], [1, 30]]})
    liningAfter = (await session.expectSuccess("runPython", {"code": readLining}))["result"]
    checkedCarved = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    contours = await session.expectError("cutContours", {"objectName": "ground", "levels": [30]})
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    regradedDetail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectSuccess("collapseShapingPasses", {"objectName": "ground"})
    deleted = await session.expectError("deleteFaces", {"objectName": "ground", "selector": {"box": {"minimum": [-200, 300, -50], "maximum": [-150, 350, 200]}}})
    return fresh, nudged, stale, checkedStale, game, test, refitted, fitted, liningBefore, carved, liningAfter, checkedCarved, contours, regraded, regradedDetail, deleted

  fresh, nudged, stale, checkedStale, game, test, refitted, fitted, liningBefore, carved, liningAfter, checkedCarved, contours, regraded, regradedDetail, deleted = stageBlenderServer.session(steps)
  assert [(cave["name"], cave["stale"]) for cave in fresh["caves"]] == [("hall", False)]
  # A sculpt at the mouth moves the ground under the cave: it is flagged stale, and its ring and lining still hold.
  assert nudged["caveLiningLeft"] > 0 and [(cave["name"], cave["stale"]) for cave in stale["caves"]] == [("hall", True)]
  assert checkedStale["edgesOnThreeOrMoreFaces"] == 0 and checkedStale["openEdges"] == borderEdges and checkedStale["largestRingMiss"] <= 1e-4 and checkedStale["largestLiningMove"] == 0.0
  # Stale, it is listed to confirm and a game export refuses it; refitted, it is not stale.
  assert [entry["staleCaves"] for entry in test["toConfirm"]] == [["hall"]]
  assert [failure["staleCaves"] for failure in game["failures"] if failure["failure"] == "stale"] == [["hall"]]
  assert refitted["restored"]["restoredFaces"] > 0 and [cave["stale"] for cave in fitted["caves"]] == [False]
  # A carve over the hill leaves every lining vertex exactly where it was, and says how many it left.
  assert carved["caveLiningLeft"] > 0 and liningAfter == liningBefore
  assert checkedCarved["largestRingMiss"] <= 1e-4 and checkedCarved["largestLiningMove"] == 0.0
  # Contour cuts at the cave are refused, naming it; a regrade cuts the cave again to fit the carved hill.
  assert "cave(s) ['hall']" in contours and "Nothing was changed" in contours
  assert [entry["cave"] for entry in regraded["refittedCaves"]] == ["hall"] and [cave["stale"] for cave in regradedDetail["caves"]] == [False]
  # With its passes collapsed, a face edit is still refused, naming the cave and the way to make the change.
  assert "holds caves ['hall']" in deleted and "removeCave" in deleted and "cutCave" in deleted


def testCaveRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    # Its floor sunk 8 into the graded approach in front of the cliff, with its walls and vault in the open.
    partInRock = await session.expectError("cutCave", hall | {"name": "partInRock", "path": [[0, -60, -6]] + hall["path"][1:]})
    # Its floor starting 10 over the graded approach, falling to the tunnel.
    hanging = await session.expectError("cutCave", hall | {"name": "hanging", "path": [[0, -60, 12]] + hall["path"][1:]})
    steep = await session.expectError("cutCave", hall | {"name": "steep", "path": [[0, -60, 2], [0, 0, 60]], "widths": [40] * 2, "heights": [45] * 2})
    tight = await session.expectError("cutCave", hall | {"name": "tight", "path": [[0, -60, 2], [0, 40, 6], [-30, 10, 6]], "widths": [40] * 3, "heights": [45] * 3})
    border = await session.expectError("cutCave", hall | {"name": "border", "path": [[0, -60, 2], [0, 30, 6], [0, 370, 6]], "widths": [40] * 3, "heights": [45] * 3})
    await session.expectSuccess("cutCave", hall)
    before = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    # A tunnel 45 east of the hall, whose reach takes in the hall's mouth.
    overlap = await session.expectError("cutCave", hall | {"name": "beside", "path": [[45, -60, 2], [45, 30, 6], [45, 150, 8]], "widths": [40] * 3, "heights": [45] * 3})
    edited = await session.expectError("editCave", {"objectName": "ground", "name": "hall", "changes": {"path": [[0, -60, 2], [0, 0, 60], [0, 90, 8], [0, 130, 8], [0, 250, 8]]}})
    after = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return partInRock, hanging, steep, tight, border, overlap, edited, before, after, detail

  partInRock, hanging, steep, tight, border, overlap, edited, before, after, detail = stageBlenderServer.session(steps)
  assert "The cave's start at [0.0, -60.0, -6.0] is part in the rock (up to 8.0 into it)" in partInRock
  assert "The cave's floor hangs in the air from [0.0, -60.0, 12.0]" in hanging and "the ground up to 10.0 below it" in hanging
  assert "rises 58.0 from point 0 to point 1 over a run of 60.0" in steep and "needs a run of 100.5" in steep
  assert "tighter than half its width" in tight
  assert "reaches the edge of 'ground'" in border
  assert "Cave 'beside' would reach the mouth of cave(s) ['hall']" in overlap
  # A refused edit leaves the cave as it was.
  assert "rises 58.0" in edited and after == before and detail["caves"][0]["from"] == hall["path"][0]


def testATracedGalleryIsEvenlyGradedInsideTheRockByItsShareLevelAcrossAndWalkedUnderCover(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    traced = await session.expectSuccess("traceLedge", gallery)
    direction = numpy.subtract(gallery["end"], gallery["start"]) / numpy.linalg.norm(numpy.subtract(gallery["end"], gallery["start"]))
    toSide = [float(-direction[1]), float(direction[0]), 0.0]
    shares = (await session.expectSuccess("runPython", {"code": f"path = {traced['path']!r}\ntoSide = {toSide!r}\nwidth = 40\n" + measureShares}))["result"]
    await session.expectSuccess("cutCave", {
      "objectName": "ground", "name": "gallery", "path": traced["path"], "widths": traced["widths"], "heights": traced["heights"],
      "wallMaterial": "caveRock", "floorMaterial": "caveFloor", "worldUnitsPerRepeat": 48, "breakup": hall["breakup"],
    })
    across = (await session.expectSuccess("runPython", {"code": f"path = {traced['path']!r}\n" + measureFloorAcross}))["result"]
    walk = await session.expectSuccess("walkRoute", {"path": traced["path"], "sampleSpacing": 4})
    return traced, shares, across, walk

  traced, shares, across, walk = stageBlenderServer.session(steps)
  path = numpy.array(traced["path"])
  runs = numpy.linalg.norm(numpy.diff(path[:, :2], axis=0), axis=1)
  # The floor rises evenly from floorFrom to floorTo along the traced path: every segment's grade, as returned and as the path holds it.
  evenGrade = numpy.degrees(numpy.arctan2(80 - 2, runs.sum()))
  assert path[0, 2] == 2 and path[-1, 2] == 80
  assert all(abs(segment["gradeDegrees"] - evenGrade) <= 0.1 for segment in traced["segments"])
  assert numpy.abs(numpy.degrees(numpy.arctan2(numpy.diff(path[:, 2]), runs)) - evenGrade).max() <= 0.1
  # Every point found the cliff, four fifths of its width inside the uncut rock as measured here.
  assert all(point["traced"] for point in traced["points"]) and len(shares) == len(path)
  assert max(abs(share - 0.8) for share in shares) <= 0.05
  # Cut, its floor is level across at each inner point: no more than 2 degrees between 6 out and 12 in from the middle.
  assert all(outer is not None and inner is not None for outer, inner in across)
  assert max(numpy.degrees(numpy.arctan2(abs(outer - inner), 18)) for outer, inner in across) <= 2
  # Walked from foot to top, under rock for at least half the way.
  assert walk["walkable"] is True and walk["problems"] == []
  covered = [row["headroom"] is not None for row in walk["profile"]]
  assert 2 * sum(covered) >= len(covered)


def testUnderRockPlotsAndDroppedThingsFindTheCaveFloorNotTheHill(stageBlenderServer, tmp_path):
  housing = {"role": "featured", "intent": "plots in caves", "placement": "world", "plotBudget": {"player": 2, "guild": 0}}
  plot = {"address": "1 Hall", "center": [0, 170], "facingDegrees": 180, "size": [80, 80]}
  readColumn = r"""
import mathutils
depsgraph = bpy.context.evaluated_depsgraph_get()
up, down = mathutils.Vector((0, 0, 1)), mathutils.Vector((0, 0, -1))
result = {
  'ceiling': bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((0, 170, 10)), up)[1].z,
  'hill': bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((0, 170, 1000)), down)[1].z,
}
"""
  readPebbles = "result = sorted(round(pebble.location.z, 2) for pebble in bpy.data.collections['pebbles'].objects)"

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("setZoneHousing", housing)
    unseated = await session.expectError("placePlot", plot)
    await session.expectSuccess("placePlot", plot | {"height": 8})
    assessed = await session.expectSuccess("assessPlot", {"address": "1 Hall"})
    column = (await session.expectSuccess("runPython", {"code": readColumn}))["result"]
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "boulder", "size": [4, 4, 4], "location": [30, 220, 28]})
    settled = await session.expectSuccess("settleObjects", {"names": ["boulder"]})
    await session.expectSuccess("createPrimitive", {"kind": "cube", "name": "pebble", "size": [1, 1, 1], "location": [150, -120, 0]})
    scatter = {"sourceObject": "pebble", "region": {"circle": {"center": [-20, 230], "radius": 15}}, "density": 100, "minimumSpacing": 2, "collection": "pebbles"}
    unscattered = await session.expectError("scatterInRegion", scatter)
    scattered = await session.expectSuccess("scatterInRegion", scatter | {"castFromHeight": 40})
    pebbles = (await session.expectSuccess("runPython", {"code": readPebbles}))["result"]
    return unseated, assessed, column, settled, unscattered, scattered, pebbles

  unseated, assessed, column, settled, unscattered, scattered, pebbles = stageBlenderServer.session(steps)
  # Under the hill stands the room: placing a plot without its height is refused, naming the hill's top, the rock's underside, and the floor.
  named = re.search(r"at (-?\d+\.\d), is the top of rock whose underside is at (-?\d+\.\d), over ground at (-?\d+\.\d)", unseated)
  assert named is not None and numpy.allclose([float(value) for value in named.groups()], [column["hill"], column["ceiling"], 8.0], atol=0.051)
  # Placed on the floor, it is measured there: its ground is the floor and the rock over it is the room's ceiling.
  assert abs(assessed["under"]["lowest"] - 8) <= 1 and abs(assessed["under"]["highest"] - 8) <= 1 and abs(assessed["overhead"] - column["ceiling"]) <= 1
  # A boulder let go 20 over the floor lands on it, not on the hill.
  assert settled["settled"][0]["spans"][0] == 8.0 and settled["settled"][0]["under"] == [8.0, 8.0]
  # Scattering over the room needs to be told which ground; told to drop from under the ceiling, every pebble lands on the floor.
  assert "rock lies over ground" in unscattered and "give castFromHeight" in unscattered
  assert scattered["placed"] == scattered["targetCount"] > 0 and pebbles == [8.0] * scattered["placed"]


def testACaveExportsAndIsWalkedAfterImport(stageBlenderServer, tmp_path):
  archivePath = tmp_path / "cavetest.eqg"

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("saveFile", {"path": str(tmp_path / "cavetest.blend")})
    exported = await session.expectSuccess("exportZone", {"path": str(archivePath), "purpose": "test"})
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    await session.expectSuccess("importZoneFile", {"path": str(archivePath)})
    imported = await session.expectSuccess("getObjectDetail", {"name": "cavetest"})
    walk = await session.expectSuccess("walkRoute", {"path": intoTheRoom, "sampleSpacing": 4})
    return exported, imported, walk

  exported, imported, walk = stageBlenderServer.session(steps)
  assert imported["triangles"] == exported["terrainTriangles"]
  assert walk["walkable"] is True and walk["problems"] == [] and walk["lowestHeadroom"]["headroom"] >= 45 - 5 - 1
