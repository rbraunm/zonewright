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
sculptModes = ("raise", "lower", "smooth", "flatten", "crease", "carve", "fill")
fractionModes = ("smooth", "flatten", "carve", "fill")
# carve lowers ground to a cross-section profile along a path; fill raises it to one.
profileModes = ("carve", "fill")
booleanOperations = ("DIFFERENCE", "UNION", "INTERSECT")
creasePinch = 0.25
noiseBasis = "PERLIN_ORIGINAL"
# The standard deviation of a PERLIN_ORIGINAL sample, and of each component of its noise vector, measured over 20000 random points;
# dividing by it makes an amplitude the typical move.
noiseSpread = 0.278
maximumOctaves = 8
roughenDirections = ("normal", "up")
# horizontal keeps heights and moves every height at a spot alike, so a wall bends without shearing; surface moves along the surface;
# full moves in every direction.
warpPlanes = ("horizontal", "surface", "full")
# A cell's diagonal turns only when that shortens the height step across the cell by more than this share of the mesh's edge length,
# so flat and evenly sloping cells keep theirs.
diagonalTurnMargin = 0.05
# A cell's other diagonal is about as long as its own; a much longer one would reach across two cells.
diagonalStretch = 1.5
# A triangle under this share of the square on the mesh's edge length is a sliver: no diagonal turns to make one, and one is always
# turned away.
diagonalSliverShare = 0.05
diagonalSweeps = 4
# Two neighbors on either side of one break both snap onto it unless they would land closer than this share of an edge apart along
# it (where a warp squeezed the grid); then only the nearer snaps, as both would fold the faces between them. A rim slide that would
# land this close to a neighbor (one snapped onto a break lying on the rim) is not made either.
breakCrowding = 0.35
# Where a cell is left with no diagonal that faces up (a vertex snapped onto a break between two that snapped from its other side),
# carve and fill put that face's snapped vertices back where the plain cut leaves them, and triangulate again, this many times at
# most.
snapRepairs = 4


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


def sculpt(objectName, mode, strokeFractions, nearestPoints, strength, curve, direction, iterations, profileStroke):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if mode not in sculptModes:
    raise ValueError(f"mode must be one of {list(sculptModes)}, got '{mode}'")
  if mode in fractionModes and not 0 < strength <= 1:
    raise ValueError(f"{mode} strength is a fraction in (0, 1], got {strength}")
  if mode not in fractionModes and strength <= 0:
    raise ValueError(f"{mode} strength is a distance in units and must be positive, got {strength}")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = falloffWeights(strokeFractions(positions), "sharp" if mode == "crease" else curve)
  affected = weights > 0
  if not affected.any():
    raise ValueError(f"No vertices of '{objectName}' lie within the stroke")
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
    updated, unsnapped = profileStroke(sceneObject, positions, affected, strength)
  moved = numpy.linalg.norm(updated - positions, axis=1)
  writeWorldPositions(sceneObject, updated)
  shaped = moved > 1e-9
  if mode not in profileModes:
    return {"affectedVertices": int(shaped.sum()), "largestMove": round(float(moved.max()), 3), "foldedFaces": bridgeMeshAccess.foldedFaceCount(sceneObject, positions, updated)}
  triangulation = triangulateAlongContours(sceneObject, updated, shaped) | {"keptOffContours": 0}
  for _ in range(snapRepairs):
    returning = overturnedVertexMask(sceneObject, updated, shaped) & (numpy.abs(updated[:, :2] - unsnapped[:, :2]).max(axis=1) > 1e-9)
    if not returning.any():
      break
    updated[returning] = unsnapped[returning]
    writeWorldPositions(sceneObject, updated)
    triangulation["turnedDiagonals"] += triangulateAlongContours(sceneObject, updated, shaped)["turnedDiagonals"]
    triangulation["keptOffContours"] += int(returning.sum())
  moved = numpy.linalg.norm(updated - positions, axis=1)
  return {"affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3), "foldedFaces": int(overturnedFaces(sceneObject, updated, shaped).sum())} | triangulation


def sculptAtPoint(objectName, mode, center, radius, strength, falloff, direction, iterations):
  if mode in profileModes:
    raise ValueError(f"{mode} shapes a cross-section profile; use sculptAlongPath, whose path can be a single point")
  if radius <= 0:
    raise ValueError(f"radius must be positive, got {radius}")
  centerArray = bridgeMeshAccess.toArray(center)
  return sculpt(
    objectName, mode,
    lambda positions: numpy.linalg.norm(positions - centerArray, axis=1) / radius,
    lambda positions: numpy.broadcast_to(centerArray, positions.shape),
    strength, falloff, direction, iterations, None,
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


def overturnedFaces(sceneObject, worldPositions, vertexMask):
  """Faces touching the masked vertices that lie flat or face down seen from above: on ground shaped by moves up and down, folds."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  touching = numpy.add.reduceat(vertexMask[loopVertices].astype(numpy.int64), numpy.cumsum(loopTotals) - loopTotals) > 0
  return touching & (bridgeMeshAccess.faceNormals(sceneObject, worldPositions)[:, 2] <= 0)


def overturnedVertexMask(sceneObject, worldPositions, vertexMask):
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  corners = numpy.zeros(len(worldPositions), dtype=bool)
  corners[loopVertices[numpy.repeat(overturnedFaces(sceneObject, worldPositions, vertexMask), loopTotals)]] = True
  return corners


def movedToProfile(positions, affected, strength, targets, mode):
  """carve lowers the affected vertices above their targets toward them; fill raises those below."""
  updated = positions.copy()
  moving = affected & ((positions[:, 2] > targets) if mode == "carve" else (positions[:, 2] < targets))
  updated[moving, 2] -= strength * (positions[moving, 2] - targets[moving])
  return updated


def profileAlongPath(sceneObject, positions, affected, strength, path, radii, profileArray, mode, conformRim, conformBreaks):
  """Move vertices toward the path floor plus the profile height: carve lowers those above it, fill raises those below it. With
  conformBreaks, the vertices nearest each break of the profile (each point between its first and last) first slide sideways onto
  that break's contour, so ledges and cliff bands run along the path as clean lines instead of zigzagging across the grid. With
  conformRim (carve only), untouched vertices just outside the cut slide sideways onto the rim contour so the edge follows the profile
  instead of the grid. The mesh's open edge never slides, so a cut running off the terrain keeps its border."""
  fractions, floors, radiiHere, nearest = bridgeMeshAccess.strokeAlongPath(positions, path, radii, horizontal=True)
  lateral = fractions * radiiHere
  unsnapped = movedToProfile(positions, affected, strength, floors + numpy.interp(numpy.clip(fractions, 0, 1), profileArray[:, 0], profileArray[:, 1]), mode)
  border = bridgeMeshAccess.boundaryVertexMask(sceneObject)
  meshEdges = numpy.empty(len(sceneObject.data.edges) * 2, dtype=numpy.int64)
  sceneObject.data.edges.foreach_get("vertices", meshEdges)
  meshEdges = meshEdges.reshape(-1, 2).T
  if conformBreaks:
    if len(profileArray) < 3:
      raise ValueError("conformBreaks needs a profile with breaks: points between its first and last")
    breakLaterals = profileArray[1:-1, 0][None, :] * radiiHere[:, None]
    offsets = breakLaterals - lateral[:, None]
    closest = numpy.abs(offsets).argmin(axis=1)
    slide = offsets[numpy.arange(len(offsets)), closest]
    edgeLength = medianEdgeLength(sceneObject, positions, affected)
    snapping = affected & (numpy.abs(slide) <= 0.5 * edgeLength) & (lateral > 0) & ~border
    first, second = meshEdges
    pairs = snapping[first] & snapping[second] & (closest[first] == closest[second]) & (numpy.sign(slide[first]) != numpy.sign(slide[second]))
    first, second = first[pairs], second[pairs]
    outwardOfFirst = (positions[first, :2] - nearest[first]) / lateral[first, None]
    apart = positions[second, :2] - positions[first, :2]
    alongBreak = numpy.abs(outwardOfFirst[:, 0] * apart[:, 1] - outwardOfFirst[:, 1] * apart[:, 0])
    crowded = alongBreak < breakCrowding * edgeLength
    snapping[numpy.where(numpy.abs(slide[first]) > numpy.abs(slide[second]), first, second)[crowded]] = False
    outward = (positions[snapping, :2] - nearest[snapping]) / lateral[snapping, None]
    positions = positions.copy()
    positions[snapping, :2] += outward * slide[snapping, None]
    lateral[snapping] = breakLaterals[snapping, closest[snapping]]
    fractions[snapping] = profileArray[1:-1, 0][closest[snapping]]
  targets = floors + numpy.interp(numpy.clip(fractions, 0, 1), profileArray[:, 0], profileArray[:, 1])
  updated = movedToProfile(positions, affected, strength, targets, mode)
  moving = updated[:, 2] != positions[:, 2]
  if conformRim:
    heightsAboveFloor = positions[:, 2] - floors
    contourLateral = numpy.interp(heightsAboveFloor, profileArray[:, 1], profileArray[:, 0]) * radiiHere
    slide = contourLateral - lateral
    rimEdgeLength = medianEdgeLength(sceneObject, positions, moving)
    sliding = affected & ~moving & (heightsAboveFloor > profileArray[0, 1]) & (heightsAboveFloor < profileArray[-1, 1]) & (slide < 0) & (-slide <= 0.75 * rimEdgeLength) & (lateral > 0) & ~border
    outward = (positions[sliding, :2] - nearest[sliding]) / lateral[sliding, None]
    updated[sliding, :2] += outward * slide[sliding, None]
    first, second = meshEdges
    touching = sliding[first] | sliding[second]
    first, second = first[touching], second[touching]
    crowded = numpy.linalg.norm(updated[first, :2] - updated[second, :2], axis=1) < breakCrowding * rimEdgeLength
    larger = numpy.where(numpy.abs(slide[first]) > numpy.abs(slide[second]), first, second)
    staying = numpy.where(sliding[first] & sliding[second], larger, numpy.where(sliding[first], first, second))[crowded]
    updated[staying, :2] = positions[staying, :2]
  return updated, unsnapped


def sculptAlongPath(objectName, mode, path, radius, radii, strength, falloff, direction, iterations, profile, conformRim, conformBreaks):
  if (radius is None) == (radii is None):
    raise ValueError("Give either radius (the whole stroke) or radii (one per path point)")
  strokeRadii = [radius] * len(path) if radii is None else radii
  if (mode in profileModes) != (profile is not None):
    raise ValueError(f"{' and '.join(profileModes)} need a profile, and only they take one")
  if len(path) == 1 and mode in profileModes:
    path, strokeRadii = path * 2, list(strokeRadii) * 2
  if len(path) < 2:
    raise ValueError(f"A path needs at least two points (one for {' or '.join(profileModes)})")
  if conformRim is None:
    conformRim = mode == "carve"
  if conformRim and mode != "carve":
    raise ValueError("conformRim slides a carve's rim; only carve takes it")
  if conformBreaks and mode not in profileModes:
    raise ValueError(f"conformBreaks shapes a profile's breaks; only {' and '.join(profileModes)} take it")
  horizontal = mode in profileModes
  profileArray = validatedProfile(profile, conformRim) if mode in profileModes else None

  def nearestPoints(positions):
    pathArray = bridgeMeshAccess.toArray(path)
    densified = numpy.concatenate([numpy.linspace(start, end, 32, endpoint=False) for start, end in zip(pathArray[:-1], pathArray[1:])] + [pathArray[-1:]])
    return densified[numpy.linalg.norm(positions[:, None] - densified[None], axis=2).argmin(1)]

  return sculpt(
    objectName, mode,
    lambda positions: bridgeMeshAccess.strokeAlongPath(positions, path, strokeRadii, horizontal)[0],
    nearestPoints, strength, falloff, direction, iterations,
    lambda sceneObject, positions, affected, profileStrength: profileAlongPath(sceneObject, positions, affected, profileStrength, path, strokeRadii, profileArray, mode, conformRim, conformBreaks),
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


def faceSignature(face, layers):
  return face.material_index, face.smooth, tuple(face[layer] for layer in layers)


def planarTurn(first, second, third):
  return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (third[0] - first[0])


def diagonalTurnGain(edge, heights, planar, margin, sliverArea, layers):
  """How much turning an edge between two triangles to their cell's other diagonal improves the cell: infinite when it removes a
  sliver or a fold (three corners snapped onto one break, the middle one just past the line), else how much it shortens the height
  step along the diagonal; 0 when that would not follow the contours better, would fold, stretch, or sliver the cell, or would merge
  faces of different material or surfacing. Terrain faces up, so a face's winding seen from above is counterclockwise."""
  faces = edge.link_faces
  if len(faces) != 2 or len(faces[0].verts) != 3 or len(faces[1].verts) != 3 or faceSignature(faces[0], layers) != faceSignature(faces[1], layers):
    return 0.0
  first, second = (vertex.index for vertex in edge.verts)
  winding = [vertex.index for vertex in faces[0].verts]
  if winding[(winding.index(first) + 1) % 3] != second:
    first, second = second, first
  across = [next(vertex.index for vertex in face.verts if vertex.index not in (first, second)) for face in faces]
  if math.dist(planar[across[0]], planar[across[1]]) > diagonalStretch * math.dist(planar[first], planar[second]):
    return 0.0

  def area(a, b, c):
    return planarTurn(planar[a], planar[b], planar[c]) / 2

  current = [area(first, second, across[0]), area(second, first, across[1])]
  turned = [area(first, across[1], across[0]), area(across[1], second, across[0])]
  if min(turned) < sliverArea:
    return 0.0
  if min(current) < sliverArea:
    return math.inf
  gain = abs(heights[first] - heights[second]) - abs(heights[across[0]] - heights[across[1]])
  return gain if gain > margin else 0.0


def triangulateAlongContours(sceneObject, worldPositions, vertexMask):
  """Split the quads around the masked vertices into triangles and turn each cell's diagonal to the one with the smaller height step,
  so the triangulation runs along the ground's contours: a ledge or cliff edge crossing the grid stays a clean line instead of notching
  where it steps to the next row. Vertices never move, so shaping passes keep what they hold."""
  if not vertexMask.any():
    return {"splitCells": 0, "turnedDiagonals": 0}
  heights = worldPositions[:, 2].tolist()
  planar = worldPositions[:, :2].tolist()
  edgeLength = medianEdgeLength(sceneObject, worldPositions, vertexMask)
  margin, sliverArea = diagonalTurnMargin * edgeLength, diagonalSliverShare * edgeLength * edgeLength
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  layers = [layer for kind in ("bool", "float", "int", "string") for layer in getattr(meshEditor.faces.layers, kind).values()]
  maskedVertices = [meshEditor.verts[index] for index in numpy.flatnonzero(vertexMask)]
  quads = list({face for vertex in maskedVertices for face in vertex.link_faces if len(face.verts) == 4})
  bmesh.ops.triangulate(meshEditor, faces=quads, quad_method="FIXED")
  turned = 0
  for _ in range(diagonalSweeps):
    meshEditor.edges.index_update()
    gains = {}
    for edge in {edge for vertex in maskedVertices for face in vertex.link_faces for edge in face.edges}:
      gain = diagonalTurnGain(edge, heights, planar, margin, sliverArea, layers)
      if gain > 0:
        gains[edge] = gain
    claimed, turning = set(), []
    for edge in sorted(gains, key=lambda edge: (-gains[edge], edge.index)):
      if claimed.isdisjoint(edge.link_faces):
        claimed.update(edge.link_faces)
        turning.append(edge)
    if not turning:
      break
    turned += sum(bmesh.utils.edge_rotate(edge) is not None for edge in turning)
  meshEditor.to_mesh(sceneObject.data)
  meshEditor.free()
  sceneObject.data.update()
  return {"splitCells": len(quads), "turnedDiagonals": turned}


def followContours(objectName, selector):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "vertices")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  return triangulateAlongContours(sceneObject, positions, mask) | bridgeMeshAccess.meshCounts(sceneObject)


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
  """Perlin noise summed over octaves, each twice the frequency of the last and `roughness` times its amplitude, scaled to a standard
  deviation of 1. The octaves are nearly independent, so their spreads add in quadrature."""
  total = numpy.zeros(len(points))
  amplitude, frequency, squareSum = 1.0, 1.0, 0.0
  for _ in range(octaves):
    total += amplitude * numpy.array([mathutils.noise.noise(mathutils.Vector(point * frequency), noise_basis=noiseBasis) for point in points])
    squareSum += amplitude * amplitude
    amplitude *= roughness
    frequency *= 2
  return total / (noiseSpread * math.sqrt(squareSum))


def moveSummary(sceneObject, positions, updated):
  moved = numpy.linalg.norm(updated - positions, axis=1)
  return {
    "affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3),
    "meanMove": round(float(moved[moved > 1e-9].mean()) if (moved > 1e-9).any() else 0.0, 3),
    "foldedFaces": bridgeMeshAccess.foldedFaceCount(sceneObject, positions, updated),
  }


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
  return moveSummary(sceneObject, positions, updated)


def warp(objectName, featureSize, amplitude, seed, plane, selector, fadeDistance):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if amplitude <= 0:
    raise ValueError(f"amplitude must be positive, got {amplitude}")
  if plane not in warpPlanes:
    raise ValueError(f"plane must be one of {list(warpPlanes)}, got '{plane}'")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  samplePositions = positions.copy()
  if plane == "horizontal":
    samplePositions[:, 2] = 0
  vectors = numpy.array([list(mathutils.noise.noise_vector(mathutils.Vector(point), noise_basis=noiseBasis)) for point in noiseSamplePoints(samplePositions, featureSize, seed)])
  if plane == "horizontal":
    vectors[:, 2] = 0
  elif plane == "surface":
    vectors -= (vectors * normals).sum(1)[:, None] * normals
  vectors /= noiseSpread * math.sqrt(3 if plane == "full" else 2)
  updated = positions + amplitude * weights[:, None] * vectors
  writeWorldPositions(sceneObject, updated)
  return moveSummary(sceneObject, positions, updated)


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
  "followContours": (followContours, True),
  "roughen": (roughen, True),
  "warp": (warp, True),
}
