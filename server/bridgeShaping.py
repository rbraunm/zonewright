"""Shaping meshes: vertex moves, sculpting, and topology edits, all addressed by selectors in world units. Runs under Blender's Python."""
import collections
import math

import bmesh
import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeCaveData
import bridgeMeshAccess
import bridgeNoise
import bridgePasses
import bridgeSurfacing

falloffCurves = ("constant", "linear", "smooth", "sharp")
sculptModes = ("raise", "lower", "smooth", "flatten", "crease", "carve", "fill")
fractionModes = ("smooth", "flatten", "carve", "fill")
# carve lowers ground to a cross-section profile along a path; fill raises it to one.
profileModes = ("carve", "fill")
booleanOperations = ("DIFFERENCE", "UNION", "INTERSECT")
# Marks the cutter's faces through a cut, so the faces it makes are told from the target's own.
cutterFaceAttribute = "zonewrightCutterFace"
faceValueTypes = {"INT": numpy.int32, "INT8": numpy.int8, "FLOAT": numpy.float32, "BOOLEAN": bool}
creasePinch = 0.25
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
# A face whose area in plan is this share of an edge squared or less is as good as folded: stored in single precision, its sign is noise.
degenerateAreaShare = 1e-6
diagonalSweeps = 4
# A vertex this close to a level already lies on it, and a cut runs through it.
contourTolerance = 1e-6
# Halvings that place a cut on a distance level along its edge: 2 to the -20th of the edge.
contourBisections = 20
# An edge whose ends lie on one side of a distance level is sampled at most this share of the level apart to find where the distance
# passes the level between them; splitting there and triangulating repeats at most this many times.
contourSampleShare = 0.25
contourPeakRounds = 4
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
  """Move the mesh as seen; with shaping passes, the move goes into the active pass. A cave's lining stays where it is and its ring on
  its plug triangles (bridgeCaveData.guardedKey); returns what a result says of the lining vertices it left alone."""
  localPositions = bridgeMeshAccess.localPositions(sceneObject, worldPositions)
  if bridgeMeshAccess.hasShapingPasses(sceneObject):
    return bridgeCaveData.liningReport(bridgePasses.writeIntoActivePass(sceneObject, localPositions))
  current = numpy.empty(len(sceneObject.data.vertices) * 3)
  sceneObject.data.vertices.foreach_get("co", current)
  guarded, left = bridgeCaveData.guardedKey(sceneObject, current.reshape(-1, 3), localPositions)
  sceneObject.data.vertices.foreach_set("co", guarded.ravel())
  sceneObject.data.update()
  return bridgeCaveData.liningReport(left)


def moveVertices(objectName, selector, offset, falloff):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "moveVertices")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "vertices")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = mask.astype(numpy.float64)
  if falloff is not None:
    distances = numpy.linalg.norm(positions - bridgeMeshAccess.toArray(falloff["center"]), axis=1) / falloff["radius"]
    weights *= falloffWeights(distances, falloff["curve"])
  displacement = weights[:, None] * bridgeMeshAccess.toArray(offset)
  left = writeWorldPositions(sceneObject, positions + displacement)
  return {"movedVertices": int((weights > 0).sum()), "largestMove": round(float(numpy.linalg.norm(displacement, axis=1).max()), 3)} | left


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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "sculptAtPoint and sculptAlongPath")
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
    return finishProfileStroke(sceneObject, positions, updated, unsnapped)
  moved = numpy.linalg.norm(updated - positions, axis=1)
  left = writeWorldPositions(sceneObject, updated)
  return {"affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3), "foldedFaces": bridgeMeshAccess.foldedFaceCount(sceneObject, positions, updated)} | left


def finishProfileStroke(sceneObject, positions, updated, unsnapped):
  """Write a carve or fill, triangulate what it shaped along the contours, and put back any snapped vertices that leave their cell no
  diagonal facing up."""
  left = writeWorldPositions(sceneObject, updated)
  shaped = numpy.linalg.norm(updated - positions, axis=1) > 1e-9
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
  return {"affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3), "foldedFaces": int(overturnedFaces(sceneObject, updated, shaped).sum())} | triangulation | left


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
  """Faces touching the masked vertices that face down seen from above or have next to no area in plan: on ground shaped by moves up
  and down, folds."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  touching = numpy.add.reduceat(vertexMask[loopVertices].astype(numpy.int64), numpy.cumsum(loopTotals) - loopTotals) > 0
  edgeLength = medianEdgeLength(sceneObject, worldPositions, vertexMask)
  # A cave's vault faces down by design: its faces are no fold.
  return touching & ~bridgeCaveData.caveFaceMask(sceneObject) & (bridgeMeshAccess.faceNormals(sceneObject, worldPositions)[:, 2] <= degenerateAreaShare * edgeLength * edgeLength)


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


def unslidWhereUnshaped(positions, snapped, updated):
  """A vertex slid onto a break but left at its height by the stroke (ground already past the profile there) goes back where it was:
  slid, it would stand at its old height in a new place, a tooth off the ledge line."""
  unshaped = (updated[:, 2] == snapped[:, 2]) & (numpy.abs(snapped[:, :2] - positions[:, :2]).max(axis=1) > 0)
  updated = updated.copy()
  updated[unshaped] = positions[unshaped]
  return updated, unshaped


def meshEdgeEnds(sceneObject):
  edges = numpy.empty(len(sceneObject.data.edges) * 2, dtype=numpy.int64)
  sceneObject.data.edges.foreach_get("vertices", edges)
  return edges.reshape(-1, 2).T


def snapOntoBreaks(sceneObject, positions, affected, coordinate, breakCoordinates, directions, border):
  """Slide each affected vertex within half an edge of a break, and the nearer end of every edge a break crosses (farther than that
  on a stretched edge), sideways along its direction (the way coordinate grows) onto that break, so the break runs through vertices;
  except where it would land almost on a neighbor snapping onto the same break from the other side. breakCoordinates holds each
  vertex's breaks; returns the slid positions and their coordinates."""
  offsets = breakCoordinates - coordinate[:, None]
  closest = numpy.abs(offsets).argmin(axis=1)
  rows = numpy.arange(len(offsets))
  slide = offsets[rows, closest]
  edgeLength = medianEdgeLength(sceneObject, positions, affected)
  first, second = meshEdgeEnds(sceneObject)
  crossing = affected[first] & affected[second] & (closest[first] == closest[second]) & (numpy.sign(slide[first]) != numpy.sign(slide[second]))
  snapping = affected & (numpy.abs(slide) <= 0.5 * edgeLength)
  snapping[numpy.where(numpy.abs(slide[first]) <= numpy.abs(slide[second]), first, second)[crossing]] = True
  snapping &= (numpy.linalg.norm(directions, axis=1) > 0.5) & ~border
  pairs = snapping[first] & snapping[second] & (closest[first] == closest[second]) & (numpy.sign(slide[first]) != numpy.sign(slide[second]))
  first, second = first[pairs], second[pairs]
  apart = positions[second, :2] - positions[first, :2]
  alongBreak = numpy.abs(directions[first, 0] * apart[:, 1] - directions[first, 1] * apart[:, 0])
  crowded = alongBreak < breakCrowding * edgeLength
  snapping[numpy.where(numpy.abs(slide[first]) > numpy.abs(slide[second]), first, second)[crowded]] = False
  positions = positions.copy()
  positions[snapping, :2] += directions[snapping] * slide[snapping, None]
  coordinate = coordinate.copy()
  coordinate[snapping] = breakCoordinates[snapping, closest[snapping]]
  return positions, coordinate


def profileAlongPath(sceneObject, positions, affected, strength, path, radii, profileArray, mode, conformRim, conformBreaks):
  """Move vertices toward the path floor plus the profile height: carve lowers those above it, fill raises those below it. With
  conformBreaks, the vertices nearest each break of the profile (each point between its first and last) first slide sideways onto
  that break's contour, so ledges and cliff bands run along the path as clean lines instead of zigzagging across the grid. With
  conformRim (carve only), untouched vertices just outside the cut slide sideways onto the rim contour so the edge follows the profile
  instead of the grid. The mesh's open edge never slides, so a cut running off the terrain keeps its border."""
  fractions, floors, radiiHere, nearest = bridgeMeshAccess.strokeAlongPath(positions, path, radii, horizontal=True)
  lateral = unslidLateral = fractions * radiiHere
  unsnapped = movedToProfile(positions, affected, strength, floors + numpy.interp(numpy.clip(fractions, 0, 1), profileArray[:, 0], profileArray[:, 1]), mode)
  border = bridgeMeshAccess.boundaryVertexMask(sceneObject) | bridgeCaveData.fixedInPlan(sceneObject)
  outwards = numpy.zeros((len(positions), 2))
  away = lateral > 0
  outwards[away] = (positions[away, :2] - nearest[away]) / lateral[away, None]
  snapped = positions
  if conformBreaks:
    if len(profileArray) < 3:
      raise ValueError("conformBreaks needs a profile with breaks: points between its first and last")
    snapped, lateral = snapOntoBreaks(sceneObject, positions, affected, lateral, profileArray[1:-1, 0][None, :] * radiiHere[:, None], outwards, border)
    fractions = lateral / radiiHere
  targets = floors + numpy.interp(numpy.clip(fractions, 0, 1), profileArray[:, 0], profileArray[:, 1])
  updated, unslid = unslidWhereUnshaped(positions, snapped, movedToProfile(snapped, affected, strength, targets, mode))
  lateral = numpy.where(unslid, unslidLateral, lateral)
  moving = updated[:, 2] != positions[:, 2]
  if conformRim:
    heightsAboveFloor = positions[:, 2] - floors
    contourLateral = numpy.interp(heightsAboveFloor, profileArray[:, 1], profileArray[:, 0]) * radiiHere
    slide = contourLateral - lateral
    rimEdgeLength = medianEdgeLength(sceneObject, positions, moving)
    sliding = affected & ~moving & (heightsAboveFloor > profileArray[0, 1]) & (heightsAboveFloor < profileArray[-1, 1]) & (slide < 0) & (-slide <= 0.75 * rimEdgeLength) & (lateral > 0) & ~border
    updated[sliding, :2] += outwards[sliding] * slide[sliding, None]
    first, second = meshEdgeEnds(sceneObject)
    touching = sliding[first] | sliding[second]
    first, second = first[touching], second[touching]
    crowded = numpy.linalg.norm(updated[first, :2] - updated[second, :2], axis=1) < breakCrowding * rimEdgeLength
    larger = numpy.where(numpy.abs(slide[first]) > numpy.abs(slide[second]), first, second)
    staying = numpy.where(sliding[first] & sliding[second], larger, numpy.where(sliding[first], first, second))[crowded]
    updated[staying, :2] = positions[staying, :2]
  return updated, unsnapped


def signedDistanceToOutline(points, outline):
  """Each point's distance from a closed outline, positive inside it, and the direction in plan in which that distance grows."""
  starts, ends = outline, numpy.roll(outline, -1, axis=0)
  segments = ends - starts
  along = numpy.clip(((points[:, None, :2] - starts[None]) * segments[None]).sum(2) / (segments * segments).sum(1)[None], 0, 1)
  nearest = starts[None] + along[:, :, None] * segments[None]
  distances = numpy.linalg.norm(points[:, None, :2] - nearest, axis=2)
  closest = distances.argmin(axis=1)
  rows = numpy.arange(len(points))
  distance, nearestPoint = distances[rows, closest], nearest[rows, closest]
  inside = bridgeMeshAccess.insidePolygon(points[:, :2], outline)
  away = numpy.zeros((len(points), 2))
  apart = distance > 1e-9
  away[apart] = (points[apart, :2] - nearestPoint[apart]) / distance[apart, None]
  return numpy.where(inside, distance, -distance), numpy.where(inside[:, None], away, -away)


def sculptOutline(objectName, mode, outline, base, profile, strength, conformBreaks):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "sculptOutline")
  if mode not in profileModes:
    raise ValueError(f"mode must be one of {list(profileModes)}, got '{mode}'")
  if not 0 < strength <= 1:
    raise ValueError(f"strength is a fraction in (0, 1], got {strength}")
  outlineArray = bridgeMeshAccess.toArray(outline)
  if outlineArray.ndim != 2 or outlineArray.shape[1] != 2 or len(outlineArray) < 3:
    raise ValueError(f"An outline is at least three [x, y] points, got {outline!r}")
  profileArray = bridgeMeshAccess.toArray(profile)
  if profileArray.ndim != 2 or profileArray.shape[1] != 2 or len(profileArray) < 2 or (numpy.diff(profileArray[:, 0]) <= 0).any():
    raise ValueError("profile is [[signedDistance, heightAboveBase], ...] with distances rising (negative outside the outline, positive inside)")
  if conformBreaks and len(profileArray) < 3:
    raise ValueError("conformBreaks needs a profile with breaks: points between its first and last")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  distance, directions = signedDistanceToOutline(positions, outlineArray)
  affected = distance >= profileArray[0, 0]
  if not affected.any():
    raise ValueError(f"No vertices of '{objectName}' lie within {-profileArray[0, 0]:g} of the outline or inside it")
  unsnapped = movedToProfile(positions, affected, strength, base + numpy.interp(distance, profileArray[:, 0], profileArray[:, 1]), mode)
  snapped = positions
  if conformBreaks:
    breaks = numpy.broadcast_to(profileArray[1:-1, 0][None, :], (len(positions), len(profileArray) - 2))
    snapped, distance = snapOntoBreaks(sceneObject, positions, affected, distance, breaks, directions, bridgeMeshAccess.boundaryVertexMask(sceneObject) | bridgeCaveData.fixedInPlan(sceneObject))
  updated, _ = unslidWhereUnshaped(positions, snapped, movedToProfile(snapped, affected, strength, base + numpy.interp(distance, profileArray[:, 0], profileArray[:, 1]), mode))
  return finishProfileStroke(sceneObject, positions, updated, unsnapped)


def facet(objectName, selector, cellSize, strength, seed, fadeDistance):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "facet")
  if cellSize <= 0 or not 0 < strength <= 1:
    raise ValueError(f"cellSize must be positive and strength a fraction in (0, 1], got {cellSize} and {strength}")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  selected = numpy.flatnonzero(weights > 0)
  points = positions[selected]
  low = points.min(axis=0)
  counts = numpy.ceil((points.max(axis=0) - low) / cellSize).astype(numpy.int64) + 1
  cellIndices = numpy.stack(numpy.meshgrid(*[numpy.arange(count) for count in counts], indexing="ij"), axis=-1).reshape(-1, 3)
  seeds = low + (cellIndices + numpy.random.default_rng(seed).uniform(0, 1, cellIndices.shape)) * cellSize
  tree = mathutils.kdtree.KDTree(len(seeds))
  for index, seedPoint in enumerate(seeds):
    tree.insert(seedPoint, index)
  tree.balance()
  cells = numpy.array([tree.find(point)[1] for point in points])
  moves = numpy.zeros_like(points)
  facets = 0
  for cell in numpy.unique(cells):
    members = numpy.flatnonzero(cells == cell)
    if len(members) < 3:
      continue
    centered = points[members] - points[members].mean(axis=0)
    normal = numpy.linalg.eigh(centered.T @ centered)[1][:, 0]
    moves[members] = -(centered @ normal)[:, None] * normal
    facets += 1
  updated = positions.copy()
  updated[selected] += strength * weights[selected, None] * moves
  left = writeWorldPositions(sceneObject, updated)
  return moveSummary(sceneObject, positions, updated) | {"facets": facets} | left


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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "deleteFaces")
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  if len(faces) == len(meshEditor.faces):
    meshEditor.free()
    raise ValueError(f"Selector {selector!r} matches every face of '{objectName}'; delete the object instead")
  bmesh.ops.delete(meshEditor, geom=faces, context="FACES")
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"deletedFaces": len(faces)} | bridgeMeshAccess.meshCounts(sceneObject)


def materialDensities(sceneObject):
  """Each material slot's world units per repeat as box projection would map its faces as they are now; None without a UV layer."""
  # Measured against box projection's own areas, not the faces' true areas, so a mesh box-projected at d reads exactly d however its
  # faces slope away from the axes, and faces mapped from it continue its texture without a jump in scale.
  areas = bridgeMeshAccess.textureAreas(sceneObject)
  if areas is None:
    return None
  return {int(slot): bridgeMeshAccess.worldUnitsPerRepeat(areas.box[areas.materials == slot].sum(), areas.uv[areas.materials == slot].sum()) for slot in numpy.unique(areas.materials)}


def keepLayeredMapping(meshEditor, faces):
  """On a mesh whose surfacing layers compose its UV map (bridgeSurfacing.baseMappingName), keep the faces' mapping as they show it now
  in its base mapping and out of every transition's, so the layers show it again each time they compose."""
  vectorLayers = meshEditor.loops.layers.float_vector
  base = vectorLayers.get(bridgeSurfacing.baseMappingName)
  if base is None:
    return
  uvLayer = meshEditor.loops.layers.uv.active
  transitions = [layer for name, layer in vectorLayers.items() if name.startswith(bridgeSurfacing.transitionMappingPrefix)]
  for face in faces:
    for loop in face.loops:
      u, v = loop[uvLayer].uv
      loop[base] = (u, v, 0.0)
      for layer in transitions:
        loop[layer] = (math.nan, math.nan, math.nan)


def mapNewFaces(meshEditor, sceneObject, faces, densities):
  """Box-map the faces an edit made at their material's density before it (materialDensities), kept so through surfacing layers
  (keepLayeredMapping): (mapped, unmapped and why) by material."""
  slots = sceneObject.material_slots

  def materialName(slot):
    return slots[slot].material.name if slot < len(slots) and slots[slot].material else None

  if densities is None:
    counts = collections.Counter(face.material_index for face in faces)
    return [], [{"material": materialName(slot), "faces": count, "reason": "the mesh has no UV layer"} for slot, count in sorted(counts.items())]
  uvLayer = meshEditor.loops.layers.uv.active
  meshEditor.normal_update()
  matrix = sceneObject.matrix_world
  normalMatrix = matrix.to_3x3().inverted().transposed()
  boxAxes = [bridgeSurfacing.planarAxes(numpy.eye(3)[axis]) for axis in range(3)]
  mapped, unmapped = collections.Counter(), collections.Counter()
  for face in faces:
    density = densities.get(face.material_index)
    if density is None:
      unmapped[face.material_index] += 1
      continue
    across, along = boxAxes[int(numpy.abs(numpy.array(normalMatrix @ face.normal)).argmax())]
    for loop in face.loops:
      point = numpy.array(matrix @ loop.vert.co)
      loop[uvLayer].uv = (float(point @ across) / density, float(point @ along) / density)
    mapped[face.material_index] += 1
  keepLayeredMapping(meshEditor, faces)
  return (
    [{"material": materialName(slot), "faces": count, "worldUnitsPerRepeat": round(densities[slot], 3)} for slot, count in sorted(mapped.items())],
    [{"material": materialName(slot), "faces": count, "reason": "the material's faces on the mesh had no UV area to take a density from"} for slot, count in sorted(unmapped.items())],
  )


def extrudeFaces(objectName, selector, distance, direction):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "extrudeFaces")
  _, faceNormals, _ = bridgeMeshAccess.readFaceArrays(sceneObject)
  densities = materialDensities(sceneObject)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, mask = selectedFaces(meshEditor, sceneObject, selector)
  worldDirection = faceNormals[mask].sum(0) if direction is None else bridgeMeshAccess.toArray(direction)
  if numpy.linalg.norm(worldDirection) == 0:
    meshEditor.free()
    raise ValueError("The selected faces' normals cancel out; pass a direction")
  worldDirection = worldDirection / numpy.linalg.norm(worldDirection) * distance
  existing = set(meshEditor.faces)
  extruded = bmesh.ops.extrude_face_region(meshEditor, geom=faces)
  moved = {element for element in extruded["geom"] if isinstance(element, bmesh.types.BMFace)}
  newVertices = [element for element in extruded["geom"] if isinstance(element, bmesh.types.BMVert)]
  bmesh.ops.translate(meshEditor, verts=newVertices, vec=mathutils.Vector(bridgeMeshAccess.localDirection(sceneObject, worldDirection)))
  bmesh.ops.delete(meshEditor, geom=faces, context="FACES")
  mapped, unmapped = mapNewFaces(meshEditor, sceneObject, [face for face in meshEditor.faces if face not in existing and face not in moved], densities)
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"extrudedFaces": len(faces), "mappedFaces": mapped, "unmappedFaces": unmapped} | bridgeMeshAccess.meshCounts(sceneObject)


def insetFaces(objectName, selector, thickness, depth):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "insetFaces")
  densities = materialDensities(sceneObject)
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  rim = bmesh.ops.inset_region(meshEditor, faces=faces, thickness=thickness, depth=depth, use_even_offset=True, use_interpolate=True)["faces"]
  mapped, unmapped = mapNewFaces(meshEditor, sceneObject, rim, densities)
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  return {"insetFaces": len(faces), "mappedFaces": mapped, "unmappedFaces": unmapped} | bridgeMeshAccess.meshCounts(sceneObject)


def bevelEdges(objectName, selector, width, segments, minimumAngleDegrees):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "bevelEdges")
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
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "subdivide")
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  faces, _ = selectedFaces(meshEditor, sceneObject, selector)
  # Sets of mesh elements iterate in memory order, which changes from run to run; index order keeps the result the same.
  edges = sorted({edge for face in faces for edge in face.edges}, key=lambda edge: edge.index)
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


def worldFaceTree(sceneObject, faceIndices):
  """A BVH over the given faces of a mesh in world space; its hits name the faces by their place in faceIndices."""
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  polygons = bridgeMeshAccess.faceVertexIndices(sceneObject)
  return mathutils.bvhtree.BVHTree.FromPolygons(positions.tolist(), [polygons[index].tolist() for index in faceIndices])


def faceValues(mesh):
  """Each face attribute a cut carries over to the faces it makes (all but Blender's own hidden ones), by name, as an array."""
  values = {}
  for attribute in mesh.attributes:
    if attribute.domain != "FACE" or attribute.name.startswith("."):
      continue
    if attribute.data_type not in faceValueTypes:
      raise ValueError(f"'{mesh.name}' has a face attribute '{attribute.name}' of type {attribute.data_type}, which a cut cannot carry over")
    array = numpy.empty(len(mesh.polygons), dtype=faceValueTypes[attribute.data_type])
    attribute.data.foreach_get("value", array)
    values[attribute.name] = array
  return values


def booleanCut(objectName, cutterName, operation, keepCutter):
  """Cut with the exact solver; each face it makes takes the face attributes of the nearest face the cutter crosses, and is box-mapped."""
  if operation not in booleanOperations:
    raise ValueError(f"operation must be one of {list(booleanOperations)}, got '{operation}'")
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "booleanCut")
  cutter = bridgeMeshAccess.requireMeshObject(cutterName)
  if cutter == sceneObject:
    raise ValueError(f"'{objectName}' cannot cut itself")
  bridgeMeshAccess.requireNoShapingPasses(sceneObject, "cut it")
  bridgeCaveData.requireNoCaves(sceneObject, "cut it with a boolean")
  requireNoModifiers(sceneObject)
  before = bridgeMeshAccess.meshCounts(sceneObject)
  pairs = worldFaceTree(sceneObject, range(len(sceneObject.data.polygons))).overlap(worldFaceTree(cutter, range(len(cutter.data.polygons))))
  crossed = sorted({first for first, _ in pairs})
  if not crossed:
    raise ValueError(f"'{cutterName}' does not cross the surface of '{objectName}' (it lies apart from it, wholly inside it, or wholly around it), so it cuts no opening; nothing was changed")
  crossedTree = worldFaceTree(sceneObject, crossed)
  densities = materialDensities(sceneObject)
  materials = list(sceneObject.data.materials)
  values = faceValues(sceneObject.data)
  values["material_index"] = numpy.empty(len(sceneObject.data.polygons), dtype=numpy.int32)
  sceneObject.data.polygons.foreach_get("material_index", values["material_index"])
  marker = cutter.data.attributes.new(cutterFaceAttribute, "INT", "FACE")
  marker.data.foreach_set("value", numpy.ones(len(cutter.data.polygons), dtype=numpy.int32))
  modifier = sceneObject.modifiers.new("zonewrightBoolean", "BOOLEAN")
  modifier.object = cutter
  modifier.operation = operation
  modifier.solver = "EXACT"
  try:
    applyModifier(sceneObject, modifier)
  finally:
    cutter.data.attributes.remove(cutter.data.attributes[cutterFaceAttribute])
  mesh = sceneObject.data
  fromCutter = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
  mesh.attributes[cutterFaceAttribute].data.foreach_get("value", fromCutter)
  mesh.attributes.remove(mesh.attributes[cutterFaceAttribute])
  made = numpy.flatnonzero(fromCutter)
  centers = numpy.empty(len(mesh.polygons) * 3)
  mesh.polygons.foreach_get("center", centers)
  centers = bridgeMeshAccess.worldPositions(sceneObject, centers.reshape(-1, 3))
  sources = numpy.array([crossed[crossedTree.find_nearest(mathutils.Vector(centers[face]))[2]] for face in made], dtype=numpy.int64)
  for name, array in values.items():
    target = mesh.polygons if name == "material_index" else mesh.attributes[name].data
    field = "material_index" if name == "material_index" else "value"
    current = numpy.empty(len(mesh.polygons), dtype=array.dtype)
    target.foreach_get(field, current)
    current[made] = array[sources]
    target.foreach_set(field, current)
  while len(mesh.materials) > len(materials):
    mesh.materials.pop()
  mesh.update()
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  mapped, unmapped = mapNewFaces(meshEditor, sceneObject, [meshEditor.faces[int(face)] for face in made], densities)
  bridgeMeshAccess.storeBMesh(meshEditor, sceneObject)
  if not keepCutter:
    cutterMesh = cutter.data
    bpy.data.objects.remove(cutter)
    if cutterMesh.users == 0:
      bpy.data.meshes.remove(cutterMesh)
  result = {
    "before": before, "after": bridgeMeshAccess.meshCounts(sceneObject), "cutterKept": keepCutter, "madeFaces": len(made), "mappedFaces": mapped,
    "unmappedFaces": unmapped,
  }
  if operation == "DIFFERENCE" and len(made) == 0:
    result["warning"] = (
      f"The cut left an opening in '{objectName}' with nothing lining it: no face of '{cutterName}' was kept, because '{objectName}' encloses"
      " nothing where the cutter crosses it (an open surface such as a terrain sheet or a plane), so the opening shows through to whatever"
      " lies beyond. That is right for a window in a one-sided wall; to dig a pit into open ground, shape the ground instead (sculptAtPoint"
      " lower, sculptAlongPath carve)."
    )
  return result


def decimate(objectName, ratio):
  if not 0 < ratio < 1:
    raise ValueError(f"ratio must be in (0, 1), got {ratio}")
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "decimate")
  bridgeMeshAccess.requireNoShapingPasses(sceneObject, "decimate it")
  bridgeCaveData.requireNoCaves(sceneObject, "decimate it")
  requireNoModifiers(sceneObject)
  before = bridgeMeshAccess.meshCounts(sceneObject)
  modifier = sceneObject.modifiers.new("zonewrightDecimate", "DECIMATE")
  modifier.ratio = ratio
  applyModifier(sceneObject, modifier)
  return {"before": before, "after": bridgeMeshAccess.meshCounts(sceneObject)}


def cleanupMesh(objectName, mergeDistance, recalculateNormals):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "cleanupMesh")
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


def diagonalTaken(edge):
  """Whether the other diagonal of an edge's two triangles is already an edge: bmesh turns an edge onto it without refusing (where the
  ground folds over the cell), leaving two faces on the same corners."""
  first, second = (next(vertex for vertex in face.verts if vertex not in edge.verts) for face in edge.link_faces)
  return any(second in other.verts for other in first.link_edges)


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
  if diagonalTaken(edge):
    return 0.0
  across = [next(vertex.index for vertex in face.verts if vertex not in edge.verts) for face in faces]
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
  # A cave puts its plug faces back on the vertices they were cut from: a cell turned or split beside them would overlap them then.
  caveVertices = set(numpy.flatnonzero(bridgeCaveData.fixedInPlan(sceneObject)).tolist())
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)
  layers = [layer for kind in ("bool", "float", "int", "string") for layer in getattr(meshEditor.faces.layers, kind).values()]
  maskedVertices = [meshEditor.verts[index] for index in numpy.flatnonzero(vertexMask)]

  def atCave(face):
    return any(vertex.index in caveVertices for vertex in face.verts)

  quads = sorted({face for vertex in maskedVertices for face in vertex.link_faces if len(face.verts) == 4 and not atCave(face)}, key=lambda face: face.index)
  bmesh.ops.triangulate(meshEditor, faces=quads, quad_method="FIXED")
  turned = 0
  for _ in range(diagonalSweeps):
    meshEditor.edges.index_update()
    gains = {}
    for edge in {edge for vertex in maskedVertices for face in vertex.link_faces for edge in face.edges}:
      if caveVertices and any(atCave(face) for face in edge.link_faces):
        continue
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
    turned += sum(bmesh.utils.edge_rotate(edge) is not None for edge in turning if not diagonalTaken(edge))
  bridgeMeshAccess.storeSplitBMesh(meshEditor, sceneObject)
  return {"splitCells": len(quads), "turnedDiagonals": turned}


def followContours(objectName, selector):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "followContours")
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "vertices")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "vertices")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  return triangulateAlongContours(sceneObject, positions, mask) | bridgeMeshAccess.meshCounts(sceneObject)


class HeightMeasure:
  """Levels as heights: a level crosses an edge where its height does, evenly along it, so never twice."""

  tolerance = contourTolerance

  def __init__(self, positions):
    self.values = positions[:, 2].tolist()

  def crossesTwice(self, level):
    return False

  def fractions(self, starts, ends, level, startValues, endValues):
    return (startValues - level) / (startValues - endValues)


class BorderMeasure:
  """Levels as distances from the border of the faces a selector picks (with onlyAbove, from its stretches at a wall's foot), measured
  for the vertices of the faces being cut."""

  tolerance = contourTolerance

  def __init__(self, sceneObject, positions, distanceFrom, onlyAbove, within, levels):
    if min(levels) <= 0:
      raise ValueError(f"Distances from a border are positive, got {levels!r}")
    picked = bridgeMeshAccess.evaluateSelector(distanceFrom, sceneObject, "faces")
    bridgeMeshAccess.requireSelection(picked, distanceFrom, sceneObject, "faces")
    border = (bridgeMeshAccess.footBorders(sceneObject, ~picked, picked) if onlyAbove else bridgeMeshAccess.faceBorders(sceneObject, picked, ~picked))[0]
    if not len(border):
      raise ValueError(f"The faces {distanceFrom!r} picks have no border{' below the faces beyond them' if onlyAbove else ''} on '{sceneObject.name}'")
    edges = bridgeMeshAccess.meshEdges(sceneObject.data)
    self.tree = bridgeMeshAccess.BorderDistance(positions[edges[border, 0]], positions[edges[border, 1]])
    loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
    measured = numpy.unique(loopVertices[numpy.repeat(within, loopTotals)])
    values = numpy.full(len(positions), numpy.inf)
    values[measured] = self.tree.distances(positions[measured])
    self.values = values.tolist()

  def crossesTwice(self, level):
    return True

  def valuesAt(self, points):
    return self.tree.distances(points)

  def fractions(self, starts, ends, level, startValues, endValues):
    return bisectedFractions(self.valuesAt, starts, ends, level, startValues)


class WaterlineMeasure:
  """Levels as distances in plan out from a pool or river's waterline on the mesh (0 the waterline itself), negative under the water."""

  tolerance = bridgeMeshAccess.waterlineTolerance

  def __init__(self, sceneObject, positions, surface, levels):
    if min(levels) < 0:
      raise ValueError(f"Distances from a waterline are 0 (the waterline) or more out of the water, got {levels!r}")
    self.surface = surface
    segments = surface.waterline(positions, bridgeMeshAccess.meshTriangles(sceneObject))
    if not len(segments):
      raise ValueError(f"'{sceneObject.name}' never crosses the surface of '{surface.name}': it has no waterline")
    self.border = bridgeMeshAccess.waterlineBorder(segments)
    rise = surface.rise(positions)
    values = numpy.where(rise < 0, -1.0, 1.0)
    if max(levels) > 0:
      edges = bridgeMeshAccess.meshEdges(sceneObject.data)
      margin = max(levels) + float(numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1).max())
      low, high = segments.reshape(-1, 2).min(0) - margin, segments.reshape(-1, 2).max(0) + margin
      distances = numpy.full(len(positions), numpy.inf)
      measured = numpy.flatnonzero(((positions[:, :2] >= low) & (positions[:, :2] <= high)).all(axis=1))
      distances[measured] = self.planDistances(positions[measured])
      values *= distances
    values[numpy.abs(rise) <= self.tolerance] = 0.0
    self.values = values.tolist()

  def planDistances(self, points):
    flat = numpy.zeros((len(points), 3))
    flat[:, :2] = points[:, :2]
    return self.border.distances(flat)

  def crossesTwice(self, level):
    # The waterline itself is where an edge crosses the surface; only a distance out from it can pass a level twice along an edge.
    return level != 0

  def valuesAt(self, points):
    return self.planDistances(points) * numpy.where(self.surface.rise(points) < 0, -1.0, 1.0)

  def fractions(self, starts, ends, level, startValues, endValues):
    """Where along each edge the level lies, NaN where the edge leaves the surface there rather than crossing the line."""
    if level == 0:
      fractions = self.surface.crossingFractions(starts, ends)
      crossings = starts + fractions[:, None] * (ends - starts)
      heights, apart = self.surface.heights(crossings)
      return numpy.where((apart <= self.tolerance) & (numpy.abs(crossings[:, 2] - heights) <= self.tolerance), fractions, numpy.nan)
    fractions = bisectedFractions(self.valuesAt, starts, ends, level, startValues)
    reached = numpy.abs(self.valuesAt(starts + fractions[:, None] * (ends - starts)) - level) <= self.tolerance
    return numpy.where(reached, fractions, numpy.nan)


def bisectedFractions(measure, starts, ends, level, startValues):
  """Where along each edge a measure that does not change evenly along it reaches a level, by halving them all at once."""
  low, high = numpy.zeros(len(starts)), numpy.ones(len(starts))
  startsOver = startValues > level
  for _ in range(contourBisections):
    middle = (low + high) / 2
    sameSide = (measure(starts + middle[:, None] * (ends - starts)) > level) == startsOver
    low, high = numpy.where(sameSide, middle, low), numpy.where(sameSide, high, middle)
  return (low + high) / 2


def splitAtPeaks(meshEditor, cutting, values, points, measure, level, refuseAtCaves):
  """Split edges a distance level crosses twice where they lie farthest past it, and triangulate around; returns how many it split."""
  splits = []
  for edge in sorted({edge for face in cutting for edge in face.edges}, key=lambda edge: edge.index):
    start, end = edge.verts
    first, second = values[start.index] - level, values[end.index] - level
    # An end on the level (an edge a cut made along it, or out from it) already has the crossing there.
    if not (math.isfinite(first) and math.isfinite(second)) or first * second < 0 or min(abs(first), abs(second)) <= measure.tolerance:
      continue
    length = float(numpy.linalg.norm(points[end.index] - points[start.index]))
    # Distance changes no faster than one moves along the edge, so it reaches the level only on an edge at least this long.
    if abs(first) + abs(second) >= length:
      continue
    count = max(2, math.ceil(length / (contourSampleShare * level)))
    fractions = numpy.arange(1, count) / count
    past = measure.valuesAt(points[start.index] + fractions[:, None] * (points[end.index] - points[start.index])) - level
    farthest = int(numpy.argmax(-numpy.sign(first) * past))
    if numpy.sign(past[farthest]) != numpy.sign(first):
      splits.append((edge, start, float(fractions[farthest]), float(past[farthest]) + level))
  refuseAtCaves([face for edge, _, _, _ in splits for face in edge.link_faces])
  touched, inserted = set(), set()
  for edge, start, fraction, distance in splits:
    end = edge.other_vert(start)
    _, vertex = bmesh.utils.edge_split(edge, start, fraction)
    vertex.index = len(values)
    values.append(distance)
    points.append(points[start.index] + fraction * (points[end.index] - points[start.index]))
    touched.update(vertex.link_faces)
    inserted.add(vertex)
  # An edge up a wall one cell wide, both ends on the border, passes a level below half the wall's height twice. A face that gained two
  # new vertices is split between them first: both lie past the level, and a diagonal from one end of the wall to the other would pass
  # it twice again.
  for face in sorted(touched, key=lambda face: face.index):
    peaks = [vertex for vertex in face.verts if vertex in inserted]
    if len(peaks) >= 2 and not any(peaks[1] in edge.verts for edge in peaks[0].link_edges if edge in face.edges):
      newFace, _ = bmesh.utils.face_split(face, peaks[0], peaks[1])
      touched.add(newFace)
  meshEditor.faces.index_update()
  if touched:
    triangulated = bmesh.ops.triangulate(meshEditor, faces=sorted(touched, key=lambda face: face.index), quad_method="BEAUTY", ngon_method="BEAUTY")
    cutting.update(triangulated["faces"])
    cutting.difference_update([face for face in cutting if not face.is_valid])
  return len(splits)


def cutAlongLevels(sceneObject, positions, measure, levels, within):
  """Split the faces of a face mask along each level of a measure: each edge the level crosses twice where it lies farthest past it,
  then each crossed edge where the level crosses it, then each face between two such splits."""
  values = list(measure.values)
  points = list(positions)
  caveVertices = bridgeCaveData.CaveVertices(sceneObject) if bridgeCaveData.holdsCaves(sceneObject) else None
  meshEditor = bridgeMeshAccess.loadBMesh(sceneObject)

  def refuseAtCaves(faces):
    # A split edge's new vertex blends its ends' cave ids, which would leave an id twice or a made-up one; a split face beside a cave
    # would overlap its plug when the cave is taken back.
    if caveVertices is None:
      return
    touched = [vertex.index for face in faces for vertex in face.verts if vertex.index < len(caveVertices.tagged) and caveVertices.tagged[vertex.index]]
    if touched:
      meshEditor.free()
      raise ValueError(
        f"The cut would split faces of '{sceneObject.name}' at cave(s) {caveVertices.namesOf(numpy.array(touched))}, whose take-back needs the"
        " faces around it as they were cut; cut clear of the cave (a selector away from its mouth and the ground over it), or take the cave"
        " back (removeCave, which returns its definition), cut, and cut the cave again (cutCave with that definition). Nothing was changed"
      )

  cutting = {meshEditor.faces[index] for index in numpy.flatnonzero(within)}
  crossed = [
    face for face in cutting if len(face.verts) > 3
    and any(min(values[vertex.index] for vertex in face.verts) < level < max(values[vertex.index] for vertex in face.verts) for level in levels)
  ]
  refuseAtCaves(crossed)
  if crossed:
    # A face that is not flat is drawn as triangles split from its first corner (Blender's own); cut as those triangles, the ground
    # keeps its shape, so cutting again finds the same lines.
    triangles = bmesh.ops.triangulate(meshEditor, faces=crossed, quad_method="FIXED", ngon_method="EAR_CLIP")["faces"]
    cutting = (cutting - set(crossed)) | set(triangles)
    meshEditor.edges.index_update()
    meshEditor.faces.index_update()
  splitEdges = splitFaces = doubleCrossings = 0
  for level in sorted(levels):
    if measure.crossesTwice(level):
      for _ in range(contourPeakRounds + 1):
        split = splitAtPeaks(meshEditor, cutting, values, points, measure, level, refuseAtCaves)
        meshEditor.edges.index_update()
        meshEditor.faces.index_update()
        doubleCrossings += split
        if not split:
          break
      else:
        meshEditor.free()
        raise ValueError(f"Edges the distance level {level} crosses twice remained after {contourPeakRounds} rounds of splitting them on '{sceneObject.name}'; nothing was changed")
    onLevel = set()
    crossing = []
    for edge in sorted({edge for face in cutting for edge in face.edges}, key=lambda edge: edge.index):
      start, end = edge.verts
      below, above = values[start.index] - level, values[end.index] - level
      if below * above < 0 and min(abs(below), abs(above)) > measure.tolerance:
        crossing.append((edge, start, end))
    refuseAtCaves([face for edge, _, _ in crossing for face in edge.link_faces])
    if crossing:
      starts = numpy.array([points[start.index] for _, start, _ in crossing])
      ends = numpy.array([points[end.index] for _, _, end in crossing])
      startValues = numpy.array([values[start.index] for _, start, _ in crossing])
      endValues = numpy.array([values[end.index] for _, _, end in crossing])
      for (edge, start, _), fraction, startPoint, endPoint in zip(crossing, measure.fractions(starts, ends, level, startValues, endValues), starts, ends):
        if numpy.isnan(fraction):
          continue
        _, vertex = bmesh.utils.edge_split(edge, start, fraction)
        vertex.index = len(values)
        values.append(level)
        points.append(startPoint + fraction * (endPoint - startPoint))
        onLevel.add(vertex)
        splitEdges += 1
    for face in list(cutting):
      corners = [vertex for vertex in face.verts if vertex in onLevel or abs(values[vertex.index] - level) <= measure.tolerance]
      if len(corners) == 2 and not any(edge in face.edges for edge in corners[0].link_edges if corners[1] in edge.verts):
        refuseAtCaves([face])
        newFace, _ = bmesh.utils.face_split(face, corners[0], corners[1])
        cutting.add(newFace)
        splitFaces += 1
    meshEditor.edges.index_update()
  polygons = [face for face in cutting if face.is_valid and len(face.verts) > 3]
  bmesh.ops.triangulate(meshEditor, faces=polygons, quad_method="BEAUTY", ngon_method="BEAUTY")
  bridgeMeshAccess.storeSplitBMesh(meshEditor, sceneObject)
  return {"splitEdges": splitEdges, "splitFaces": splitFaces, "doubleCrossings": doubleCrossings}


def cutContours(objectName, levels, distanceFrom, waterline, selector, onlyAbove):
  """Cut the selected faces along level lines, as an artist adds an edge loop: of equal height, of equal distance from the border of
  the faces distanceFrom picks (with onlyAbove, from its stretches at a wall's foot: footBorders), or of equal distance out from a pool
  or river's waterline."""
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "cutContours")
  if not levels or len(set(levels)) != len(levels):
    raise ValueError(f"levels is a list of different values, got {levels!r}")
  if distanceFrom is not None and waterline is not None:
    raise ValueError("distanceFrom and waterline each say what the levels measure from; give one of them")
  if onlyAbove and distanceFrom is None:
    raise ValueError("onlyAbove measures distance from where the faces beyond distanceFrom's rise above them; it needs distanceFrom")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  if waterline is not None:
    measure = WaterlineMeasure(sceneObject, positions, bridgeMeshAccess.WaterSurface(bridgeMeshAccess.requireWater(waterline)), levels)
  elif distanceFrom is not None:
    measure = BorderMeasure(sceneObject, positions, distanceFrom, onlyAbove, within, levels)
  else:
    measure = HeightMeasure(positions)
  return cutAlongLevels(sceneObject, positions, measure, levels, within) | bridgeMeshAccess.meshCounts(sceneObject)


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


def moveSummary(sceneObject, positions, updated):
  moved = numpy.linalg.norm(updated - positions, axis=1)
  return {
    "affectedVertices": int((moved > 1e-9).sum()), "largestMove": round(float(moved.max()), 3),
    "meanMove": round(float(moved[moved > 1e-9].mean()) if (moved > 1e-9).any() else 0.0, 3),
    "foldedFaces": bridgeMeshAccess.foldedFaceCount(sceneObject, positions, updated),
  }


def roughen(objectName, featureSize, amplitude, octaves, roughness, seed, direction, selector, fadeDistance):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "roughen")
  if amplitude <= 0 or not 1 <= octaves <= maximumOctaves or not 0 < roughness <= 1:
    raise ValueError(f"amplitude must be positive, octaves 1 to {maximumOctaves}, and roughness in (0, 1]")
  if direction not in roughenDirections:
    raise ValueError(f"direction must be one of {list(roughenDirections)}, got '{direction}'")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  values = bridgeNoise.fractalNoise(bridgeNoise.noiseSamplePoints(positions, featureSize, seed), octaves, roughness)
  pushDirections = normals if direction == "normal" else numpy.broadcast_to((0.0, 0.0, 1.0), normals.shape)
  updated = positions + (amplitude * values * weights)[:, None] * pushDirections
  left = writeWorldPositions(sceneObject, updated)
  summary = moveSummary(sceneObject, positions, updated) | left
  edgeLength = medianEdgeLength(sceneObject, positions, weights > 0)
  finest = featureSize / 2 ** (octaves - 1)
  if finest < edgeLength:
    usable = math.floor(math.log2(featureSize / edgeLength)) + 1 if featureSize >= edgeLength else 0
    summary["warning"] = (
      f"The finest of {octaves} octaves is {finest:g} units across, finer than the mesh's {edgeLength:.3g}-unit edges: the mesh cannot hold it,"
      " so it reads as a grain along the triangles. " + (f"Use at most {usable} octaves at this featureSize, or a finer mesh." if usable else "featureSize itself is finer than the edges; use a larger one or a finer mesh.")
    )
  return summary


def warp(objectName, featureSize, amplitude, seed, plane, selector, fadeDistance):
  sceneObject = bridgeMeshAccess.requireEditableMesh(objectName, "warp")
  if amplitude <= 0:
    raise ValueError(f"amplitude must be positive, got {amplitude}")
  if plane not in warpPlanes:
    raise ValueError(f"plane must be one of {list(warpPlanes)}, got '{plane}'")
  positions, normals = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = maskWeights(sceneObject, selector, fadeDistance, positions)
  samplePositions = positions.copy()
  if plane == "horizontal":
    samplePositions[:, 2] = 0
  vectors = bridgeNoise.noiseVectors(bridgeNoise.noiseSamplePoints(samplePositions, featureSize, seed))
  if plane == "horizontal":
    vectors[:, 2] = 0
  elif plane == "surface":
    vectors -= (vectors * normals).sum(1)[:, None] * normals
  vectors /= bridgeNoise.noiseSpread * math.sqrt(3 if plane == "full" else 2)
  updated = positions + amplitude * weights[:, None] * vectors
  left = writeWorldPositions(sceneObject, updated)
  return moveSummary(sceneObject, positions, updated) | left


commands = {
  "moveVertices": (moveVertices, True),
  "sculptAtPoint": (sculptAtPoint, True),
  "sculptAlongPath": (sculptAlongPath, True),
  "sculptOutline": (sculptOutline, True),
  "facet": (facet, True),
  "deleteFaces": (deleteFaces, True),
  "extrudeFaces": (extrudeFaces, True),
  "insetFaces": (insetFaces, True),
  "bevelEdges": (bevelEdges, True),
  "subdivide": (subdivide, True),
  "booleanCut": (booleanCut, True),
  "decimate": (decimate, True),
  "cleanupMesh": (cleanupMesh, True),
  "followContours": (followContours, True),
  "cutContours": (cutContours, True),
  "roughen": (roughen, True),
  "warp": (warp, True),
}
