"""World-space mesh access and selectors: which vertices, edges, or faces of a mesh an operation touches. Runs under Blender's Python."""
import contextlib
import json
import math
import statistics
import typing

import bmesh
import bpy
import mathutils
import mathutils.bvhtree
import mathutils.kdtree
import numpy

import bridgeCaveData
import bridgeNoise
import bridgeStructureData
import playerScale
from buildTolerances import levelProbeLift

selectorKeys = (
  "all", "sphere", "box", "cylinder", "facing", "slope", "height", "nearPath", "material", "vertexGroup", "insideObject", "region", "noise",
  "underWater", "nearWater", "cave", "and", "or", "not",
)
selectorFields = {
  "sphere": ("center", "radius"), "box": ("minimum", "maximum"), "cylinder": ("center", "radius", "bottom", "top"),
  "facing": ("direction", "withinDegrees"), "slope": ("minimumDegrees", "maximumDegrees"), "height": ("minimum", "maximum"),
  "nearPath": ("path", "radius"), "noise": ("featureSize", "share", "seed"), "nearWater": ("water", "distance"),
}
# A region is a vertical prism over an outline: an area of the zone chosen for what it is to become, its intent kept in this property,
# and whether players reach it (play, view, or none) in the next; a region made before access was decided has none.
regionIntentProperty = "zonewrightRegionIntent"
regionAccessProperty = "zonewrightRegionAccess"
# An entry (bridgeEntries) is an arrow empty where players arrive, its kind and where they come from kept in this property.
entryProperty = "zonewrightEntry"
# A mesh surfaced by layers keeps their order in this property; its face materials are composed from them.
surfaceLayersProperty = "zonewrightSurfaceLayers"
# A water body (bridgeWater) keeps what it was made from in this property, so every edit rebuilds it from that against the ground.
waterProperty = "zonewrightWater"
# A swim volume (bridgeSwim) keeps its liquid, its body, and what it was built from in this property.
swimProperty = "zonewrightSwimVolume"
# An emitter a water body's spray placed keeps its body, its spray, and what it stands on in this property; it goes with its body.
sprayProperty = "zonewrightWaterSpray"
# A guide is drawn to design with (a plot's outline) and never exported; a plot's border is a server-placed door, exported in the
# zone's housing file rather than its geometry.
guideProperty = "zonewrightGuide"
plotBorderProperty = "zonewrightPlotBorder"
# What placed client content is, so export leaves it out and says why: spawns and doors are the server's data, client objects do not
# export yet, and imported zones are reference.
clientContentProperty = "zonewrightClientContent"
clientContentKinds = ("spawn", "door", "object", "zone", "zoneFile")
boundaryProperty = "zonewrightBoundary"
zoneLineProperty = "zonewrightZoneLine"
# What players pass through: an object marked passable (markPassable), a face an imported client file flags passable (this face
# attribute), and a face of a cutout or liquid material (createMaterial, createLiquidMaterial; export flags them). A liquid material
# keeps its liquid and shader values, which export writes as the client's shader properties; a client liquid material keeps only its
# liquid, as whether players pass through it is its file's flags.
passableProperty = "zonewrightPassable"
passableAttribute = "zonewrightPassable"
cutoutProperty = "zonewrightCutout"
liquidProperty = "zonewrightLiquid"
clientLiquidProperty = "zonewrightClientLiquid"
swumLiquids = ("water", "lava")
waterReach = 100000.0
up = mathutils.Vector((0.0, 0.0, 1.0))
down = mathutils.Vector((0.0, 0.0, -1.0))
# Casts from a surface start this far off it, so they do not meet the face they start on.
castNudge = 0.01
# Ground inside a solid is told by level casts this far above it, clear of the ground's own rises, every sixteenth of a turn.
enclosureProbeHeight = 1.0
aroundDirections = [mathutils.Vector((math.cos(turn * math.pi / 8), math.sin(turn * math.pi / 8), 0.0)) for turn in range(16)]


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


def isPlayerSolid(sceneObject, collision=False):
  """Whether players stand on and are blocked by an object: rendered meshes and collection instances, but not objects marked passable,
  guides, plot borders, regions, water bodies (swum, not stood on), spawns (players pass through them), or doors (taken as open); of
  its faces, only those players do not pass through (meshFaces). With collision, as the client collides: boundaries, never drawn,
  block too."""
  if collision and boundaryProperty in sceneObject:
    return sceneObject.type == "MESH"
  if passableProperty in sceneObject or sceneObject.hide_render or isDesignAid(sceneObject) or regionIntentProperty in sceneObject or waterProperty in sceneObject:
    return False
  if sceneObject.get(clientContentProperty) in ("spawn", "door"):
    return False
  return sceneObject.type == "MESH" or isCollectionInstance(sceneObject)


def collectionParts(collection):
  """Each rendered mesh a collection draws where it is instanced, with its matrix from the collection's instance_offset: its own meshes
  and, to any depth, those of the collections its instance members instance (a prefab's part holding placed pieces)."""
  offset = mathutils.Matrix.Translation(-collection.instance_offset)
  parts = []
  for member in collection.all_objects:
    if member.hide_render:
      continue
    if member.type == "MESH":
      parts.append((member, offset @ member.matrix_world))
    elif isCollectionInstance(member):
      parts.extend((mesh, offset @ member.matrix_world @ matrix) for mesh, matrix in collectionParts(member.instance_collection))
  return parts


def objectParts(sceneObject):
  """A mesh with its world matrix, or each rendered mesh of a collection instance as placed, nested instances included."""
  if sceneObject.type == "MESH":
    return [(sceneObject, sceneObject.matrix_world.copy())]
  if isCollectionInstance(sceneObject):
    return [(member, sceneObject.matrix_world @ matrix) for member, matrix in collectionParts(sceneObject.instance_collection)]
  raise ValueError(f"'{sceneObject.name}' is a {sceneObject.type}; it has no mesh")


def worldBoundsCorners(sceneObject, depsgraph):
  """The world corners of the evaluated bounding boxes of an object's meshes (objectParts)."""
  return [matrix @ mathutils.Vector(corner) for part, matrix in objectParts(sceneObject) for corner in part.evaluated_get(depsgraph).bound_box]


def playerSolidObjects(excluding=(), collision=False):
  """The objects players stand on and are blocked by (isPlayerSolid), leaving out the objects named in excluding."""
  # An object moved or made since the last evaluation still holds its old world matrix until the scene is evaluated.
  bpy.context.view_layer.update()
  return [sceneObject for sceneObject in bpy.context.scene.objects if isPlayerSolid(sceneObject, collision) and sceneObject.name not in excluding]


def isPassableMaterial(material):
  """Players pass through liquid surfaces (water, waterfall, lava) and cutout cards, as the client's own zones flag them (0x1)."""
  return material is not None and (liquidProperty in material or bool(material.get(cutoutProperty)))


class MeshFaces(typing.NamedTuple):
  """An evaluated mesh's vertex positions in its own space, its triangles, each triangle's material (an index into materials, where
  len(materials) is none), and which triangles players pass through."""
  positions: numpy.ndarray
  triangles: numpy.ndarray
  slots: numpy.ndarray
  materials: list
  passable: numpy.ndarray


def meshFaces(sceneObject, depsgraph):
  """A mesh's MeshFaces: players pass through all of a mesh marked passable, and otherwise the faces of a liquid or cutout material
  and those the client file it came from flags passable."""
  evaluated = sceneObject.evaluated_get(depsgraph)
  mesh = evaluated.to_mesh()
  try:
    mesh.calc_loop_triangles()
    positions = numpy.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", positions)
    triangles = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("vertices", triangles)
    polygons = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
    mesh.loop_triangles.foreach_get("polygon_index", polygons)
    faceSlots = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
    mesh.polygons.foreach_get("material_index", faceSlots)
    flagged = numpy.zeros(len(mesh.polygons), dtype=bool)
    attribute = mesh.attributes.get(passableAttribute)
    if attribute is not None:
      attribute.data.foreach_get("value", flagged)
    materials = [slot.material for slot in evaluated.material_slots]
  finally:
    evaluated.to_mesh_clear()
  slots = numpy.minimum(faceSlots[polygons], len(materials))
  passableSlots = numpy.array([isPassableMaterial(material) for material in materials] + [False], dtype=bool)
  passable = passableSlots[slots] | flagged[polygons] | (passableProperty in sceneObject)
  return MeshFaces(positions.reshape(-1, 3), triangles.reshape(-1, 3), slots, materials, passable)


def solidFaces(faces):
  """Which of a mesh's triangles (MeshFaces) players do not pass through."""
  return ~faces.passable


def isSwumMaterial(material):
  """A client liquid players swim under where its file lets them through: water or lava, not a waterfall."""
  return material is not None and material.get(clientLiquidProperty) in swumLiquids


def swumFaces(faces):
  """Which of a mesh's triangles (MeshFaces) are a client liquid's surface players swim under (isSwumMaterial, passable)."""
  return numpy.array([isSwumMaterial(material) for material in faces.materials] + [False], dtype=bool)[faces.slots] & faces.passable


def solidTrees(owners):
  """(owner name, world matrix, BVH tree in the mesh's own space) for each of the objects' meshes (objectParts) over the faces players
  do not pass through (meshFaces), leaving out meshes players pass through whole."""
  bpy.context.view_layer.update()
  depsgraph = bpy.context.evaluated_depsgraph_get()
  trees = []
  for owner in owners:
    for part, matrix in objectParts(owner):
      faces = meshFaces(part, depsgraph)
      solid = solidFaces(faces)
      if solid.all():
        trees.append((owner.name, matrix, mathutils.bvhtree.BVHTree.FromObject(part, depsgraph)))
      elif solid.any():
        trees.append((owner.name, matrix, mathutils.bvhtree.BVHTree.FromPolygons(faces.positions.tolist(), faces.triangles[solid].tolist())))
  return trees


def wholeTrees(owners):
  """(owner name, world matrix, BVH tree in the mesh's own space) for each of the objects' meshes (objectParts), every face of it."""
  bpy.context.view_layer.update()
  depsgraph = bpy.context.evaluated_depsgraph_get()
  return [(owner.name, matrix, mathutils.bvhtree.BVHTree.FromObject(part, depsgraph)) for owner in owners for part, matrix in objectParts(owner)]


def selectedTriangles(owners, select):
  """The world positions of the objects' meshes (objectParts) and the triangles select(MeshFaces) picks of them, all in one."""
  bpy.context.view_layer.update()
  depsgraph = bpy.context.evaluated_depsgraph_get()
  positions, triangles, offset = [], [], 0
  for owner in owners:
    for part, worldMatrix in objectParts(owner):
      faces = meshFaces(part, depsgraph)
      matrix = matrixArray(worldMatrix)
      positions.append(faces.positions @ matrix[:3, :3].T + matrix[:3, 3])
      triangles.append(faces.triangles[select(faces)] + offset)
      offset += len(positions[-1])
  if not positions:
    return numpy.zeros((0, 3)), numpy.zeros((0, 3), dtype=numpy.int64)
  return numpy.concatenate(positions), numpy.concatenate(triangles)


def playerSolidTriangles(excluding=()):
  """The world positions and triangles players stand on and are blocked by (playerSolidObjects, solidFaces), leaving out the objects
  named in excluding."""
  positions, triangles = selectedTriangles(playerSolidObjects(excluding), solidFaces)
  if not len(triangles):
    raise ValueError("The scene has nothing players stand on besides water, guides, regions, spawns, doors, and what they pass through")
  return positions, triangles


class Footing(typing.NamedTuple):
  """Where a player stands: the point, its face's normal, its object, and the first face above it whichever way it faces (point, normal)."""
  point: mathutils.Vector
  normal: mathutils.Vector
  objectName: str
  overhead: tuple | None


class PlayerSurfaces:
  """Ray casts against what players stand on and are blocked by, never the faces they pass through (solidTrees): playerSolidObjects
  but excluding; or against objects named as the surfaces to use, whole, whatever players do there (wholeTrees); or given (object
  name, world matrix, BVH tree) trees."""

  def __init__(self, excluding=(), objects=None, trees=None):
    if trees is None:
      trees = solidTrees(playerSolidObjects(excluding)) if objects is None else wholeTrees([sceneObject for sceneObject in objects if sceneObject.name not in excluding])
    self.members = [(name, matrix, matrix.inverted(), tree) for name, matrix, tree in trees]
    if not self.members:
      what = "the named objects have no meshes" if objects is not None else "the scene has nothing players stand on besides water, guides, regions, spawns, doors, and what they pass through"
      raise ValueError(f"Nothing to cast against: {what}" + (f" once {sorted(excluding)} are left out" if excluding else ""))

  def cast(self, origin, direction, distance):
    """The nearest world hit point within distance, or None."""
    hit = self.castWithNormal(origin, direction, distance)
    return hit[0] if hit else None

  def footingBelow(self, origin, distance):
    """The point of footingOn, or None."""
    footing = self.footingOn(origin, distance)
    return footing.point if footing else None

  def footingOn(self, origin, distance):
    """The first up-facing surface below origin within distance that is not ground inside a solid, as a Footing, or None."""
    while True:
      hit = self.castOn(origin, down, distance)
      if hit is None:
        return None
      point, normal, name = hit
      if normal.z > 0:
        overhead = self.castWithNormal(point + up * castNudge, up, waterReach)
        # A face above met from behind is the top of a solid around the point, or one-sided cover over open ground (a roof plane, a leaf
        # card, a deck): only the ways round it tell which.
        if overhead is None or overhead[1].z <= 0 or not self.enclosedAround(point + up * enclosureProbeHeight):
          return Footing(point, normal, name, overhead)
      distance -= origin.z - point.z + castNudge
      origin = point + down * castNudge

  def enclosedAround(self, point):
    """Whether most level ways out of point meet a face from behind first, as from inside a solid."""
    # Most, not every: client meshes are one-sided and seldom closed, so from under a terrace whose only faces are its floor and its
    # outer walls a few ways out still meet something in front.
    behind, elsewhere = 0, 0
    for direction in aroundDirections:
      hit = self.castWithNormal(point, direction, waterReach)
      if hit is not None and hit[1].dot(direction) > 0:
        behind += 1
      else:
        elsewhere += 1
      if 2 * behind > len(aroundDirections) or 2 * elsewhere >= len(aroundDirections):
        break
    return 2 * behind > len(aroundDirections)

  def castWithNormal(self, origin, direction, distance):
    """The nearest world hit point within distance and the normal of the face hit there, or None."""
    hit = self.castOn(origin, direction, distance)
    return hit[:2] if hit else None

  def groundAtLevel(self, x, y, level):
    """The ground at [x, y] for a player at `level` (a plot's): where the point levelProbeLift over the level lies inside a solid, the
    top of it (ground standing above the level); otherwise the footing under that point, so rock over a cave's floor is never taken for
    its ground. None where neither is found."""
    origin = mathutils.Vector((x, y, level + levelProbeLift))
    above = self.castWithNormal(origin, up, waterReach)
    if above is not None and above[1].z > 0 and self.enclosedAround(origin):
      return above[0].z
    footing = self.footingBelow(origin, waterReach)
    return None if footing is None else footing.z

  def castOn(self, origin, direction, distance):
    """castWithNormal's hit with the name of the object it belongs to, or None."""
    nearest = None
    for name, matrix, inverse, tree in self.members:
      location, normal, _, _ = tree.ray_cast(inverse @ origin, (inverse.to_3x3() @ direction).normalized())
      if location is None:
        continue
      hit = matrix @ location
      along = (hit - origin).length
      if along <= distance and (nearest is None or along < nearest[0]):
        nearest = (along, hit, (matrix.to_3x3().inverted().transposed() @ normal).normalized(), name)
    return nearest[1:] if nearest else None


def rockOverGround(castWithNormal, x, y, top):
  """Looking down at [x, y] from `top` (castWithNormal(origin, direction, distance) gives (point, normal) or None): where the highest
  ground is the top of rock with room for a player between its underside and ground under it (a hill over a cave, an overhang), the
  heights of the top, the underside, and that ground; None where the column holds one level of ground (a block resting on the ground
  is one)."""
  highest = castWithNormal(mathutils.Vector((x, y, top)), down, waterReach)
  if highest is None:
    return None
  underside = castWithNormal(highest[0] + down * castNudge, down, waterReach)
  if underside is None or underside[1].z >= 0:
    return None
  floor = castWithNormal(underside[0] + down * castNudge, down, waterReach)
  if floor is None or underside[0].z - floor[0].z < playerScale.playerHeight:
    return None
  return highest[0].z, underside[0].z, floor[0].z


def overGroundOn(surfaces, x, y, top):
  """rockOverGround on PlayerSurfaces, with the name of the object whose underside stands over the ground: its levels and that name, or
  None."""
  levels = rockOverGround(surfaces.castWithNormal, x, y, top)
  if levels is None:
    return None
  over = surfaces.castOn(mathutils.Vector((x, y, levels[1] - castNudge)), up, levels[0] - levels[1] + 2 * castNudge)
  return levels, None if over is None else over[2]


def describeRockOverGround(where, levels, name=None):
  """A refusal's account of rock over ground, naming the object over the ground when known (a placed roof, a hill over a cave)."""
  top, underside, floor = (round(level, 1) + 0.0 for level in levels)
  if name is None:
    return (
      f"rock lies over ground at {where}: the highest ground there, at {top:.1f}, is the top of rock whose underside is at {underside:.1f},"
      f" over ground at {floor:.1f} (a cave or an overhang)"
    )
  return f"'{name}' stands over the ground at {where}: the highest ground there, at {top:.1f}, is the top of '{name}', whose underside, at {underside:.1f}, stands over ground at {floor:.1f}"


def swimSurfaces(leftOut=(), added=None):
  """A BVH over the surfaces players swim under: rendered pools and rivers (falls are not swum) and imported client liquids
  (swumFaces), but for the bodies named in leftOut, and with `added`, a body's surface as it is about to stand ((world positions,
  polygons)); None when there are none."""
  bodies = [
    sceneObject for sceneObject in bpy.context.scene.objects
    if waterProperty in sceneObject and not sceneObject.hide_render and json.loads(sceneObject[waterProperty])["kind"] != "fall" and sceneObject.name not in leftOut
  ]
  liquidOwners = [owner for owner in playerSolidObjects(leftOut) if any(isSwumMaterial(slot.material) for part, _ in objectParts(owner) for slot in part.material_slots)]
  positions, triangles = selectedTriangles(liquidOwners, swumFaces)
  bodyPositions, bodyTriangles = worldTriangles(bodies)
  polygons = numpy.concatenate([triangles, bodyTriangles + len(positions)]).tolist()
  positions = numpy.concatenate([positions, bodyPositions]).tolist()
  if added is not None:
    polygons += [[index + len(positions) for index in polygon] for polygon in added[1]]
    positions += list(added[0])
  return mathutils.bvhtree.BVHTree.FromPolygons(positions, polygons) if polygons else None


def waterDepthAt(water, ground, point):
  """How far a pool or river's surface (water, from swimSurfaces) stands above a point with nothing players stand on (ground,
  PlayerSurfaces) between, or None where no water lies above it or something covers the point first, as a floating pool's basin does."""
  if water is None:
    return None
  origin = mathutils.Vector(point)
  location, _, _, _ = water.ray_cast(origin, up, waterReach)
  if location is None:
    return None
  depth = location.z - origin.z
  return None if ground.cast(origin + up * castNudge, up, depth) is not None else depth


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


# A point this close to a water surface lies on it: a vertex cut onto the waterline is at the level, not above it.
waterlineTolerance = 1e-3
waterlineHalvings = 20


class WaterSurface:
  """A pool or river's surface in plan: what lies under it, its height, and the waterline where a mesh crosses it."""

  def __init__(self, waterObject):
    if json.loads(waterObject[waterProperty])["kind"] == "fall":
      raise ValueError(f"'{waterObject.name}' is a fall; it has no waterline, bed, or banks")
    self.name = waterObject.name
    positions, triangles = worldTriangles([waterObject])
    first, second, third = (positions[triangles[:, corner]] for corner in range(3))
    normals = numpy.cross(second - first, third - first)
    # A triangle of a clipped cell can come out without area; it has no plane to give a height.
    triangles, normals, first = (values[numpy.abs(normals[:, 2]) > 1e-9] for values in (triangles, normals, first))
    self.slopes = -normals[:, :2] / normals[:, 2:]
    self.offsets = first[:, 2] - (self.slopes * first[:, :2]).sum(axis=1)
    flat = positions.copy()
    flat[:, 2] = 0.0
    self.plan = mathutils.bvhtree.BVHTree.FromPolygons(flat.tolist(), triangles.tolist())
    self.low, self.high = positions[:, :2].min(0), positions[:, :2].max(0)

  def heights(self, points):
    """For [x, y] points: the height of the surface's plane nearest each in plan, and how far off the surface each lies in plan."""
    faces, apart = numpy.empty(len(points), dtype=numpy.int64), numpy.empty(len(points))
    for index, (x, y) in enumerate(points[:, :2].tolist()):
      _, _, faces[index], apart[index] = self.plan.find_nearest(mathutils.Vector((x, y, 0.0)))
    return (self.slopes[faces] * points[:, :2]).sum(axis=1) + self.offsets[faces], apart

  def nearby(self, points, margin):
    return ((points[:, :2] >= self.low - margin) & (points[:, :2] <= self.high + margin)).all(axis=1)

  def rise(self, positions):
    """How far each point stands over the surface (negative under it), NaN where the surface does not lie over it."""
    rise = numpy.full(len(positions), numpy.nan)
    candidates = numpy.flatnonzero(self.nearby(positions, waterlineTolerance))
    heights, apart = self.heights(positions[candidates])
    rise[candidates] = numpy.where(apart <= waterlineTolerance, positions[candidates, 2] - heights, numpy.nan)
    return rise

  def underMask(self, positions):
    """Which points lie under the surface or on it."""
    return numpy.nan_to_num(self.rise(positions), nan=numpy.inf) <= waterlineTolerance

  def above(self, positions):
    """How far each point stands over the plane of the surface nearest it in plan, on or off the surface."""
    return positions[:, 2] - self.heights(positions)[0]

  def crossingFractions(self, starts, ends):
    """Where segments from one side of the surface to the other cross it, as the fraction along each, by halving."""
    low, high = numpy.zeros(len(starts)), numpy.ones(len(starts))
    startsBelow = self.above(starts) < 0
    for _ in range(waterlineHalvings):
      middle = (low + high) / 2
      sameSide = (self.above(starts + middle[:, None] * (ends - starts)) < 0) == startsBelow
      low, high = numpy.where(sameSide, middle, low), numpy.where(sameSide, high, middle)
    return (low + high) / 2

  def waterline(self, positions, triangles):
    """Where triangles of a mesh cross the surface over them, as plan segments (k x 2 x 2): the shore players see."""
    nearTriangles = triangles[self.nearby(positions, waterlineTolerance)[triangles].any(axis=1)]
    used = numpy.unique(nearTriangles)
    values = numpy.zeros(len(positions))
    values[used] = self.above(positions[used])
    # A vertex cut onto the waterline is on it, not a hair to either side, so a cut mesh gives the same line as before the cut.
    sides = numpy.sign(numpy.where(numpy.abs(values) <= waterlineTolerance, 0.0, values))
    edges = nearTriangles[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 3, 2)
    straddling = sides[edges[:, :, 0]] * sides[edges[:, :, 1]] < 0
    pairs, which = numpy.unique(numpy.sort(edges[straddling], axis=1), axis=0, return_inverse=True)
    fractions = self.crossingFractions(positions[pairs[:, 0]], positions[pairs[:, 1]])
    crossings = numpy.zeros(edges.shape)
    crossings[straddling] = (positions[pairs[:, 0], :2] + fractions[:, None] * (positions[pairs[:, 1], :2] - positions[pairs[:, 0], :2]))[which.reshape(-1)]
    points = numpy.stack([positions[nearTriangles][:, :, :2], crossings], axis=2).reshape(-1, 6, 2)
    onLine = numpy.stack([sides[nearTriangles] == 0, straddling], axis=2).reshape(-1, 6)
    paired = onLine.sum(axis=1) == 2
    segments = points[paired][onLine[paired]].reshape(-1, 2, 2)
    segments = segments[numpy.linalg.norm(segments[:, 1] - segments[:, 0], axis=1) > waterlineTolerance]
    if len(segments):
      _, apart = self.heights(segments.mean(axis=1))
      segments = segments[apart <= waterlineTolerance]
    return segments


def meshTriangles(sceneObject):
  """The mesh's faces split into triangles, as vertex indices."""
  mesh = sceneObject.data
  mesh.calc_loop_triangles()
  corners = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("vertices", corners)
  return corners.reshape(-1, 3)


def waterlineDistances(segments, points):
  """Each point's distance in plan from a waterline (WaterSurface.waterline), infinite where there is none."""
  if not len(segments):
    return numpy.full(len(points), numpy.inf)
  flat = numpy.zeros((len(points), 3))
  flat[:, :2] = points[:, :2]
  return waterlineBorder(segments).distances(flat)


def waterlineBorder(segments):
  flat = numpy.zeros((len(segments), 2, 3))
  flat[:, :, :2] = segments
  return BorderDistance(flat[:, 0], flat[:, 1])


def cornerCounts(sceneObject, vertexMask):
  """For each face, how many of its corners a vertex mask holds, and how many corners it has."""
  loopTotals, loopVertices = faceLoops(sceneObject)
  return numpy.add.reduceat(vertexMask[loopVertices].astype(numpy.int64), numpy.cumsum(loopTotals) - loopTotals), loopTotals


def underWaterMask(waterObject, sceneObject, elementKind):
  """The vertices under a pool or river's surface or on it, or the faces all of whose corners are: its bed."""
  positions, _ = readVertexArrays(sceneObject)
  under = WaterSurface(waterObject).underMask(positions)
  if elementKind == "vertices":
    return under
  counts, totals = cornerCounts(sceneObject, under)
  return counts == totals


def nearWaterMask(waterObject, sceneObject, elementKind, distance):
  """The vertices out of a pool or river within distance in plan of its waterline, or the faces the waterline crosses or meets and those all of whose corners are within distance: its wet banks."""
  if distance <= 0:
    raise ValueError(f"The nearWater selector's distance must be positive, got {distance}")
  surface = WaterSurface(waterObject)
  positions, _ = readVertexArrays(sceneObject)
  under = surface.underMask(positions)
  segments = surface.waterline(positions, meshTriangles(sceneObject))
  near = numpy.zeros(len(positions), dtype=bool)
  if len(segments):
    low, high = segments.reshape(-1, 2).min(0) - distance, segments.reshape(-1, 2).max(0) + distance
    candidates = numpy.flatnonzero(~under & ((positions[:, :2] >= low) & (positions[:, :2] <= high)).all(axis=1))
    near[candidates] = waterlineDistances(segments, positions[candidates]) <= distance + waterlineTolerance
  if elementKind == "vertices":
    return near
  underCounts, totals = cornerCounts(sceneObject, under)
  nearCounts, _ = cornerCounts(sceneObject, near)
  return (underCounts < totals) & ((underCounts > 0) | (nearCounts == totals))


def requireMeshObject(name):
  sceneObject = requireObject(name)
  if sceneObject.type != "MESH":
    raise ValueError(f"'{name}' is a {sceneObject.type}, not a mesh")
  return sceneObject


def requireEditableMesh(name, action):
  """A mesh the tool named by action may change: not part of a structure laid from its definition."""
  sceneObject = requireMeshObject(name)
  bridgeStructureData.requireNotStructurePart(sceneObject, action)
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


class TextureAreas(typing.NamedTuple):
  """Each triangle's world area, its area as box projection maps it at one unit a repeat, its UV area, and its material slot."""
  world: numpy.ndarray
  box: numpy.ndarray
  uv: numpy.ndarray
  materials: numpy.ndarray


def textureAreas(sceneObject):
  """The mesh's TextureAreas on its active UV layer, or None when it has no UV layer."""
  mesh = sceneObject.data
  if not mesh.uv_layers:
    return None
  mesh.calc_loop_triangles()
  triangleLoops = numpy.empty(len(mesh.loop_triangles) * 3, dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("loops", triangleLoops)
  triangleLoops = triangleLoops.reshape(-1, 3)
  materialIndices = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("material_index", materialIndices)
  trianglePolygons = numpy.empty(len(mesh.loop_triangles), dtype=numpy.int64)
  mesh.loop_triangles.foreach_get("polygon_index", trianglePolygons)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  positions, _ = readVertexArrays(sceneObject)
  _, faceNormals, _ = readFaceArrays(sceneObject)
  uvs = numpy.empty(len(mesh.loops) * 2)
  mesh.uv_layers.active.data.foreach_get("uv", uvs)
  corners = positions[loopVertices[triangleLoops]]
  crossProducts = numpy.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0])
  # Box projection maps each face onto the plane across its normal's largest world axis, as projectUVs does.
  boxAxes = numpy.abs(faceNormals).argmax(axis=1)[trianglePolygons]
  uvCorners = uvs.reshape(-1, 2)[triangleLoops]
  uvEdgeA, uvEdgeB = uvCorners[:, 1] - uvCorners[:, 0], uvCorners[:, 2] - uvCorners[:, 0]
  return TextureAreas(
    numpy.linalg.norm(crossProducts, axis=1) / 2, numpy.abs(crossProducts[numpy.arange(len(crossProducts)), boxAxes]) / 2,
    numpy.abs(uvEdgeA[:, 0] * uvEdgeB[:, 1] - uvEdgeA[:, 1] * uvEdgeB[:, 0]) / 2, materialIndices,
  )


def worldUnitsPerRepeat(worldArea, uvArea):
  """How many world units one texture repeat spans over faces of these areas; None when they have no UV area."""
  return math.sqrt(worldArea / uvArea) if uvArea > 0 else None


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
  """Faces a move turned over: their normal now points against where it pointed. A cave's own faces are left out: its lining holds still
  while the ground around its mouth moves, so its first row stretches rather than folds."""
  return int((((faceNormals(sceneObject, before) * faceNormals(sceneObject, after)).sum(1) < 0) & ~bridgeCaveData.caveFaceMask(sceneObject)).sum())


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
  if key == "underWater":
    return underWaterMask(requireWater(value), sceneObject, elementKind)
  if key == "nearWater":
    return nearWaterMask(requireWater(value["water"]), sceneObject, elementKind, value["distance"])
  if key == "cave":
    return bridgeCaveData.liningSelection(sceneObject, value, elementKind)
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


def tracedChains(segments, joins):
  """Border edges [vertex, vertex] joined end to end, on through a vertex two meet at or that `joins` pairs: (vertices, edges, closed)."""
  touching = {}
  for index, (start, end) in enumerate(segments.tolist()):
    touching.setdefault(start, []).append(index)
    touching.setdefault(end, []).append(index)

  def onward(vertex, arriving):
    if vertex in joins:
      return joins[vertex].get(arriving)
    others = touching[vertex]
    return (others[1] if others[0] == arriving else others[0]) if len(others) == 2 else None

  used = numpy.zeros(len(segments), dtype=bool)
  chains = []
  for seed in range(len(segments)):
    if used[seed]:
      continue
    used[seed] = True
    start, end = (int(vertex) for vertex in segments[seed])
    vertices, indices, closed = [start, end], [seed], False
    for forward in (True, False):
      segment, tip = seed, (end if forward else start)
      while not closed:
        following = onward(tip, segment)
        if following is None:
          break
        if following == seed:
          closed = True
          break
        if used[following]:
          raise RuntimeError(f"Border edge {following} joins two chains")
        used[following] = True
        segment = following
        tip = int(segments[segment][1] if segments[segment][0] == tip else segments[segment][0])
        if forward:
          vertices.append(tip)
          indices.append(segment)
        else:
          vertices.insert(0, tip)
          indices.insert(0, segment)
    chains.append((vertices, indices, closed))
  return chains


def footBorders(sceneObject, side, other):
  """The edges where side faces meet other faces along the chains of them where side mostly rises above other: a wall's foot, not its lip."""
  borderEdges, sideFaces, otherFaces = faceBorders(sceneObject, side, other)
  heights = readFaceArrays(sceneObject)[0][:, 2]
  positions, _ = readVertexArrays(sceneObject)
  segments = meshEdges(sceneObject.data)[borderEdges]
  rising = numpy.where(heights[sideFaces] > heights[otherFaces], 1.0, -1.0) * numpy.linalg.norm(positions[segments[:, 1]] - positions[segments[:, 0]], axis=1)
  # A notch of ground in a wall's foot runs down beside rock for a step: the foot is decided chain by chain, so it stays whole there.
  foot = numpy.zeros(len(borderEdges), dtype=bool)
  for _, indices, _ in tracedChains(segments, {}):
    foot[indices] = rising[indices].sum() > 0
  return borderEdges[foot], sideFaces[foot], otherFaces[foot]


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

  def distances(self, points):
    """The distance from each of many points to the border, as nearest gives it."""
    owners = self.owners.tolist()
    pointIndices, segmentIndices = [], []
    for index, point in enumerate(points.tolist()):
      _, _, sampled = self.tree.find(point)
      segments = {owners[sample] for _, sample, _ in self.tree.find_range(point, sampled + self.reach)}
      pointIndices.extend([index] * len(segments))
      segmentIndices.extend(segments)
    pointIndices, segmentIndices = numpy.array(pointIndices, dtype=numpy.int64), numpy.array(segmentIndices, dtype=numpy.int64)
    offsets, spans = points[pointIndices] - self.starts[segmentIndices], self.spans[segmentIndices]
    fractions = numpy.clip((offsets * spans).sum(axis=1) / numpy.maximum((spans * spans).sum(axis=1), 1e-12), 0, 1)
    distances = numpy.full(len(points), numpy.inf)
    numpy.minimum.at(distances, pointIndices, numpy.linalg.norm(offsets - fractions[:, None] * spans, axis=1))
    return distances


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


def storeSplitBMesh(meshEditor, sceneObject):
  """Store a mesh whose edges and faces were only split or turned: bmesh places a split's new vertex alike in every shaping pass, so the passes stay."""
  meshEditor.normal_update()
  meshEditor.to_mesh(sceneObject.data)
  meshEditor.free()
  sceneObject.data.update()


def storeBMesh(meshEditor, sceneObject):
  if hasShapingPasses(sceneObject) or bridgeCaveData.holdsCaves(sceneObject):
    meshEditor.free()
    requireNoShapingPasses(sceneObject, "change its faces")
    bridgeCaveData.requireNoCaves(sceneObject, "change its faces")
  meshEditor.normal_update()
  meshEditor.to_mesh(sceneObject.data)
  meshEditor.free()
  sceneObject.data.update()


def triangleCount(sceneObject):
  return sum(len(polygon.vertices) - 2 for polygon in sceneObject.data.polygons)


def meshCounts(sceneObject):
  mesh = sceneObject.data
  return {"vertices": len(mesh.vertices), "faces": len(mesh.polygons), "triangles": triangleCount(sceneObject)}


def rayCast(origin, direction, distance):
  """Nearest hit along a ray in the open scene; None when nothing is hit within distance."""
  depsgraph = bpy.context.evaluated_depsgraph_get()
  hit, location, normal, faceIndex, hitObject, _ = bpy.context.scene.ray_cast(depsgraph, mathutils.Vector(origin), mathutils.Vector(direction).normalized(), distance=distance)
  return (location, normal, faceIndex, hitObject) if hit else None


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
