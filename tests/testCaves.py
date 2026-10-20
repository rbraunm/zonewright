import json
import math
import re
import sys
from pathlib import Path

import numpy

from conftest import writePNG, walkOf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import playerScale

# The medium body group, which holds the walked human male, is the one these tests size their courses by.
medium = playerScale.groups["medium"]
steepestWalkableDegrees = medium.steepestWalkableDegrees
# A whole degree past the steepest face players walk, which a grade limit may not reach.
pastWalkableDegrees = math.floor(steepestWalkableDegrees) + 1

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
# Which vertices of ground and control the approach's pass moves.
readApproach = r"""
import numpy, bridgePasses
result = {}
for name in ('ground', 'control'):
  keys = bpy.data.objects[name].data.shape_keys
  moved = bridgePasses.keyCoordinates(keys.key_blocks['route approach']) - bridgePasses.keyCoordinates(keys.reference_key)
  result[name] = (numpy.abs(moved).max(axis=1) > 1e-9).tolist()
"""
# A gallery up the fixture's cliff: 40 wide and 30 tall, four fifths of its width in the rock, rising from 2 to 80.
gallery = {"objectName": "ground", "start": [-120, -45], "end": [120, 10], "floorFrom": 2, "floorTo": 80, "width": 40, "height": 30, "side": "left", "insideShare": 0.8, "step": 20}
# The share of each point's width inside the uncut rock, probeOverFloor over its floor across the line from start to end, at 201 points.
measureShares = r"""
import numpy, mathutils, mathutils.bvhtree
import bridgeMeshAccess
from buildTolerances import probeOverFloor
ground = bpy.data.objects['ground']
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
tree = mathutils.bvhtree.BVHTree.FromPolygons(shown.tolist(), bridgeMeshAccess.meshTriangles(ground).tolist())

def inside(point):
  location, normal, _, _ = tree.ray_cast(mathutils.Vector(point), mathutils.Vector((0, 0, 1)))
  return location is not None and normal.z > 0

result = [float(numpy.mean([inside(numpy.array(point) + [0, 0, probeOverFloor] + share * width * numpy.array(toSide)) for share in numpy.linspace(-0.5, 0.5, 201)])) for point in path]
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


async def caveCanyon(session, tmp_path, east=0):
  """caveCanyon reaching far enough north to hold a room and its reach: a 480 x 560 grid of 8-unit cells, a cliff 120 high at about 80
  degrees along y = 0 with the river side to the south, warp and roughen over it, grass and rock on slopes of 40 or more, and the
  approach to the mouth graded level. Moved east, the grid's own coordinates lie that far out, as an imported zone's do."""
  await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
  for name, color in (("grass", (90, 120, 60, 255)), ("rock", (110, 105, 100, 255)), ("caveRock", (80, 76, 72, 255)), ("caveFloor", (120, 100, 80, 255))):
    await session.expectSuccess("createMaterial", {"name": name, "diffuseTexture": str(writePNG(tmp_path / f"{name}.png", 4, 4, color))})
  await session.expectSuccess("createTerrainGrid", {"name": "ground", "size": [480, 560], "spacing": 8, "location": [east, 120, 0], "collection": "terrain"})
  if east:
    await session.expectSuccess("runPython", {"code": "ground = bpy.data.objects['ground']\nground.data.transform(ground.matrix_basis)\nground.matrix_basis.identity()"})
  await session.expectSuccess("assignMaterial", {"objectName": "ground", "materialName": "grass"})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "cliff"})
  await session.expectSuccess("sculptOutline", {"objectName": "ground", "mode": "fill", "outline": [[east - 300, 0], [east + 300, 0], [east + 300, 500], [east - 300, 500]], "base": 0, "profile": [[-21, 0], [0, 120], [500, 120]]})
  await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "breakup"})
  await session.expectSuccess("warp", {"objectName": "ground", "featureSize": 80, "amplitude": 12, "seed": 4, "plane": "horizontal"})
  await session.expectSuccess("roughen", {"objectName": "ground", "featureSize": 20, "amplitude": 2.5, "octaves": 3, "roughness": 0.5, "seed": 5, "direction": "normal"})
  await session.expectSuccess("addSurfaceLayer", {"objectName": "ground", "name": "rock"})
  await session.expectSuccess("paintSurface", {"objectName": "ground", "layer": "rock", "material": "rock", "selector": {"slope": {"minimumDegrees": 40, "maximumDegrees": 180}}})
  await session.expectSuccess("gradeRoute", {"objectName": "ground", "name": "approach", "points": [[east, -140, 2], [east, -50, 2]], "width": 56})


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
  assert walkOf(walk)["walkable"] is True and walkOf(walk)["problems"] == []
  assert walkOf(walk)["lowestHeadroom"]["headroom"] >= 45 - 5 - 1 and walkOf(walk)["steepest"]["slopeDegrees"] <= 30
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
  # The same turned 8 degrees and moved out to [2600, 2600, 50], its corners where single precision keeps them there, up to 1.8e-4 off
  # their line; and two copies of one new point a single precision spacing (2.4e-4) apart there, which are one point.
  farSlit = r"""
import numpy
import bridgeCaves
turn = numpy.radians(8)
turned = numpy.array([[numpy.cos(turn), -numpy.sin(turn), 0], [numpy.sin(turn), numpy.cos(turn), 0], [0, 0, 1]])
positions = numpy.array([[0, 0, 0], [10, 0, 0], [10, 5, 0], [0, 5, 0], [4, 0, 0], [10, -5, 0], [0, -5, 0], [7, 0, 0]], dtype=float) @ turned.T + [2600, 2600, 50]
positions = positions.astype(numpy.float32).astype(float)
faces = [[0, 7, 1, 2, 3], [6, 5, 1, 4, 0], [1, 7, 0, 4]]
kept, sources, _ = bridgeCaves.withoutSlivers(positions, faces, [0, 1, -2], [[0, 0, 1]] * 3)
copies = numpy.array([[2600, 2600, 50], [2610, 2600, 50], [2600, 2610, 50], [float(numpy.nextafter(numpy.float32(2600), numpy.float32(4096))), 2600, 50]])
snapped, _, _ = bridgeCaves.snappedCut(copies, [[0, 1, 2], [3, 2, 1]], [-2, -2], [[0, 0, 1]] * 2, numpy.full(4, -1))
result = {'faces': kept, 'sources': sources, 'snapped': snapped}
"""
  fine = hall | {"path": [[0, -60, 2], [0, 10, 2], [0, 60, 2], [0, 90, 2], [0, 250, 2]], "breakup": None}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    zipped = (await session.expectSuccess("runPython", {"code": slit}))["result"]
    zippedFar = (await session.expectSuccess("runPython", {"code": farSlit}))["result"]
    checks = []
    for edgeLength, breakup in ((8, None), (8, {"featureSize": 40, "amplitude": 3, "seed": 11}), (12, None), (16, None), (16, None)):
      await session.expectSuccess("cutCave", fine | {"edgeLength": edgeLength, "breakup": breakup})
      checks.append((await session.expectSuccess("runPython", {"code": checkCave}))["result"])
      await session.expectSuccess("removeCave", {"objectName": "ground", "name": "hall"})
    return zipped, zippedFar, checks

  zipped, zippedFar, checks = stageBlenderServer.session(steps)
  # The slit goes, and each face takes the other's corner on the line, so the two meet edge to edge; far out too, where the copies of
  # one point come together.
  assert zipped == {"faces": [[0, 4, 7, 1, 2, 3], [6, 5, 1, 7, 4, 0]], "sources": [0, 1]}
  assert zippedFar == zipped | {"snapped": [[0, 1, 2], [0, 2, 1]]}
  # At every edgeLength, and cut again after being taken back, the cut is sealed where the tube's end stands on the ground in front of
  # the mouth.
  for checked in checks:
    assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges


def testTheCavesPatchTakesInTheHolesInTheReachAndRefusesOnlyWhatNoGroundFills(stageBlenderServer, tmp_path):
  # A grid of triangles; the patch is the faces whose centers lie within 3.5 of the origin on both axes, less the faces round the origin's
  # vertex (a pinhole) or the eight triangles of the four cells about it.
  holes = r"""
import bmesh, numpy
import bridgeCaves
editor = bmesh.new()
bmesh.ops.create_grid(editor, x_segments=8, y_segments=8, size=4)
bmesh.ops.triangulate(editor, faces=list(editor.faces))
editor.verts.ensure_lookup_table()
editor.faces.ensure_lookup_table()
centers = {face: face.calc_center_median() for face in editor.faces}
inside = [face for face in editor.faces if abs(centers[face].x) < 3.5 and abs(centers[face].y) < 3.5]
insideSet = set(inside)
origin = min(editor.verts, key=lambda vertex: vertex.co.length)
pinhole = [face for face in inside if origin not in face.verts]
cells = [face for face in inside if max(abs(centers[face].x), abs(centers[face].y)) >= 1]
positions = numpy.array([list(vertex.co) for vertex in editor.verts])
free = [False] * len(editor.faces)
lined = free[:]
lined[next(face for face in inside if face not in set(cells)).index] = True
border = [edge for face in inside for edge in face.edges if sum(other in insideSet for other in edge.link_faces) == 1]
result = {
  'inside': len(inside), 'pinhole': [len(pinhole), len(bridgeCaves.closedPatch(pinhole, free))],
  'cells': [len(cells), len(bridgeCaves.closedPatch(cells, free))], 'lined': [len(cells), len(bridgeCaves.closedPatch(cells, lined))],
  'whole': len(bridgeCaves.closedPatch(inside, free)), 'border': len(border),
  'rings': [len(bridgeCaves.patchBoundary(bridgeCaves.closedPatch(patch, free), positions)) for patch in (pinhole, cells, inside)],
}
try:
  bridgeCaves.patchBoundary(bridgeCaves.closedPatch(cells, lined), positions)
except ValueError as error:
  result['refusal'] = str(error)
editor.free()
"""

  async def steps(session):
    await session.expectSuccess("newFile", {"discardUnsavedChanges": True})
    return (await session.expectSuccess("runPython", {"code": holes}))["result"]

  found = stageBlenderServer.session(steps)
  # Each hole's faces join the patch, so it comes out as the patch with no hole does (which also fills its jagged edge's notches) and its
  # edge is one loop; a hole holding a face another cave's lining marks is left, and the refusal then says the edge has a second loop
  # and where.
  assert found["pinhole"][0] == found["inside"] - 6 and found["cells"][0] == found["inside"] - 8
  assert found["pinhole"][1] == found["cells"][1] == found["whole"] > found["inside"]
  assert found["rings"][0] == found["rings"][1] == found["rings"][2] and found["lined"][1] < found["whole"]
  assert "first loop only" in found["refusal"] and "vertices of the other loops lie near [[" in found["refusal"]


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
    approach = (await session.expectSuccess("runPython", {"code": readApproach}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return removed, shown, approach, detail

  removed, shown, approach, detail = stageBlenderServer.session(steps)
  ground, control = numpy.array(shown["ground"]), numpy.array(shown["control"])
  # The cut settled the approach, grading it again on the cut ground; everywhere else the ground is the uncut copy's, and the approach,
  # graded on ground the cave no longer cuts, is stale to grade again.
  graded = numpy.array(approach["ground"]) | numpy.array(approach["control"])
  assert ground.shape == control.shape and graded.any() and numpy.abs(ground - control)[~graded].max() <= 1e-5
  assert [(entry["route"], entry["stale"]) for entry in detail["definedPasses"]] == [("approach", True)]
  assert removed["definition"]["path"] == hall["path"] and removed["definition"]["widths"] == hall["widths"] and removed["restoredFaces"] > 0
  assert detail["caves"] == []


def testACaveFarFromTheOriginIsEditedRefittedAndRemovedWhileADisplacedRingIsStillCaught(stageBlenderServer, tmp_path):
  # Out past 2048, where single precision keeps coordinates 2.4e-4 apart: more than the ring's own tolerance near the origin.
  east = 2600
  far = hall | {"path": [[x + east, y, z] for x, y, z in hall["path"]]}
  displaceRing = r"""
import numpy
mesh = bpy.data.objects['ground'].data
tags = numpy.empty(len(mesh.vertices), dtype=numpy.int32)
mesh.attributes['zonewrightCaveVertex:hall'].data.foreach_get('value', tags)
row = int(numpy.flatnonzero(tags == -1)[0])
point = mesh.shape_keys.key_blocks['cliff'].data[row]
result = {'row': row, 'co': list(point.co)}
point.co = (result['co'][0] + 0.01, result['co'][1], result['co'][2])
mesh.update()
"""

  async def steps(session):
    await caveCanyon(session, tmp_path, east=east)
    await session.expectSuccess("cutCave", far)
    checked = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    edited = await session.expectSuccess("editCave", {"objectName": "ground", "name": "hall"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "mouthEdit"})
    await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [east, -30, 30], "radius": 30, "strength": 5, "direction": [0, -1, 0]})
    stale = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    refitted = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    original = (await session.expectSuccess("runPython", {"code": displaceRing}))["result"]
    displaced = await session.expectError("editCave", {"objectName": "ground", "name": "hall"})
    await session.expectSuccess("runPython", {"code": f"point = bpy.data.objects['ground'].data.shape_keys.key_blocks['cliff'].data[{original['row']!r}]\npoint.co = {tuple(original['co'])!r}"})
    for index in range(30):
      await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": f"mouth{index}"})
      await session.expectSuccess("sculptAtPoint", {"objectName": "ground", "mode": "raise", "center": [east + index % 5 * 7 - 14, index - 10, 60], "radius": 80, "strength": 0.4 + index * 0.01})
    await session.expectSuccess("collapseShapingPasses", {"objectName": "ground"})
    removed = await session.expectSuccess("removeCave", {"objectName": "ground", "name": "hall"})
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return checked, edited, stale, regraded, refitted, displaced, removed, detail

  checked, edited, stale, regraded, refitted, displaced, removed, detail = stageBlenderServer.session(steps)
  # Cut, the ring misses its plug triangles by single precision's rounding there, past 1e-4, and the lining stands still.
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges and checked["ringVertices"] > 0
  assert 1e-4 < checked["largestRingMiss"] <= 1e-3 and checked["largestLiningMove"] == 0.0
  # The cave is cut again unchanged, flagged stale by a sculpt at its mouth, refitted by a regrade, and, with thirty passes over its
  # mouth collapsed (Blender mixes them in single precision), taken back out.
  assert edited["cut"]["liningFaces"] > 0 and [cave["stale"] for cave in stale["caves"]] == [True]
  assert [entry["cave"] for entry in regraded["refittedCaves"]] == ["hall"] and refitted["edgesOnThreeOrMoreFaces"] == 0
  assert refitted["largestRingMiss"] <= 1e-3 and refitted["largestLiningMove"] == 0.0
  assert removed["restoredFaces"] > 0 and detail["caves"] == []
  # A ring vertex moved 0.01 in one pass is caught there alone.
  found = re.findall(r"a ring vertex stands ([0-9.e-]+) off its plug triangle in ([^;(]+)", displaced)
  assert len(found) == 1 and found[0][1].strip() == "pass 'cliff'" and abs(float(found[0][0]) - 0.01) <= 5e-4


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


def testRoutesAndACaveThatMoveEachOthersGroundSettleTogether(stageBlenderServer, tmp_path):
  # caveCanyon grades the approach up to the mouth before the cave is cut; the ramp beside the mouth is graded after, inside its reach.
  ramp = {"objectName": "ground", "name": "ramp", "points": [[70, -150], [70, -50]], "width": 30}
  stale = lambda detail: ([entry["route"] for entry in detail["definedPasses"] if entry["stale"]], [cave["name"] for cave in detail["caves"] if cave["stale"]])

  async def steps(session):
    await caveCanyon(session, tmp_path)
    _, cut = await session.expectImage("cutCave", hall)
    afterCut = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    graded = await session.expectSuccess("gradeRoute", ramp)
    afterGrade = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    await session.expectImage("editCave", {"objectName": "ground", "name": "hall"})
    afterRefit = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    regraded = await session.expectSuccess("regradeTerrain", {"objectName": "ground"})
    return cut, afterCut, graded, afterGrade, afterRefit, regraded

  cut, afterCut, graded, afterGrade, afterRefit, regraded = stageBlenderServer.session(steps)
  # The cut runs through the approach's end, so the approach is graded again and the cave refitted to it; nothing is left stale.
  assert cut["settled"]["unsettled"] is None and [entry["route"] for entry in cut["settled"]["rounds"][0]["replayed"]] == ["approach"]
  assert [entry["cave"] for entry in cut["settled"]["rounds"][0]["refittedCaves"]] == ["hall"] and stale(afterCut) == ([], [])
  # The ramp's batters reach the cliff inside the cave's reach: the cave is refitted and both routes graded again, all settled.
  assert graded["settled"]["unsettled"] is None and any(round["refittedCaves"] for round in graded["settled"]["rounds"])
  assert graded["staleCaves"] == [] and stale(afterGrade) == ([], [])
  # A refit settles what it moves too, and a regrade then has nothing left to move.
  assert stale(afterRefit) == ([], [])
  assert [entry["movedVertices"] for entry in regraded["replayed"]] == [0, 0] and regraded["refittedCaves"] == []
  assert regraded["furtherRounds"] == [] and regraded["unsettled"] is None


def testSubdividingInFrontOfACaveCutsItAgainAndRegradesTheApproach(stageBlenderServer, tmp_path):
  forecourt = {"box": {"minimum": [-60, -120, -20], "maximum": [60, -30, 60]}}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectImage("cutCave", hall)
    before = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    split = await session.expectSuccess("subdivide", {"objectName": "ground", "selector": forecourt, "cuts": 1})
    after = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    checked = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    walk = await session.expectSuccess("walkRoute", {"path": intoTheRoom, "sampleSpacing": 4})
    return before, split, after, checked, walk

  before, split, after, checked, walk = stageBlenderServer.session(steps)
  # The cave comes back cut on the finer ground, every pass kept, and nothing is left stale; the cave is sealed and walked into.
  assert split["subdividedFaces"] > 0 and after["faces"] > before["faces"]
  assert [entry["cave"] for entry in split["recutCaves"]] == ["hall"] and split["recutCaves"][0]["liningFaces"] > 0
  assert split["settled"]["unsettled"] is None
  assert [entry["name"] for entry in after["shapingPasses"]] == [entry["name"] for entry in before["shapingPasses"]]
  assert [(entry["route"], entry["stale"]) for entry in after["definedPasses"]] == [("approach", False)]
  assert [(cave["name"], cave["stale"]) for cave in after["caves"]] == [("hall", False)]
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges and checked["largestLiningMove"] == 0.0
  assert walkOf(walk)["walkable"]


# Moves the vertex of the hall's reach nearest `at` (none of the cave's own) by `shift` in plan, alike in the mesh and every pass, so
# each pass keeps its offset there and the base's own layout folds.
foldBaseInReach = r"""
import numpy
import bridgeCaveData, bridgeMeshAccess
ground = bpy.data.objects['ground']
mesh = ground.data
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
candidates = numpy.flatnonzero(bridgeCaveData.reachOf(ground, 'hall') & ~bridgeCaveData.fixedInPlan(ground))
row = int(candidates[numpy.argmin(numpy.linalg.norm(shown[candidates, :2] - numpy.array(at), axis=1))])
for block in [mesh.vertices] + [key.data for key in mesh.shape_keys.key_blocks]:
  block[row].co = (block[row].co[0] + shift[0], block[row].co[1] + shift[1], block[row].co[2])
mesh.update()
result = shown[row].tolist()
"""
# The ground's faces (none of the cave's) centered in the forecourt east of the mouth that face down seen from above or have no area
# in plan.
forecourtFolds = r"""
import numpy
import bridgeMeshAccess
ground = bpy.data.objects['ground']
mesh = ground.data
shown, _ = bridgeMeshAccess.readVertexArrays(ground)
tags = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
mesh.attributes['zonewrightCaveFace:hall'].data.foreach_get('value', tags)
result = 0
for polygon, tag in zip(mesh.polygons, tags):
  corners = shown[list(polygon.vertices)]
  middle = corners.mean(axis=0)
  if tag == 0 and 36 <= middle[0] <= 100 and -70 <= middle[1] <= -24 and -20 <= middle[2] <= 60:
    following = numpy.roll(corners, -1, axis=0)
    result += int((corners[:, 0] * following[:, 1] - following[:, 0] * corners[:, 1]).sum() <= 0)
"""
snapshotGround = r"""
ground = bpy.data.objects['ground']
mesh = ground.data
result = {
  'caves': ground['zonewrightCaves'], 'faces': [list(polygon.vertices) for polygon in mesh.polygons], 'mesh': [list(vertex.co) for vertex in mesh.vertices],
  'passes': {key.name: [list(point.co) for point in key.data] for key in mesh.shape_keys.key_blocks}, 'active': ground.active_shape_key_index,
}
"""


def testUnfoldingABaseFoldedBeforeACaveRefitsTheCaveWholeOrNotAtAll(stageBlenderServer, tmp_path):
  # The ground at the cliff's foot east of the mouth, in the hall's reach and clear of the approach's bench and the cliff.
  forecourt = {"box": {"minimum": [36, -70, -20], "maximum": [100, -24, 60]}}
  # A spike beside the approach far higher than its batters can reach: grading the approach again on that ground refuses.
  spike = {"objectName": "ground", "selector": {"box": {"minimum": [36, -138, -100], "maximum": [52, -122, 100]}}, "offset": [0, 0, 2400]}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectImage("cutCave", hall)
    foldsCut = (await session.expectSuccess("runPython", {"code": forecourtFolds}))["result"]
    foldedAt = (await session.expectSuccess("runPython", {"code": "at = [60, -40]\nshift = [13, 5]" + foldBaseInReach}))["result"]
    foldsMade = (await session.expectSuccess("runPython", {"code": forecourtFolds}))["result"]
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "spike"})
    await session.expectSuccess("moveVertices", spike)
    before = (await session.expectSuccess("runPython", {"code": snapshotGround}))["result"]
    refused = await session.expectError("unfoldTerrain", {"objectName": "ground", "selector": forecourt})
    kept = (await session.expectSuccess("runPython", {"code": snapshotGround}))["result"]
    await session.expectSuccess("removeShapingPass", {"objectName": "ground", "name": "spike"})
    await session.expectSuccess("addShapingPass", {"objectName": "ground", "name": "unfold"})
    unfolded = await session.expectSuccess("unfoldTerrain", {"objectName": "ground", "selector": forecourt})
    foldsLeft = (await session.expectSuccess("runPython", {"code": forecourtFolds}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    checked = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    again = await session.expectSuccess("unfoldTerrain", {"objectName": "ground", "selector": forecourt})
    return foldsCut, foldedAt, foldsMade, before, refused, kept, unfolded, foldsLeft, detail, checked, again

  foldsCut, foldedAt, foldsMade, before, refused, kept, unfolded, foldsLeft, detail, checked, again = stageBlenderServer.session(steps)
  assert foldsCut == 0 and foldsMade > 0 and math.dist(foldedAt[:2], [60, -40]) < 8
  # Grading the approach again on the spike refuses, so the unfold that moved the cave's ground is taken back whole.
  assert "settling them refused" in refused and "Route 'approach' runs" in refused
  assert kept == before
  # Without it, the fold the base holds is laid out again, the cave on that ground is cut again, and nothing is left stale or folded.
  assert unfolded["relaidVertices"] > 0 and unfolded["foldedFaces"] == 0 and foldsLeft == 0
  assert unfolded["settled"]["unsettled"] is None and ["hall"] in [[entry["cave"] for entry in settleRound["refittedCaves"]] for settleRound in unfolded["settled"]["rounds"]]
  assert [(cave["name"], cave["stale"]) for cave in detail["caves"]] == [("hall", False)] and [entry["stale"] for entry in detail["definedPasses"]] == [False]
  assert checked["edgesOnThreeOrMoreFaces"] == 0 and checked["openEdges"] == borderEdges and checked["largestRingMiss"] <= 1e-4 and checked["largestLiningMove"] == 0.0
  # Laid out, the forecourt holds no fold to unfold, so a second unfold moves nothing and has nothing to settle.
  assert (again["unfoldedFaces"], again["relaidFaces"], again["foldedFaces"], again["movedVertices"], again["relaidVertices"]) == (0, 0, 0, 0, 0) and "settled" not in again


def testCaveRefusals(stageBlenderServer, tmp_path):
  async def steps(session):
    await caveCanyon(session, tmp_path)
    # Its floor sunk 8 into the graded approach in front of the cliff, with its walls and vault in the open.
    partInRock = await session.expectError("cutCave", hall | {"name": "partInRock", "path": [[0, -60, -6]] + hall["path"][1:]})
    # Its floor starting 10 over the graded approach, falling to the tunnel.
    hanging = await session.expectError("cutCave", hall | {"name": "hanging", "path": [[0, -60, 12]] + hall["path"][1:]})
    # Its floor starting 3 over the approach at 2 and rising toward the tunnel: a player steps up that far, yet the floor shows hanging
    # in the air.
    hangingLow = await session.expectError("cutCave", hall | {"name": "hangingLow", "path": [[0, -60, 5]] + hall["path"][1:]})
    steep = await session.expectError("cutCave", hall | {"name": "steep", "path": [[0, -60, 2], [0, 0, 60]], "widths": [40] * 2, "heights": [45] * 2})
    tight = await session.expectError("cutCave", hall | {"name": "tight", "path": [[0, -60, 2], [0, 40, 6], [-30, 10, 6]], "widths": [40] * 3, "heights": [45] * 3})
    border = await session.expectError("cutCave", hall | {"name": "border", "path": [[0, -60, 2], [0, 30, 6], [0, 370, 6]], "widths": [40] * 3, "heights": [45] * 3})
    overWalkable = await session.expectError("cutCave", hall | {"name": "overWalkable", "maximumFloorDegrees": pastWalkableDegrees})
    await session.expectSuccess("cutCave", hall)
    before = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    # A tunnel 45 east of the hall, whose reach takes in the hall's mouth.
    overlap = await session.expectError("cutCave", hall | {"name": "beside", "path": [[45, -60, 2], [45, 30, 6], [45, 150, 8]], "widths": [40] * 3, "heights": [45] * 3})
    edited = await session.expectError("editCave", {"objectName": "ground", "name": "hall", "changes": {"path": [[0, -60, 2], [0, 0, 60], [0, 90, 8], [0, 130, 8], [0, 250, 8]]}})
    after = (await session.expectSuccess("runPython", {"code": checkCave}))["result"]
    detail = await session.expectSuccess("getObjectDetail", {"name": "ground"})
    return partInRock, hanging, hangingLow, steep, tight, border, overWalkable, overlap, edited, before, after, detail

  partInRock, hanging, hangingLow, steep, tight, border, overWalkable, overlap, edited, before, after, detail = stageBlenderServer.session(steps)
  assert "The cave's start at [0.0, -60.0, -6.0] is part in the rock (up to 8.0 into it)" in partInRock
  assert "The cave's floor hangs in the air from [0.0, -60.0, 12.0]" in hanging and "the ground up to 10.0 below it" in hanging
  assert "The cave's floor hangs in the air from [0.0, -60.0, 5.0] to [0.0, -24.0, 5.4]: no rock lies under its middle within 2, the ground up to 3.4 below it" in hangingLow
  assert "rises 58.0 from point 0 to point 1 over a run of 60.0" in steep and "needs a run of 100.5" in steep
  assert "tighter than half its width" in tight
  assert "reaches the edge of 'ground'" in border
  # A cave's floor is walked, so its grade limit runs up to the steepest face every group its start is built for walks (playerScale).
  assert f"maximumFloorDegrees is above 0 and at most {steepestWalkableDegrees:.1f} (the steepest face every body group the cave's start is built for walks, ['small', 'medium', 'large'], playerScale), got {pastWalkableDegrees}" in overWalkable
  assert "Cave 'beside' would reach the mouth of cave(s) ['hall']" in overlap
  # A refused edit leaves the cave as it was.
  assert "rises 58.0" in edited and after == before and detail["caves"][0]["from"] == hall["path"][0]


def testACaveFloorClimbsAsSteeplyAsPlayersWalk(stageBlenderServer, tmp_path):
  shaft = hall | {"name": "shaft", "path": [[0, -60, 2], [0, 30, 6], [0, 60, 62]], "widths": [40] * 3, "heights": [45] * 3, "breakup": None, "maximumFloorDegrees": 65}

  async def steps(session):
    await caveCanyon(session, tmp_path)
    cut = await session.expectSuccess("cutCave", shaft)
    floor = await session.expectSuccess("measure", {"points": [[0, 40, 45], [0, 55, 75]], "snapToSurface": True})
    return cut, floor

  cut, floor = stageBlenderServer.session(steps)
  # A floor climbing past 60 degrees, under the steepest face players walk (playerScale), is cut at its grade.
  grade = numpy.degrees(numpy.arctan2(62 - 6, 30))
  assert grade > 60 and cut["liningFaces"] > 0 and abs(floor["segments"][0]["slopeDegrees"] - grade) <= 0.1


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
  assert walkOf(walk)["walkable"] is True and walkOf(walk)["problems"] == []
  covered = [row["headroom"] is not None for row in walkOf(walk)["profile"]]
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


def testCratesUnderALipAtACaveMouthSettleOnTheFloorEvenUnderAFaceMetFromBehind(stageBlenderServer, tmp_path):
  lip = {
    "name": "lip", "outline": [[-50, -50], [50, -50], [50, 40], [-50, 40]], "top": 120, "ground": "ground", "spacing": 8,
    "underside": {"axis": [[0, -50], [0, 40]], "profile": [[0, 70], [90, 70]]},
  }
  spots = {"underAwning": [0, -36], "underLip": [12, -40], "inside": [0, 60]}
  readFloors = f"spots = {spots!r}\n" + r"""
import mathutils
depsgraph = bpy.context.evaluated_depsgraph_get()
down = mathutils.Vector((0, 0, -1))
result = {name: [bpy.context.scene.ray_cast(depsgraph, mathutils.Vector((x + dx, y + dy, 15)), down)[1].z for dx in (-2, 0, 2) for dy in (-2, 0, 2)] for name, (x, y) in spots.items()}
"""

  async def steps(session):
    await caveCanyon(session, tmp_path)
    await session.expectSuccess("cutCave", hall)
    await session.expectSuccess("createRockFromOutline", lip)
    floors = (await session.expectSuccess("runPython", {"code": readFloors}))["result"]
    await session.expectSuccess("createPrimitive", {"kind": "plane", "name": "awning", "size": [16, 16, 0], "location": [0, -36, 20]})
    for name, (x, y) in spots.items():
      await session.expectSuccess("createPrimitive", {"kind": "cube", "name": name, "size": [4, 4, 4], "location": [x, y, max(floors[name]) + 1]})
    return floors, await session.expectSuccess("settleObjects", {"names": list(spots)})

  floors, settled = stageBlenderServer.session(steps)
  settled = {crate["name"]: crate for crate in settled["settled"]}
  # Under the lip over the hall's mouth, a one-sided awning over the floor is met from behind from a crate's middle, as the terrain's own
  # surface is where it folds over a mouth: that crate settles on the floor under it, as one under the bare lip and one under the
  # cave's ceiling do, not on the lip's top.
  for name, heights in floors.items():
    assert numpy.allclose(settled[name]["under"], [min(heights), max(heights)], atol=0.01), (name, settled[name])
    assert abs(settled[name]["spans"][0] - min(heights)) <= 0.01


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
  assert walkOf(walk)["walkable"] is True and walkOf(walk)["problems"] == [] and walkOf(walk)["lowestHeadroom"]["headroom"] >= 45 - 5 - 1
