"""World-space mesh access and selectors: which vertices, edges, or faces of a mesh an operation touches. Runs under Blender's Python."""
import contextlib
import math

import bmesh
import bpy
import mathutils
import numpy

selectorKeys = ("all", "sphere", "box", "cylinder", "facing", "material", "vertexGroup", "and", "or", "not")


def requireObject(name):
  # World matrices of objects created or moved since the last evaluation are stale until the view layer updates.
  bpy.context.view_layer.update()
  sceneObject = bpy.context.scene.objects.get(name)
  if sceneObject is None:
    raise ValueError(f"No object named '{name}' in scene '{bpy.context.scene.name}'")
  return sceneObject


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


def readVertexArrays(sceneObject):
  mesh = sceneObject.data
  count = len(mesh.vertices)
  coordinates = numpy.empty(count * 3)
  mesh.vertices.foreach_get("co", coordinates)
  normals = numpy.empty(count * 3)
  mesh.vertices.foreach_get("normal", normals)
  return worldPositions(sceneObject, coordinates.reshape(-1, 3)), worldDirections(sceneObject, normals.reshape(-1, 3))


def readFaceArrays(sceneObject):
  mesh = sceneObject.data
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


def faceVertexIndices(sceneObject):
  mesh = sceneObject.data
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopVertices = numpy.empty(len(mesh.loops), dtype=numpy.int32)
  mesh.loops.foreach_get("vertex_index", loopVertices)
  return numpy.split(loopVertices, numpy.cumsum(loopTotals)[:-1])


def evaluateSelector(selector, sceneObject, elementKind):
  """A boolean mask over the object's vertices or faces. Shapes test vertex positions or face centers in world units."""
  if not isinstance(selector, dict) or len(selector) != 1 or next(iter(selector)) not in selectorKeys:
    raise ValueError(f"A selector is one of {list(selectorKeys)} as a single-key object, got {selector!r}")
  key, value = next(iter(selector.items()))
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
  if key == "material":
    slotIndex = materialSlotIndex(sceneObject, value)
    if elementKind == "faces":
      return materialIndices == slotIndex
    faceMask = readFaceArrays(sceneObject)[2] == slotIndex
    vertexMask = numpy.zeros(len(positions), dtype=bool)
    for faceVertices in numpy.array(faceVertexIndices(sceneObject), dtype=object)[faceMask]:
      vertexMask[faceVertices] = True
    return vertexMask
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


def sceneTopHeight():
  heights = [(sceneObject.matrix_world @ mathutils.Vector(corner)).z for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" for corner in sceneObject.bound_box]
  if not heights:
    raise ValueError("The scene has no meshes")
  return max(heights)
