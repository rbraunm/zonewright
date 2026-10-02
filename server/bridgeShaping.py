"""Shaping meshes: vertex moves, sculpting, and topology edits, all addressed by selectors in world units. Runs under Blender's Python."""
import math

import bmesh
import bpy
import mathutils
import mathutils.kdtree
import mathutils.noise
import numpy

import bridgeMeshAccess
import bridgePasses

falloffCurves = ("constant", "linear", "smooth", "sharp")
sculptModes = ("raise", "lower", "smooth", "flatten", "crease", "carve")
fractionModes = ("smooth", "flatten", "carve")
booleanOperations = ("DIFFERENCE", "UNION", "INTERSECT")
creasePinch = 0.25
noiseBasis = "PERLIN_ORIGINAL"
maximumOctaves = 8
roughenDirections = ("normal", "up")
# horizontal keeps heights, surface moves along the surface, full moves in every direction.
warpPlanes = ("horizontal", "surface", "full")


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
  """Move the mesh as seen; with shaping passes, the move goes into the active pass."""
  localPositions = bridgeMeshAccess.localPositions(sceneObject, worldPositions)
  if bridgeMeshAccess.hasShapingPasses(sceneObject):
    bridgePasses.writeIntoActivePass(sceneObject, localPositions)
    return
  sceneObject.data.vertices.foreach_set("co", localPositions.ravel())
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


def sculpt(objectName, mode, distances, nearestPoints, radius, strength, curve, direction, iterations, carve):
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
    updated = carve(sceneObject, positions, affected, strength)
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


def validatedProfile(profile, conformRim):
  profileArray = bridgeMeshAccess.toArray(profile)
  if profileArray.ndim != 2 or profileArray.shape[1] != 2 or profileArray[0, 0] != 0 or profileArray[-1, 0] != 1 or (numpy.diff(profileArray[:, 0]) <= 0).any():
    raise ValueError("profile is [[lateralFraction, heightAboveFloor], ...] with fractions rising from 0 (path center) to 1 (stroke edge)")
  if conformRim and (numpy.diff(profileArray[:, 1]) <= 0).any():
    raise ValueError("conformRim needs profile heights that rise from the path center to the edge; pass conformRim false for this profile")
  return profileArray


def medianEdgeLength(sceneObject, positions, vertexMask):
  edges = numpy.empty(len(sceneObject.data.edges) * 2, dtype=numpy.int64)
  sceneObject.data.edges.foreach_get("vertices", edges)
  edges = edges.reshape(-1, 2)
  edges = edges[vertexMask[edges].any(axis=1)]
  return float(numpy.median(numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)))


def carveAlongPath(sceneObject, positions, affected, strength, path, radius, profileArray, conformRim):
  """Lower vertices to the path floor plus the profile height; with conformRim, untouched vertices just outside the cut slide sideways onto the rim contour so the edge follows the profile instead of the grid."""
  lateral, floors, nearest = bridgeMeshAccess.distancesToPolyline(positions, path, horizontal=True)
  targets = floors + numpy.interp(numpy.clip(lateral / radius, 0, 1), profileArray[:, 0], profileArray[:, 1])
  updated = positions.copy()
  lowered = affected & (positions[:, 2] > targets)
  updated[lowered, 2] -= strength * (positions[lowered, 2] - targets[lowered])
  if conformRim:
    heightsAboveFloor = positions[:, 2] - floors
    contourLateral = numpy.interp(heightsAboveFloor, profileArray[:, 1], profileArray[:, 0]) * radius
    slide = contourLateral - lateral
    maximumSlide = 0.75 * medianEdgeLength(sceneObject, positions, lowered)
    sliding = affected & ~lowered & (heightsAboveFloor > profileArray[0, 1]) & (heightsAboveFloor < profileArray[-1, 1]) & (slide < 0) & (-slide <= maximumSlide) & (lateral > 0)
    outward = (positions[sliding, :2] - nearest[sliding]) / lateral[sliding, None]
    updated[sliding, :2] += outward * slide[sliding, None]
  return updated


def sculptAlongPath(objectName, mode, path, radius, strength, falloff, direction, iterations, profile, conformRim):
  if len(path) < 2:
    raise ValueError("A path needs at least two points")
  if (mode == "carve") != (profile is not None):
    raise ValueError("carve needs a profile, and only carve takes one")
  horizontal = mode == "carve"
  profileArray = validatedProfile(profile, conformRim) if mode == "carve" else None

  def nearestPoints(positions):
    pathArray = bridgeMeshAccess.toArray(path)
    densified = numpy.concatenate([numpy.linspace(start, end, 32, endpoint=False) for start, end in zip(pathArray[:-1], pathArray[1:])] + [pathArray[-1:]])
    return densified[numpy.linalg.norm(positions[:, None] - densified[None], axis=2).argmin(1)]

  return sculpt(
    objectName, mode,
    lambda positions: bridgeMeshAccess.distancesToPolyline(positions, path, horizontal)[0],
    nearestPoints, radius, strength, falloff, direction, iterations,
    lambda sceneObject, positions, affected, carveStrength: carveAlongPath(sceneObject, positions, affected, carveStrength, path, radius, profileArray, conformRim),
  )


def selectedFaces(meshEditor, sceneObject, selector):
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  return [meshEditor.faces[index] for index in numpy.flatnonzero(mask)], mask


def deleteFaces(objectName, selector):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  if len(faces) == len(meshEditor.faces):
    meshEditor.free()
    raise ValueError(f"Selector {selector!r} matches every face of '{objectName}'; delete the object instead")
  bmesh.ops.delete(meshEditor, geom=faces, context="FACES")
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"deletedFaces": len(faces)} | bridgeMeshAccess.meshCounts(sceneObject)


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
  bridgeMeshAccess.requireNoShapingPasses(sceneObject, "cut it")
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
  bridgeMeshAccess.requireNoShapingPasses(sceneObject, "decimate it")
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


def maskWeights(sceneObject, selector, fadeDistance, positions):
  """1 inside the selection and 0 outside; with fadeDistance, rising smoothly from the selection's edge over that distance, so a
  masked change leaves no step at the edge."""
  if fadeDistance < 0:
    raise ValueError(f"fadeDistance must be non-negative, got {fadeDistance}")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "vertices")
  weights = mask.astype(numpy.float64)
  outside = positions[~mask]
  if fadeDistance == 0 or len(outside) == 0:
    return weights
  tree = mathutils.kdtree.KDTree(len(outside))
  for index, point in enumerate(outside):
    tree.insert(point, index)
  tree.balance()
  inside = numpy.flatnonzero(mask)
  ramp = numpy.clip(numpy.array([tree.find(positions[index])[2] for index in inside]) / fadeDistance, 0, 1)
  weights[inside] = ramp * ramp * (3 - 2 * ramp)
  return weights


def noiseSamplePoints(positions, featureSize, seed):
  if featureSize <= 0:
    raise ValueError(f"featureSize must be positive, got {featureSize}")
  return positions / featureSize + numpy.random.default_rng(seed).uniform(-1000, 1000, 3)


def fractalNoise(points, octaves, roughness):
  """Perlin noise summed over octaves, each twice the frequency of the last and `roughness` times its amplitude, normalized to the
  first octave's range."""
  total = numpy.zeros(len(points))
  amplitude, frequency, weightSum = 1.0, 1.0, 0.0
  for _ in range(octaves):
    total += amplitude * numpy.array([mathutils.noise.noise(mathutils.Vector(point * frequency), noise_basis=noiseBasis) for point in points])
    weightSum += amplitude
    amplitude *= roughness
    frequency *= 2
  return total / weightSum


def moveSummary(positions, updated):
  moved = numpy.linalg.norm(updated - positions, axis=1)
  return {"affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3), "meanMove": round(float(moved[moved > 1e-9].mean()) if (moved > 1e-9).any() else 0.0, 3)}


def roughen(objectName, featureSize, amplitude, octaves, roughness, seed, direction, selector, fadeDistance):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if amplitude <= 0 or not 1 <= octaves <= maximumOctaves or not 0 < roughness <= 1:
    raise ValueError(f"amplitude must be positive, octaves 1 to {maximumOctaves}, and roughness in (0, 1]")
  if direction not in roughenDirections:
    raise ValueError(f"direction must be one of {list(roughenDirections)}, got '{direction}'")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  values = fractalNoise(noiseSamplePoints(positions, featureSize, seed), octaves, roughness)
  pushDirections = normals if direction == "normal" else numpy.broadcast_to((0.0, 0.0, 1.0), normals.shape)
  updated = positions + (amplitude * values * weights)[:, None] * pushDirections
  writeWorldPositions(sceneObject, updated)
  return moveSummary(positions, updated)


def warp(objectName, featureSize, amplitude, seed, plane, selector, fadeDistance):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if amplitude <= 0:
    raise ValueError(f"amplitude must be positive, got {amplitude}")
  if plane not in warpPlanes:
    raise ValueError(f"plane must be one of {list(warpPlanes)}, got '{plane}'")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  vectors = numpy.array([list(mathutils.noise.noise_vector(mathutils.Vector(point), noise_basis=noiseBasis)) for point in noiseSamplePoints(positions, featureSize, seed)])
  if plane == "horizontal":
    vectors[:, 2] = 0
  elif plane == "surface":
    vectors -= (vectors * normals).sum(1)[:, None] * normals
  updated = positions + amplitude * weights[:, None] * vectors
  writeWorldPositions(sceneObject, updated)
  return moveSummary(positions, updated)


commands = {
  "moveVertices": (moveVertices, True),
  "sculptAtPoint": (sculptAtPoint, True),
  "sculptAlongPath": (sculptAlongPath, True),
  "deleteFaces": (deleteFaces, True),
  "extrudeFaces": (extrudeFaces, True),
  "insetFaces": (insetFaces, True),
  "bevelEdges": (bevelEdges, True),
  "subdivide": (subdivide, True),
  "booleanCut": (booleanCut, True),
  "decimate": (decimate, True),
  "cleanupMesh": (cleanupMesh, True),
  "roughen": (roughen, True),
  "warp": (warp, True),
}
