"""Shaping meshes: vertex moves, sculpting, and topology edits, all addressed by selectors in world units. Runs under Blender's Python."""
import math

import bmesh
import bpy
import mathutils
import numpy

import bridgeMeshAccess

falloffCurves = ("constant", "linear", "smooth", "sharp")
sculptModes = ("raise", "lower", "smooth", "flatten", "crease", "carve")
fractionModes = ("smooth", "flatten", "carve")
booleanOperations = ("DIFFERENCE", "UNION", "INTERSECT")
creasePinch = 0.25


def falloffWeights(normalizedDistances, curve):
  if curve not in falloffCurves:
    raise ValueError(f"falloff curve must be one of {list(falloffCurves)}, got '{curve}'")
  closeness = numpy.clip(1 - normalizedDistances, 0, 1)
  if curve == "constant":
    return (normalizedDistances <= 1).astype(numpy.float64)
  if curve == "linear":
    return closeness
  if curve == "smooth":
    return closeness * closeness * (3 - 2 * closeness)
  return closeness * closeness


def writeWorldPositions(sceneObject, worldPositions):
  sceneObject.data.vertices.foreach_set("co", bridgeMeshAccess.localPositions(sceneObject, worldPositions).ravel())
  sceneObject.data.update()


def moveVertices(objectName, selector, offset, falloff):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "vertices")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = mask.astype(numpy.float64)
  if falloff is not None:
    distances = numpy.linalg.norm(positions - bridgeMeshAccess.toArray(falloff["center"]), axis=1) / falloff["radius"]
    weights *= falloffWeights(distances, falloff["curve"])
  displacement = weights[:, None] * bridgeMeshAccess.toArray(offset)
  writeWorldPositions(sceneObject, positions + displacement)
  return {"movedVertices": int((weights > 0).sum()), "largestMove": round(float(numpy.linalg.norm(displacement, axis=1).max()), 3)}


def distancesToPolyline(points, path, horizontal):
  """Distance from each point to a polyline, and the interpolated path height at the nearest spot."""
  pathArray = bridgeMeshAccess.toArray(path)
  axes = slice(0, 2) if horizontal else slice(0, 3)
  starts, ends = pathArray[:-1], pathArray[1:]
  segments = ends[:, axes] - starts[:, axes]
  lengths = numpy.maximum((segments * segments).sum(1), 1e-12)
  offsets = points[:, None, axes] - starts[None, :, axes]
  along = numpy.clip((offsets * segments[None]).sum(2) / lengths[None], 0, 1)
  nearest = starts[None, :, axes] + along[:, :, None] * segments[None]
  distances = numpy.linalg.norm(points[:, None, axes] - nearest, axis=2)
  closest = distances.argmin(1)
  rows = numpy.arange(len(points))
  heights = starts[closest, 2] + along[rows, closest] * (ends[closest, 2] - starts[closest, 2])
  return distances[rows, closest], heights


def vertexNeighbourAverages(sceneObject, positions):
  edges = numpy.empty(len(sceneObject.data.edges) * 2, dtype=numpy.int64)
  sceneObject.data.edges.foreach_get("vertices", edges)
  edges = edges.reshape(-1, 2)
  sums = numpy.zeros_like(positions)
  counts = numpy.zeros(len(positions))
  numpy.add.at(sums, edges[:, 0], positions[edges[:, 1]])
  numpy.add.at(sums, edges[:, 1], positions[edges[:, 0]])
  numpy.add.at(counts, edges[:, 0], 1)
  numpy.add.at(counts, edges[:, 1], 1)
  return numpy.divide(sums, counts[:, None], out=positions.copy(), where=counts[:, None] > 0)


def sculpt(objectName, mode, distances, nearestPoints, radius, strength, curve, direction, iterations, carveTargets):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if mode not in sculptModes:
    raise ValueError(f"mode must be one of {list(sculptModes)}, got '{mode}'")
  if mode in fractionModes and not 0 < strength <= 1:
    raise ValueError(f"{mode} strength is a fraction in (0, 1], got {strength}")
  if mode not in fractionModes and strength <= 0:
    raise ValueError(f"{mode} strength is a distance in units and must be positive, got {strength}")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = falloffWeights(distances(positions) / radius, "sharp" if mode == "crease" else curve)
  affected = weights > 0
  if not affected.any():
    raise ValueError(f"No vertices of '{objectName}' lie within {radius} units of the stroke")
  regionNormal = (normals[affected] * weights[affected, None]).sum(0)
  regionNormal /= numpy.linalg.norm(regionNormal)
  pushDirection = regionNormal if direction is None else bridgeMeshAccess.toArray(direction) / numpy.linalg.norm(direction)
  updated = positions.copy()
  if mode in ("raise", "lower", "crease"):
    sign = 1 if mode == "raise" else -1
    updated += sign * strength * weights[:, None] * pushDirection
    if mode == "crease":
      toAxis = nearestPoints(positions) - positions
      toAxis -= (toAxis @ pushDirection)[:, None] * pushDirection
      updated += creasePinch * weights[:, None] * toAxis
  elif mode == "smooth":
    for _ in range(iterations):
      updated += strength * weights[:, None] * (vertexNeighbourAverages(sceneObject, updated) - updated)
  elif mode == "flatten":
    centroid = (positions[affected] * weights[affected, None]).sum(0) / weights[affected].sum()
    heights = (positions - centroid) @ pushDirection
    updated -= strength * weights[:, None] * heights[:, None] * pushDirection
  else:
    targets = carveTargets(positions)
    above = affected & (positions[:, 2] > targets)
    updated[above, 2] -= strength * (positions[above, 2] - targets[above])
  moved = numpy.linalg.norm(updated - positions, axis=1)
  writeWorldPositions(sceneObject, updated)
  return {"affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3)}


def sculptAtPoint(objectName, mode, center, radius, strength, falloff, direction, iterations):
  if mode == "carve":
    raise ValueError("carve follows a path; use sculptAlongPath")
  centerArray = bridgeMeshAccess.toArray(center)
  return sculpt(
    objectName, mode,
    lambda positions: numpy.linalg.norm(positions - centerArray, axis=1),
    lambda positions: numpy.broadcast_to(centerArray, positions.shape),
    radius, strength, falloff, direction, iterations, None,
  )


def profileHeights(profile, fractions):
  profileArray = bridgeMeshAccess.toArray(profile)
  if profileArray.ndim != 2 or profileArray.shape[1] != 2 or profileArray[0, 0] != 0 or profileArray[-1, 0] != 1 or (numpy.diff(profileArray[:, 0]) <= 0).any():
    raise ValueError("profile is [[lateralFraction, heightAboveFloor], ...] with fractions rising from 0 (path center) to 1 (stroke edge)")
  return numpy.interp(fractions, profileArray[:, 0], profileArray[:, 1])


def sculptAlongPath(objectName, mode, path, radius, strength, falloff, direction, iterations, profile):
  if len(path) < 2:
    raise ValueError("A path needs at least two points")
  if (mode == "carve") != (profile is not None):
    raise ValueError("carve needs a profile, and only carve takes one")
  horizontal = mode == "carve"

  def nearestPoints(positions):
    pathArray = bridgeMeshAccess.toArray(path)
    densified = numpy.concatenate([numpy.linspace(start, end, 32, endpoint=False) for start, end in zip(pathArray[:-1], pathArray[1:])] + [pathArray[-1:]])
    return densified[numpy.linalg.norm(positions[:, None] - densified[None], axis=2).argmin(1)]

  def carveTargets(positions):
    lateral, floors = distancesToPolyline(positions, path, horizontal=True)
    return floors + profileHeights(profile, numpy.clip(lateral / radius, 0, 1))

  return sculpt(
    objectName, mode,
    lambda positions: distancesToPolyline(positions, path, horizontal)[0],
    nearestPoints, radius, strength, falloff, direction, iterations, carveTargets,
  )


def selectedFaces(meshEditor, sceneObject, selector):
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  return [meshEditor.faces[index] for index in numpy.flatnonzero(mask)], mask


def extrudeFaces(objectName, selector, distance, direction):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  _, faceNormals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, mask = selectedFaces(meshEditor, sceneObject, selector)
  worldDirection = faceNormals[mask].sum(0) if direction is None else bridgeMeshAccess.toArray(direction)
  if numpy.linalg.norm(worldDirection) == 0:
    raise ValueError("The selected faces' normals cancel out; pass a direction")
  worldDirection = worldDirection / numpy.linalg.norm(worldDirection) * distance
  extruded = bmesh.ops.extrude_face_region(meshEditor, geom=faces)
  newVertices = [element for element in extruded["geom"] if isinstance(element, bmesh.types.BMVert)]
  bmesh.ops.translate(meshEditor, verts=newVertices, vec=mathutils.Vector(bridgeMeshAccess.localDirection(sceneObject, worldDirection)))
  bmesh.ops.delete(meshEditor, geom=faces, context="FACES")
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"extrudedFaces": len(faces)} | bridgeMeshAccess.meshCounts(sceneObject)


def insetFaces(objectName, selector, thickness, depth):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  bmesh.ops.inset_region(meshEditor, faces=faces, thickness=thickness, depth=depth, use_even_offset=True)
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"insetFaces": len(faces)} | bridgeMeshAccess.meshCounts(sceneObject)


def bevelEdges(objectName, selector, width, segments, minimumAngleDegrees):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  vertexMask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  edges = [edge for edge in meshEditor.edges if vertexMask[edge.verts[0].index] and vertexMask[edge.verts[1].index]]
  if minimumAngleDegrees is not None:
    edges = [edge for edge in edges if edge.is_manifold and math.degrees(edge.calc_face_angle()) >= minimumAngleDegrees]
  if not edges:
    meshEditor.free()
    raise ValueError(f"Selector {selector!r} matches no edges of '{objectName}'" + (f" sharper than {minimumAngleDegrees} degrees" if minimumAngleDegrees is not None else ""))
  bmesh.ops.bevel(meshEditor, geom=edges, offset=width, offset_type="OFFSET", segments=segments, profile=0.5, affect="EDGES")
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"beveledEdges": len(edges)} | bridgeMeshAccess.meshCounts(sceneObject)


def subdivide(objectName, selector, cuts):
  if cuts < 1:
    raise ValueError(f"cuts must be at least 1, got {cuts}")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  edges = list({edge for face in faces for edge in face.edges})
  bmesh.ops.subdivide_edges(meshEditor, edges=edges, cuts=cuts, use_grid_fill=True)
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"subdividedFaces": len(faces)} | bridgeMeshAccess.meshCounts(sceneObject)


def applyModifier(sceneObject, modifier):
  depsgraph = bpy.context.evaluated_depsgraph_get()
  appliedMesh = bpy.data.meshes.new_from_object(sceneObject.evaluated_get(depsgraph), preserve_all_data_layers=True, depsgraph=depsgraph)
  if len(appliedMesh.polygons) == 0:
    operation = modifier.type.lower()
    bpy.data.meshes.remove(appliedMesh)
    sceneObject.modifiers.remove(modifier)
    raise ValueError(f"{operation} would leave '{sceneObject.name}' with no faces; nothing was changed")
  previousMesh = sceneObject.data
  sceneObject.modifiers.remove(modifier)
  sceneObject.data = appliedMesh
  if previousMesh.users == 0:
    meshName = previousMesh.name
    bpy.data.meshes.remove(previousMesh)
    appliedMesh.name = meshName


def requireNoModifiers(sceneObject):
  if len(sceneObject.modifiers):
    raise ValueError(f"'{sceneObject.name}' has modifiers {[modifier.name for modifier in sceneObject.modifiers]}; this operation applies the whole stack, so remove or apply them first")


def booleanCut(objectName, cutterName, operation, keepCutter):
  if operation not in booleanOperations:
    raise ValueError(f"operation must be one of {list(booleanOperations)}, got '{operation}'")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  cutter = bridgeMeshAccess.requireMeshObject(cutterName)
  requireNoModifiers(sceneObject)
  before = bridgeMeshAccess.meshCounts(sceneObject)
  modifier = sceneObject.modifiers.new("zonewrightBoolean", "BOOLEAN")
  modifier.object = cutter
  modifier.operation = operation
  modifier.solver = "EXACT"
  applyModifier(sceneObject, modifier)
  if not keepCutter:
    cutterMesh = cutter.data
    bpy.data.objects.remove(cutter)
    if cutterMesh.users == 0:
      bpy.data.meshes.remove(cutterMesh)
  return {"before": before, "after": bridgeMeshAccess.meshCounts(sceneObject), "cutterKept": keepCutter}


def decimate(objectName, ratio):
  if not 0 < ratio < 1:
    raise ValueError(f"ratio must be in (0, 1), got {ratio}")
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireNoModifiers(sceneObject)
  before = bridgeMeshAccess.meshCounts(sceneObject)
  modifier = sceneObject.modifiers.new("zonewrightDecimate", "DECIMATE")
  modifier.ratio = ratio
  applyModifier(sceneObject, modifier)
  return {"before": before, "after": bridgeMeshAccess.meshCounts(sceneObject)}


def cleanupMesh(objectName, mergeDistance, recalculateNormals):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  before = bridgeMeshAccess.meshCounts(sceneObject)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  bmesh.ops.remove_doubles(meshEditor, verts=list(meshEditor.verts), dist=mergeDistance)
  bmesh.ops.dissolve_degenerate(meshEditor, edges=list(meshEditor.edges), dist=mergeDistance)
  if recalculateNormals:
    bmesh.ops.recalc_face_normals(meshEditor, faces=list(meshEditor.faces))
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  after = bridgeMeshAccess.meshCounts(sceneObject)
  return {"before": before, "after": after, "mergedVertices": before["vertices"] - after["vertices"]}


commands = {
  "moveVertices": (moveVertices, True),
  "sculptAtPoint": (sculptAtPoint, True),
  "sculptAlongPath": (sculptAlongPath, True),
  "extrudeFaces": (extrudeFaces, True),
  "insetFaces": (insetFaces, True),
  "bevelEdges": (bevelEdges, True),
  "subdivide": (subdivide, True),
  "booleanCut": (booleanCut, True),
  "decimate": (decimate, True),
  "cleanupMesh": (cleanupMesh, True),
}
