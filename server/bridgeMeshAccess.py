"""World-space mesh access and selectors: which vertices, edges, or faces of a mesh an operation touches. Runs under Blender's Python."""
import contextlib
import json
import math
import statistics

import bmesh
import bpy
import mathutils
import mathutils.bvhtree
import mathutils.kdtree
import numpy

import bridgeNoise

selectorKeys = (
  "all", "sphere", "box", "cylinder", "facing", "slope", "height", "nearPath", "material", "vertexGroup", "insideObject", "region", "noise",
  "underWater", "nearWater", "and", "or", "not",
)
selectorFields = {
  "sphere": ("center", "radius"), "box": ("minimum", "maximum"), "cylinder": ("center", "radius", "bottom", "top"),
  "facing": ("direction", "withinDegrees"), "slope": ("minimumDegrees", "maximumDegrees"), "height": ("minimum", "maximum"),
  "nearPath": ("path", "radius"), "noise": ("featureSize", "share", "seed"), "nearWater": ("water", "distance"),
}
# A region is a vertical prism over an outline: an area of the zone chosen for what it is to become, its intent kept in this property.
regionIntentProperty = "zonewrightRegionIntent"
# A mesh surfaced by layers keeps their order in this property; its face materials are composed from them.
surfaceLayersProperty = "zonewrightSurfaceLayers"
# A water body (bridgeWater) keeps what it was made from in this property, so every edit rebuilds it from that against the ground.
waterProperty = "zonewrightWater"
# A guide is drawn to design with (a plot's outline) and never exported; a plot's border is a server-placed door, exported in the
# zone's housing file rather than its geometry.
guideProperty = "zonewrightGuide"
plotBorderProperty = "zonewrightPlotBorder"
# What placed client content is, so export leaves it out and says why: spawns and doors are the server's data, client objects do not
# export yet, and imported zones are reference.
clientContentProperty = "zonewrightClientContent"
clientContentKinds = ("spawn", "door", "object", "zone", "zoneFile")
waterReach = 100000.0


def requireObject(name):
  # World matrices of objects created or moved since the last evaluation are stale until the view layer updates.
  bpy.context.view_layer.update()
  sceneObject = bpy.context.scene.objects.get(name)
  if sceneObject is None:
    raise ValueError(f"No object named '{name}' in scene '{bpy.context.scene.name}'")
  return sceneObject


def requireRegion(name):
  regionObject = requireObject(name)
  if regionObject.type != "MESH" or regionIntentProperty not in regionObject:
    raise ValueError(f"'{name}' is not a region; createRegion makes one")
  return regionObject


def regionShape(regionObject):
  """A region's outline [x, y] in world units, in order, and the heights of its bottom and top."""
  coordinates = numpy.empty(len(regionObject.data.vertices) * 3)
  regionObject.data.vertices.foreach_get("co", coordinates)
  world = worldPositions(regionObject, coordinates.reshape(-1, 3))
  sides = len(world) // 2
  return world[:sides, :2], float(world[:, 2].min()), float(world[:, 2].max())


def insidePolygon(points, outline):
  """Which [x, y] points lie inside a closed outline, by counting crossings of a ray toward +x."""
  inside = numpy.zeros(len(points), dtype=bool)
  for (x1, y1), (x2, y2) in zip(outline, numpy.roll(outline, -1, axis=0)):
    straddles = (y1 > points[:, 1]) != (y2 > points[:, 1])
    with numpy.errstate(divide="ignore", invalid="ignore"):
      crossingX = x1 + (points[:, 1] - y1) * (x2 - x1) / (y2 - y1)
    inside ^= straddles & (points[:, 0] < crossingX)
  return inside


def insideRegion(regionObject, worldPoints):
  outline, bottom, top = regionShape(regionObject)
  return insidePolygon(worldPoints[:, :2], outline) & (worldPoints[:, 2] >= bottom) & (worldPoints[:, 2] <= top)


def requireWater(name):
  waterObject = requireObject(name)
  if waterObject.type != "MESH" or waterProperty not in waterObject:
    raise ValueError(f"'{name}' is not a water body; floodWater, runWater, and pourWaterfall make them")
  return waterObject


def surfaceLayers(sceneObject):
  """A mesh's surfacing layers, bottom first: [{name, muted}]."""
  return json.loads(sceneObject[surfaceLayersProperty]) if surfaceLayersProperty in sceneObject else []


def isDesignAid(sceneObject):
  """Guides and plot borders: drawn in views, but not the zone's own geometry."""
  return guideProperty in sceneObject or plotBorderProperty in sceneObject


def isCollectionInstance(sceneObject):
  return sceneObject.type == "EMPTY" and sceneObject.instance_type == "COLLECTION" and sceneObject.instance_collection is not None


def isPlayerSolid(sceneObject):
  """Whether players stand on and are blocked by an object: rendered meshes and collection instances, but not guides, plot borders,
  regions, water bodies (swum, not stood on), spawns (players pass through them), or doors (taken as open)."""
  if sceneObject.hide_render or isDesignAid(sceneObject) or regionIntentProperty in sceneObject or waterProperty in sceneObject:
    return False
  if sceneObject.get(clientContentProperty) in ("spawn", "door"):
    return False
  return sceneObject.type == "MESH" or isCollectionInstance(sceneObject)


def objectParts(sceneObject):
  """A mesh with its world matrix, or each rendered mesh of a collection instance as placed."""
  if sceneObject.type == "MESH":
    return [(sceneObject, sceneObject.matrix_world.copy())]
  if isCollectionInstance(sceneObject):
    collection = sceneObject.instance_collection
    placement = sceneObject.matrix_world @ mathutils.Matrix.Translation(-collection.instance_offset)
    return [(member, placement @ member.matrix_world) for member in collection.all_objects if member.type == "MESH" and not member.hide_render]
  raise ValueError(f"'{sceneObject.name}' is a {sceneObject.type}; it has no mesh")


def worldBoundsCorners(sceneObject, depsgraph):
  """The world corners of the evaluated bounding boxes of an object's meshes (objectParts)."""
  return [matrix @ mathutils.Vector(corner) for part, matrix in objectParts(sceneObject) for corner in part.evaluated_get(depsgraph).bound_box]


def playerSolidParts(excluding=()):
  """Each mesh players stand on and are blocked by, with its world matrix (objectParts), leaving out the objects named in excluding."""
  # An object moved or made since the last evaluation still holds its old world matrix until the scene is evaluated.
  bpy.context.view_layer.update()
  parts = [part for sceneObject in bpy.context.scene.objects if isPlayerSolid(sceneObject) and sceneObject.name not in excluding for part in objectParts(sceneObject)]
  if not parts:
    raise ValueError("The scene has nothing players stand on: no rendered meshes or collection instances besides water, guides, regions, spawns, and doors")
  return parts


class PlayerSurfaces:
  """Ray casts against what players stand on and are blocked by (playerSolidParts)."""

  def __init__(self):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    self.members = [(matrix, matrix.inverted(), mathutils.bvhtree.BVHTree.FromObject(part, depsgraph)) for part, matrix in playerSolidParts()]

  def cast(self, origin, direction, distance):
    """The nearest world hit point within distance, or None."""
    hit = self.castWithNormal(origin, direction, distance)
    return hit[0] if hit else None

  def footingBelow(self, origin, distance):
    """The first surface below origin within distance that faces up; an underside met first means origin lies inside rock, and the
    search goes on through it."""
    down = mathutils.Vector((0, 0, -1))
    while True:
      hit = self.castWithNormal(origin, down, distance)
      if hit is None:
        return None
      if hit[1].z > 0:
        return hit[0]
      distance -= origin.z - hit[0].z + 0.01
      origin = hit[0] + down * 0.01

  def castWithNormal(self, origin, direction, distance):
    """The nearest world hit point within distance and the normal of the face hit there, or None."""
    nearest = None
    for matrix, inverse, tree in self.members:
      location, normal, _, _ = tree.ray_cast(inverse @ origin, (inverse.to_3x3() @ direction).normalized())
      if location is None:
        continue
      hit = matrix @ location
      along = (hit - origin).length
      if along <= distance and (nearest is None or along < nearest[0]):
        nearest = (along, hit, (matrix.to_3x3().inverted().transposed() @ normal).normalized())
    return nearest[1:] if nearest else None


def swimSurfaces():
  """A BVH over the surfaces of rendered pools and rivers (falls are not swum), or None when the scene has none."""
  bodies = [
    sceneObject for sceneObject in bpy.context.scene.objects
    if waterProperty in sceneObject and not sceneObject.hide_render and json.loads(sceneObject[waterProperty])["kind"] != "fall"
  ]
  return worldTree(bodies) if bodies else None


def waterDepthAt(surfaces, point):
  """How far a pool or river's surface stands above a point (surfaces from swimSurfaces), or None where no water lies above it."""
  if surfaces is None:
    return None
  location, _, _, _ = surfaces.ray_cast(mathutils.Vector(point), mathutils.Vector((0.0, 0.0, 1.0)), waterReach)
  return None if location is None else location.z - point[2]


def partTriangles(parts):
  """The evaluated meshes of (object, world matrix) parts as world positions and triangles, all in one."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  positions, triangles, offset = [], [], 0
  for sceneObject, worldMatrix in parts:
    evaluated = sceneObject.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
      mesh.calc_loop_triangles()
      coordinates = numpy.empty(len(mesh.vertices) * 3)
      mesh.vertices.foreach_get("co", coordinates)
      corners = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
      mesh.loop_triangles.foreach_get("vertices", corners)
    finally:
      evaluated.to_mesh_clear()
    matrix = matrixArray(worldMatrix)
    positions.append(coordinates.reshape(-1, 3) @ matrix[:3, :3].T + matrix[:3, 3])
    triangles.append(corners.reshape(-1, 3) + offset)
    offset += len(positions[-1])
  if not positions:
    return numpy.zeros((0, 3)), numpy.zeros((0, 3), dtype=numpy.int64)
  return numpy.concatenate(positions), numpy.concatenate(triangles)


def worldTriangles(sceneObjects):
  """The evaluated meshes of objects as world positions and triangles, all in one."""
  return partTriangles([(sceneObject, sceneObject.matrix_world) for sceneObject in sceneObjects])


def worldTree(sceneObjects):
  """A BVH tree over objects in world space, its faces wound as the meshes wind them."""
  positions, triangles = worldTriangles(sceneObjects)
  if len(triangles) == 0:
    raise ValueError(f"{[sceneObject.name for sceneObject in sceneObjects]} have no faces")
  return mathutils.bvhtree.BVHTree.FromPolygons(positions.tolist(), triangles.tolist())


def underWaterMask(waterObject, positions):
  """Which points lie under a water body's surface."""
  tree = worldTree([waterObject])
  up = mathutils.Vector((0.0, 0.0, 1.0))
  return numpy.array([tree.ray_cast(mathutils.Vector(point), up, waterReach)[0] is not None for point in positions], dtype=bool)


def nearWaterMask(waterObject, positions, distance):
  """Which points lie out of a water body but within distance of its surface, which reaches a little under its banks."""
  if distance <= 0:
    raise ValueError(f"The nearWater selector's distance must be positive, got {distance}")
  tree = worldTree([waterObject])
  under = underWaterMask(waterObject, positions)
  near = numpy.array([tree.find_nearest(mathutils.Vector(point), distance)[0] is not None for point in positions], dtype=bool)
  return near & ~under


def requireMeshObject(name):
  sceneObject = requireObject(name)
  if sceneObject.type != "MESH":
    raise ValueError(f"'{name}' is a {sceneObject.type}, not a mesh")
  return sceneObject


def toArray(values):
  return numpy.array(values, dtype=numpy.float64)


def matrixArray(matrix):
  return numpy.array([list(row) for row in matrix], dtype=numpy.float64)


def worldPositions(sceneObject, localPositions):
  matrix = matrixArray(sceneObject.matrix_world)
  return localPositions @ matrix[:3, :3].T + matrix[:3, 3]


def localPositions(sceneObject, worldPoints):
  inverse = matrixArray(sceneObject.matrix_world.inverted())
  return worldPoints @ inverse[:3, :3].T + inverse[:3, 3]


def worldDirections(sceneObject, localNormals):
  normalMatrix = matrixArray(sceneObject.matrix_world.to_3x3().inverted().transposed())
  directions = localNormals @ normalMatrix.T
  lengths = numpy.linalg.norm(directions, axis=1, keepdims=True)
  return numpy.divide(directions, lengths, out=numpy.zeros_like(directions), where=lengths > 0)


def localDirection(sceneObject, worldVector):
  return numpy.array(sceneObject.matrix_world.to_3x3().inverted() @ mathutils.Vector(worldVector))


def hasShapingPasses(sceneObject):
  return sceneObject.data.shape_keys is not None


def requireNoShapingPasses(sceneObject, action):
  if hasShapingPasses(sceneObject):
    raise ValueError(f"'{sceneObject.name}' has shaping passes, which hold one offset per vertex; collapse them (collapseShapingPasses) before you {action}")


@contextlib.contextmanager
def shapedMesh(sceneObject):
  """The mesh as it is seen: its own data, or with shaping passes, the passes combined (Blender's shape key mix)."""
  if not hasShapingPasses(sceneObject):
    yield sceneObject.data
    return
  if sceneObject.modifiers:
    raise ValueError(f"'{sceneObject.name}' has shaping passes and modifiers; passes combine before modifiers, so apply or remove the modifiers")
  bpy.context.view_layer.update()
  evaluated = sceneObject.evaluated_get(bpy.context.evaluated_depsgraph_get())
  mesh = evaluated.to_mesh()
  try:
    yield mesh
  finally:
    evaluated.to_mesh_clear()


def strokeAlongPath(points, path, radii, horizontal):
  """For a stroke along a polyline whose radius changes evenly from each path point's radius to the next: how far across the stroke each
  point lies, as a fraction of the radius there (0 on the path, 1 at the stroke's edge), taken on the segment where that fraction is
  smallest; and there, the path's height, the stroke's radius, and the nearest path spot."""
  pathArray = toArray(path)
  if pathArray.ndim != 2 or pathArray.shape[1] != 3 or len(pathArray) < 2:
    raise ValueError(f"A path is at least two [x, y, z] points, got {path!r}")
  radiiArray = toArray(radii)
  if radiiArray.shape != (len(pathArray),) or (radiiArray <= 0).any():
    raise ValueError(f"radii are one positive radius per path point ({len(pathArray)}), got {radii!r}")
  axes = slice(0, 2) if horizontal else slice(0, 3)
  starts, ends = pathArray[:-1], pathArray[1:]
  segments = ends[:, axes] - starts[:, axes]
  lengths = numpy.maximum((segments * segments).sum(1), 1e-12)
  offsets = points[:, None, axes] - starts[None, :, axes]
  along = numpy.clip((offsets * segments[None]).sum(2) / lengths[None], 0, 1)
  nearest = starts[None, :, axes] + along[:, :, None] * segments[None]
  segmentRadii = radiiArray[None, :-1] + along * (radiiArray[None, 1:] - radiiArray[None, :-1])
  fractions = numpy.linalg.norm(points[:, None, axes] - nearest, axis=2) / segmentRadii
  closest = fractions.argmin(1)
  rows = numpy.arange(len(points))
  heights = starts[closest, 2] + along[rows, closest] * (ends[closest, 2] - starts[closest, 2])
  return fractions[rows, closest], heights, segmentRadii[rows, closest], nearest[rows, closest]


def distancesToPolyline(points, path, horizontal):
  """Distance from each point to a polyline, the interpolated path height at the nearest spot, and that nearest spot."""
  distances, heights, _, nearest = strokeAlongPath(points, path, numpy.ones(len(path)), horizontal)
  return distances, heights, nearest


def readVertexArrays(sceneObject):
  with shapedMesh(sceneObject) as mesh:
    count = len(mesh.vertices)
    coordinates = numpy.empty(count * 3)
    mesh.vertices.foreach_get("co", coordinates)
    normals = numpy.empty(count * 3)
    mesh.vertices.foreach_get("normal", normals)
  return worldPositions(sceneObject, coordinates.reshape(-1, 3)), worldDirections(sceneObject, normals.reshape(-1, 3))


def readFaceArrays(sceneObject):
  with shapedMesh(sceneObject) as mesh:
    count = len(mesh.polygons)
    centers = numpy.empty(count * 3)
    mesh.polygons.foreach_get("center", centers)
    normals = numpy.empty(count * 3)
    mesh.polygons.foreach_get("normal", normals)
    materialIndices = numpy.empty(count, dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", materialIndices)
  return worldPositions(sceneObject, centers.reshape(-1, 3)), worldDirections(sceneObject, normals.reshape(-1, 3)), materialIndices


def vertexGroupMask(sceneObject, groupName):
  group = sceneObject.vertex_groups.get(groupName)
  if group is None:
    raise ValueError(f"'{sceneObject.name}' has no vertex group '{groupName}'")
  mask = numpy.zeros(len(sceneObject.data.vertices), dtype=bool)
  for vertex in sceneObject.data.vertices:
    mask[vertex.index] = any(element.group == group.index and element.weight > 0 for element in vertex.groups)
  return mask


def materialSlotIndex(sceneObject, materialName):
  for index, slot in enumerate(sceneObject.material_slots):
    if slot.material is not None and slot.material.name == materialName:
      return index
  raise ValueError(f"'{sceneObject.name}' has no material '{materialName}'")


def faceLoops(sceneObject):
  """Each face's vertex count and the vertex indices of all faces' corners, face after face."""
  mesh = sceneObject.data
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int32)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  return loopTotals, loopVertices


def boundaryVertexMask(sceneObject):
  """Vertices on the mesh's open edge: on an edge only one face uses."""
  mesh = sceneObject.data
  loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("edge_index", loopEdges)
  faceUses = numpy.bincount(loopEdges, minlength=len(mesh.edges))
  edges = numpy.empty(len(mesh.edges) * 2, dtype=numpy.int64)
  mesh.edges.foreach_get("vertices", edges)
  mask = numpy.zeros(len(mesh.vertices), dtype=bool)
  mask[edges.reshape(-1, 2)[faceUses == 1].ravel()] = True
  return mask


def faceNormals(sceneObject, positions):
  """Each face's normal scaled by its area (Newell's method), from the given vertex positions."""
  loopTotals, loopVertices = faceLoops(sceneObject)
  repeatedTotals = numpy.repeat(loopTotals, loopTotals)
  loopStarts = numpy.repeat(numpy.cumsum(loopTotals) - loopTotals, loopTotals)
  nextLoops = loopStarts + (numpy.arange(len(loopVertices)) - loopStarts + 1) % repeatedTotals
  crosses = numpy.cross(positions[loopVertices], positions[loopVertices[nextLoops]])
  normals = numpy.zeros((len(loopTotals), 3))
  numpy.add.at(normals, numpy.repeat(numpy.arange(len(loopTotals)), loopTotals), crosses)
  return normals / 2


def foldedFaceCount(sceneObject, before, after):
  """Faces a move turned over: their normal now points against where it pointed."""
  return int(((faceNormals(sceneObject, before) * faceNormals(sceneObject, after)).sum(1) < 0).sum())


def faceVertexIndices(sceneObject):
  loopTotals, loopVertices = faceLoops(sceneObject)
  return numpy.split(loopVertices, numpy.cumsum(loopTotals)[:-1])


def evaluateSelector(selector, sceneObject, elementKind):
  """A boolean mask over the object's vertices or faces. Shapes test vertex positions or face centers in world units."""
  if not isinstance(selector, dict) or len(selector) != 1 or next(iter(selector)) not in selectorKeys:
    raise ValueError(f"A selector is one of {list(selectorKeys)} as a single-key object, got {selector!r}")
  key, value = next(iter(selector.items()))
  if key in selectorFields and (not isinstance(value, dict) or set(value) != set(selectorFields[key])):
    raise ValueError(f"The {key} selector is {{\"{key}\": {{{', '.join(selectorFields[key])}}}}}, got {selector!r}")
  if elementKind == "vertices":
    positions, normals = readVertexArrays(sceneObject)
  else:
    positions, normals, materialIndices = readFaceArrays(sceneObject)
  if key == "all":
    if value is not True:
      raise ValueError("The all selector is {\"all\": true}")
    return numpy.ones(len(positions), dtype=bool)
  if key == "sphere":
    return numpy.linalg.norm(positions - toArray(value["center"]), axis=1) <= value["radius"]
  if key == "box":
    return ((positions >= toArray(value["minimum"])) & (positions <= toArray(value["maximum"]))).all(axis=1)
  if key == "cylinder":
    horizontal = numpy.linalg.norm(positions[:, :2] - toArray(value["center"]), axis=1)
    return (horizontal <= value["radius"]) & (positions[:, 2] >= value["bottom"]) & (positions[:, 2] <= value["top"])
  if key == "facing":
    direction = toArray(value["direction"])
    direction = direction / numpy.linalg.norm(direction)
    return normals @ direction >= math.cos(math.radians(value["withinDegrees"]))
  if key == "slope":
    # 0 is flat ground, 90 a vertical wall, beyond 90 an overhang.
    slopes = numpy.degrees(numpy.arccos(numpy.clip(normals[:, 2], -1, 1)))
    return (slopes >= value["minimumDegrees"]) & (slopes <= value["maximumDegrees"])
  if key == "height":
    return (positions[:, 2] >= value["minimum"]) & (positions[:, 2] <= value["maximum"])
  if key == "nearPath":
    return distancesToPolyline(positions, value["path"], horizontal=True)[0] <= value["radius"]
  if key == "material":
    slotIndex = materialSlotIndex(sceneObject, value)
    if elementKind == "faces":
      return materialIndices == slotIndex
    faceMask = readFaceArrays(sceneObject)[2] == slotIndex
    loopTotals, loopVertices = faceLoops(sceneObject)
    vertexMask = numpy.zeros(len(positions), dtype=bool)
    vertexMask[loopVertices[numpy.repeat(faceMask, loopTotals)]] = True
    return vertexMask
  if key == "insideObject":
    return insideMask(requireMeshObject(value), positions)
  if key == "region":
    return insideRegion(requireRegion(value), positions)
  if key == "underWater":
    return underWaterMask(requireWater(value), positions)
  if key == "nearWater":
    return nearWaterMask(requireWater(value["water"]), positions, value["distance"])
  if key == "noise":
    if not 0 < value["share"] < 1:
      raise ValueError(f"The noise selector's share is a fraction between 0 and 1, got {value['share']}")
    # The noise is close to normally distributed with a spread of 1, so this threshold keeps about `share` of the surface.
    threshold = statistics.NormalDist().inv_cdf(1 - value["share"])
    return bridgeNoise.fractalNoise(bridgeNoise.noiseSamplePoints(positions, value["featureSize"], value["seed"]), 2, 0.5) > threshold
  if key == "vertexGroup":
    groupMask = vertexGroupMask(sceneObject, value)
    if elementKind == "vertices":
      return groupMask
    return numpy.array([groupMask[faceVertices].all() for faceVertices in faceVertexIndices(sceneObject)], dtype=bool)
  if key in ("and", "or"):
    if not isinstance(value, list) or len(value) < 2:
      raise ValueError(f"'{key}' takes a list of at least two selectors")
    masks = [evaluateSelector(part, sceneObject, elementKind) for part in value]
    return numpy.logical_and.reduce(masks) if key == "and" else numpy.logical_or.reduce(masks)
  return ~evaluateSelector(value, sceneObject, elementKind)


def meshEdges(mesh):
  edges = numpy.empty(len(mesh.edges) * 2, dtype=numpy.int64)
  mesh.edges.foreach_get("vertices", edges)
  return edges.reshape(-1, 2)


def sharedEdges(mesh):
  """Edges two faces share, each once: the edge and the two faces."""
  loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("edge_index", loopEdges)
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopFaces = numpy.repeat(numpy.arange(len(mesh.polygons)), loopTotals)
  order = numpy.argsort(loopEdges, kind="stable")
  matching = numpy.flatnonzero(loopEdges[order][1:] == loopEdges[order][:-1])
  return loopEdges[order[matching]], loopFaces[order[matching]], loopFaces[order[matching + 1]]


def faceBorders(sceneObject, side, other):
  """The mesh edges where a face of one face mask meets a face of the other, with the face on each side."""
  edges, first, second = sharedEdges(sceneObject.data)
  forward = side[first] & other[second]
  backward = side[second] & other[first] & ~forward
  return numpy.r_[edges[forward], edges[backward]], numpy.r_[first[forward], second[backward]], numpy.r_[second[forward], first[backward]]


class BorderDistance:
  """Exact distances from points to a border made of segments between mesh positions: samples along the segments in a KD tree find
  the segments near a point, and the nearest of those gives the distance, the segment, and how far along it."""

  def __init__(self, starts, ends):
    self.starts, self.spans = starts, ends - starts
    lengths = numpy.linalg.norm(self.spans, axis=1)
    self.step = max(float(numpy.median(lengths)) / 4, 1e-6)
    counts = numpy.maximum(numpy.ceil(lengths / self.step).astype(numpy.int64), 1)
    self.owners = numpy.repeat(numpy.arange(len(starts)), counts + 1)
    fractions = numpy.concatenate([numpy.arange(count + 1) / count for count in counts])
    samples = starts[self.owners] + fractions[:, None] * self.spans[self.owners]
    self.reach = float((lengths / counts).max()) / 2
    self.tree = mathutils.kdtree.KDTree(len(samples))
    for index, sample in enumerate(samples):
      self.tree.insert(sample, index)
    self.tree.balance()

  def nearest(self, point):
    """The distance from a point to the border, the nearest segment, and the fraction along it of the nearest point on it."""
    _, _, sampled = self.tree.find(point)
    segments = numpy.unique(self.owners[[index for _, index, _ in self.tree.find_range(point, sampled + self.reach)]])
    spans = self.spans[segments]
    fractions = numpy.clip(((point - self.starts[segments]) * spans).sum(axis=1) / numpy.maximum((spans * spans).sum(axis=1), 1e-12), 0, 1)
    distances = numpy.linalg.norm(self.starts[segments] + fractions[:, None] * spans - point, axis=1)
    best = int(distances.argmin())
    return float(distances[best]), int(segments[best]), float(fractions[best])


def requireSelection(mask, selector, sceneObject, elementKind):
  count = int(mask.sum())
  if count == 0:
    raise ValueError(f"Selector {selector!r} matches no {elementKind} of '{sceneObject.name}'")
  return count


def loadBMesh(sceneObject):
  meshEditor = bmesh.new()
  meshEditor.from_mesh(sceneObject.data)
  meshEditor.verts.ensure_lookup_table()
  meshEditor.edges.ensure_lookup_table()
  meshEditor.faces.ensure_lookup_table()
  return meshEditor


def storeBMesh(meshEditor, sceneObject):
  if hasShapingPasses(sceneObject):
    meshEditor.free()
    requireNoShapingPasses(sceneObject, "change its faces")
  meshEditor.normal_update()
  meshEditor.to_mesh(sceneObject.data)
  meshEditor.free()
  sceneObject.data.update()


def triangleCount(sceneObject):
  return sum(len(polygon.vertices) - 2 for polygon in sceneObject.data.polygons)


def meshCounts(sceneObject):
  mesh = sceneObject.data
  return {"vertices": len(mesh.vertices), "faces": len(mesh.polygons), "triangles": triangleCount(sceneObject)}


def rayCast(origin, direction, distance, onlyObjects=None):
  """Nearest hit along a ray in the open scene, or only on the named objects; None when nothing is hit within distance."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  origin = mathutils.Vector(origin)
  direction = mathutils.Vector(direction).normalized()
  if onlyObjects is None:
    hit, location, normal, faceIndex, hitObject, _ = bpy.context.scene.ray_cast(depsgraph, origin, direction, distance=distance)
    return (location, normal, faceIndex, hitObject) if hit else None
  nearest = None
  for name in onlyObjects:
    sceneObject = requireMeshObject(name)
    inverse = sceneObject.matrix_world.inverted()
    hit, location, normal, faceIndex = sceneObject.ray_cast(inverse @ origin, (inverse.to_3x3() @ direction).normalized(), depsgraph=depsgraph)
    if not hit:
      continue
    worldLocation = sceneObject.matrix_world @ location
    hitDistance = (worldLocation - origin).length
    if hitDistance <= distance and (nearest is None or hitDistance < nearest[0]):
      worldNormal = (sceneObject.matrix_world.to_3x3().inverted().transposed() @ normal).normalized()
      nearest = (hitDistance, worldLocation, worldNormal, faceIndex, sceneObject)
  return nearest[1:] if nearest else None


@contextlib.contextmanager
def hiddenObjects(names):
  """Hide objects from ray casts for the duration, restoring their visibility after."""
  hidden = [requireObject(name) for name in names]
  previous = [sceneObject.hide_viewport for sceneObject in hidden]
  for sceneObject in hidden:
    sceneObject.hide_viewport = True
  try:
    yield
  finally:
    for sceneObject, wasHidden in zip(hidden, previous):
      sceneObject.hide_viewport = wasHidden


def closestOnObject(container, worldPoint, depsgraph):
  """Distance from a world point to a mesh's surface, and whether the point is inside it (behind the nearest face)."""
  inverse = container.matrix_world.inverted()
  localPoint = inverse @ mathutils.Vector(worldPoint)
  found, location, normal, _ = container.closest_point_on_mesh(localPoint, depsgraph=depsgraph)
  if not found:
    raise ValueError(f"'{container.name}' has no surface to measure against")
  worldLocation = container.matrix_world @ location
  return (worldLocation - mathutils.Vector(worldPoint)).length, (localPoint - location).dot(normal) < 0


def insideMask(container, worldPoints):
  """Which points lie inside a closed mesh; only points within its bounding box are tested."""
  corners = numpy.array([list(container.matrix_world @ mathutils.Vector(corner)) for corner in container.bound_box])
  candidates = numpy.flatnonzero(((worldPoints >= corners.min(0)) & (worldPoints <= corners.max(0))).all(axis=1))
  depsgraph = bpy.context.evaluated_depsgraph_get()
  mask = numpy.zeros(len(worldPoints), dtype=bool)
  for index in candidates:
    mask[index] = closestOnObject(container, worldPoints[index], depsgraph)[1]
  return mask


def sceneTopHeight():
  heights = [(sceneObject.matrix_world @ mathutils.Vector(corner)).z for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.bound_box]
  if not heights:
    raise ValueError("The scene has no meshes")
  return max(heights)
