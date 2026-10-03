"""Authoring a zone the way an environment artist does: regions that say what an area is to become, surfacing layers painted by intent
and edited at their boundaries, and broad strokes that take an area back to start it again. Runs under Blender's Python."""
import json

import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeEnvironment
import bridgeExport
import bridgeMeshAccess
import bridgeObjects
import bridgePasses
import bridgeShaping

regionCollectionName = "regions"
layerOrderProperty = bridgeMeshAccess.surfaceLayersProperty
baseAttributeName = "zonewrightSurfaceBase"
layerAttributePrefix = "zonewrightSurface:"
uncovered = -1
surfaceOperations = ("grow", "shrink", "smooth")
rebuildModes = ("surroundings", "height")
shapingChoices = ("keep", "reset", "rebuild")
# Rebuilding from the surroundings solves for smooth heights until the residual falls this far below where it started.
relaxTolerance = 1e-10


# Regions

def regionMesh(name, outline, bottom, top):
  outlineArray = bridgeMeshAccess.toArray(outline)
  if outlineArray.ndim != 2 or outlineArray.shape[1] != 2 or len(outlineArray) < 3:
    raise ValueError(f"A region outline is at least three [x, y] points, got {outline!r}")
  if not bottom < top:
    raise ValueError(f"A region's bottom must lie below its top, got {bottom} and {top}")
  sides = len(outlineArray)
  vertices = [(x, y, bottom) for x, y in outlineArray] + [(x, y, top) for x, y in outlineArray]
  faces = [tuple(reversed(range(sides))), tuple(range(sides, 2 * sides))] + [(index, (index + 1) % sides, sides + (index + 1) % sides, sides + index) for index in range(sides)]
  mesh = bpy.data.meshes.new(name)
  mesh.from_pydata(vertices, [], faces)
  mesh.validate()
  return mesh


def describeRegion(regionObject):
  outline, bottom, top = bridgeMeshAccess.regionShape(regionObject)
  x, y = outline[:, 0], outline[:, 1]
  area = abs(float(numpy.dot(x, numpy.roll(y, -1)) - numpy.dot(y, numpy.roll(x, -1)))) / 2
  return {"name": regionObject.name, "intent": regionObject[bridgeMeshAccess.regionIntentProperty], "outline": [[round(float(a), 2), round(float(b), 2)] for a, b in outline],
    "bottom": round(bottom, 2), "top": round(top, 2), "area": round(area)}


def createRegion(name, outline, bottom, top, intent):
  bridgeObjects.requireNewName(name)
  if not intent.strip():
    raise ValueError("A region needs its intent: what the area is to become")
  regionObject = bpy.data.objects.new(name, regionMesh(name, outline, bottom, top))
  regionObject[bridgeMeshAccess.regionIntentProperty] = intent.strip()
  regionObject.display_type = "WIRE"
  regionObject.hide_render = True
  bridgeObjects.targetCollection(regionCollectionName).objects.link(regionObject)
  return describeRegion(regionObject)


def editRegion(name, outline, bottom, top, intent):
  regionObject = bridgeMeshAccess.requireRegion(name)
  if outline is None and bottom is None and top is None and intent is None:
    raise ValueError("editRegion needs an outline, bottom, top, or intent")
  currentOutline, currentBottom, currentTop = bridgeMeshAccess.regionShape(regionObject)
  if outline is not None or bottom is not None or top is not None:
    oldMesh = regionObject.data
    regionObject.matrix_world = mathutils.Matrix.Identity(4)
    regionObject.data = regionMesh(name, outline if outline is not None else currentOutline.tolist(), currentBottom if bottom is None else bottom, currentTop if top is None else top)
    bpy.data.meshes.remove(oldMesh)
  if intent is not None:
    if not intent.strip():
      raise ValueError("A region needs its intent: what the area is to become")
    regionObject[bridgeMeshAccess.regionIntentProperty] = intent.strip()
  return describeRegion(regionObject)


def getRegions():
  bpy.context.view_layer.update()
  return {"regions": [describeRegion(sceneObject) for sceneObject in bpy.context.scene.objects if sceneObject.type == "MESH" and bridgeMeshAccess.regionIntentProperty in sceneObject]}


# Surfacing layers

def layerOrder(sceneObject):
  return json.loads(sceneObject[layerOrderProperty]) if layerOrderProperty in sceneObject else []


def requireLayer(sceneObject, name):
  layers = layerOrder(sceneObject)
  if name not in [layer["name"] for layer in layers]:
    raise ValueError(f"'{sceneObject.name}' has no surfacing layer '{name}'; its layers: {[layer['name'] for layer in layers]}")
  return layers


def readFaceInts(mesh, attributeName):
  values = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
  mesh.attributes[attributeName].data.foreach_get("value", values)
  return values


def writeFaceInts(mesh, attributeName, values):
  mesh.attributes[attributeName].data.foreach_set("value", numpy.asarray(values, dtype=numpy.int32))


def materialSlot(sceneObject, materialName):
  material = bpy.data.materials.get(materialName)
  if material is None:
    raise ValueError(f"No material named '{materialName}'")
  slot = next((index for index, existing in enumerate(sceneObject.material_slots) if existing.material == material), None)
  if slot is None:
    sceneObject.data.materials.append(material)
    slot = len(sceneObject.material_slots) - 1
  return slot


def compose(sceneObject):
  """Each face shows the topmost unmuted layer covering it, else the materials the faces had when the first layer was added."""
  mesh = sceneObject.data
  materialIndices = readFaceInts(mesh, baseAttributeName)
  for layer in layerOrder(sceneObject):
    if not layer["muted"]:
      values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
      materialIndices = numpy.where(values != uncovered, values, materialIndices)
  mesh.polygons.foreach_set("material_index", materialIndices)
  mesh.update()
  return describeLayers(sceneObject)


def describeLayers(sceneObject):
  mesh = sceneObject.data
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  described = []
  for layer in layerOrder(sceneObject):
    values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
    covered = values[values != uncovered]
    described.append({"name": layer["name"], "muted": layer["muted"], "faces": {slotNames[slot]: int((covered == slot).sum()) for slot in numpy.unique(covered)}})
  return {"object": sceneObject.name, "layers": described}


def hasSurfaceLayers(sceneObject):
  return bool(layerOrder(sceneObject))


def addSurfaceLayer(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = layerOrder(sceneObject)
  if not name or name in [layer["name"] for layer in layers]:
    raise ValueError(f"'{objectName}' already has a surfacing layer '{name}'" if name else "A surfacing layer needs a name")
  mesh = sceneObject.data
  if not layers:
    materialIndices = numpy.empty(len(mesh.polygons), dtype=numpy.int32)
    mesh.polygons.foreach_get("material_index", materialIndices)
    mesh.attributes.new(baseAttributeName, "INT", "FACE")
    writeFaceInts(mesh, baseAttributeName, materialIndices)
  mesh.attributes.new(layerAttributePrefix + name, "INT", "FACE")
  writeFaceInts(mesh, layerAttributePrefix + name, numpy.full(len(mesh.polygons), uncovered))
  sceneObject[layerOrderProperty] = json.dumps(layers + [{"name": name, "muted": False}])
  return compose(sceneObject)


def setSurfaceLayer(objectName, name, muted, position):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = requireLayer(sceneObject, name)
  if muted is None and position is None:
    raise ValueError("setSurfaceLayer needs muted or position")
  layer = next(layer for layer in layers if layer["name"] == name)
  if muted is not None:
    layer["muted"] = bool(muted)
  if position is not None:
    if not 0 <= position < len(layers):
      raise ValueError(f"position is 0 (bottom) to {len(layers) - 1} (top), got {position}")
    layers.remove(layer)
    layers.insert(position, layer)
  sceneObject[layerOrderProperty] = json.dumps(layers)
  return compose(sceneObject)


def removeSurfaceLayer(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = [layer for layer in requireLayer(sceneObject, name) if layer["name"] != name]
  mesh = sceneObject.data
  mesh.attributes.remove(mesh.attributes[layerAttributePrefix + name])
  sceneObject[layerOrderProperty] = json.dumps(layers)
  described = compose(sceneObject)
  if not layers:
    mesh.attributes.remove(mesh.attributes[baseAttributeName])
    del sceneObject[layerOrderProperty]
  return described


def faceCenters(sceneObject):
  with bridgeMeshAccess.shapedMesh(sceneObject) as mesh:
    centers = numpy.empty(len(mesh.polygons) * 3)
    mesh.polygons.foreach_get("center", centers)
  return bridgeMeshAccess.worldPositions(sceneObject, centers.reshape(-1, 3))


def nearestDistances(points, others):
  tree = mathutils.kdtree.KDTree(len(others))
  for index, point in enumerate(others):
    tree.insert(point, index)
  tree.balance()
  return numpy.array([tree.find(point)[2] for point in points])


def roughenedSelection(sceneObject, mask, edgeNoise):
  """The selection with its edge moved in and out by smooth noise up to about edgeNoise's amplitude, so a painted edge wanders as a
  painted one does instead of following a circle, a line, or the grid."""
  if edgeNoise is None or mask.all() or not mask.any():
    return mask
  unknown = sorted(set(edgeNoise) - {"featureSize", "amplitude", "seed"})
  if unknown or edgeNoise.get("amplitude", 0) <= 0:
    raise ValueError(f"edgeNoise is {{featureSize, amplitude (positive), seed}}, got {edgeNoise!r}")
  centers = faceCenters(sceneObject)
  amplitude = edgeNoise["amplitude"]
  # Only faces within twice the amplitude of the edge can cross it.
  signed = numpy.full(len(centers), numpy.inf)
  signed[mask] = nearestDistances(centers[mask], centers[~mask])
  signed[~mask] = -nearestDistances(centers[~mask], centers[mask])
  near = numpy.abs(signed) <= 2 * amplitude
  noise = bridgeShaping.fractalNoise(bridgeShaping.noiseSamplePoints(centers[near], edgeNoise["featureSize"], edgeNoise.get("seed", 0)), 2, 0.5)
  result = mask.copy()
  result[near] = signed[near] + amplitude * noise > 0
  return result


def paintSurface(objectName, layer, material, selector, edgeNoise):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = roughenedSelection(sceneObject, mask, edgeNoise)
  slot = materialSlot(sceneObject, material)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  values[mask] = slot
  writeFaceInts(sceneObject.data, layerAttributePrefix + layer, values)
  return {"painted": int(mask.sum())} | compose(sceneObject)


def eraseSurface(objectName, layer, selector, edgeNoise):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(mask, selector, sceneObject, "faces")
  mask = roughenedSelection(sceneObject, mask, edgeNoise)
  values = readFaceInts(sceneObject.data, layerAttributePrefix + layer)
  erased = int((mask & (values != uncovered)).sum())
  values[mask] = uncovered
  writeFaceInts(sceneObject.data, layerAttributePrefix + layer, values)
  return {"erased": erased} | compose(sceneObject)


def faceNeighbourPairs(mesh):
  """Pairs of faces sharing an edge, each pair once."""
  loopEdges = numpy.empty(len(mesh.loops), dtype=numpy.int64)
  mesh.loops.foreach_get("edge_index", loopEdges)
  loopTotals = numpy.empty(len(mesh.polygons), dtype=numpy.int64)
  mesh.polygons.foreach_get("loop_total", loopTotals)
  loopFaces = numpy.repeat(numpy.arange(len(mesh.polygons)), loopTotals)
  order = numpy.argsort(loopEdges, kind="stable")
  matching = numpy.flatnonzero(loopEdges[order][1:] == loopEdges[order][:-1])
  return loopFaces[order[matching]], loopFaces[order[matching + 1]]


def editSurface(objectName, layer, operation, steps, selector):
  """Grow a layer's covered faces outward, shrink them inward, or smooth them (each face takes the value most of itself and its
  neighbours hold, which absorbs islands and evens ragged edges), within the selector."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if operation not in surfaceOperations:
    raise ValueError(f"operation is one of {list(surfaceOperations)}, got '{operation}'")
  if not isinstance(steps, int) or steps < 1:
    raise ValueError(f"steps is a positive whole number, got {steps!r}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  mesh = sceneObject.data
  first, second = faceNeighbourPairs(mesh)
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  before = values.copy()
  for _ in range(steps):
    updated = values.copy()
    if operation == "grow":
      for source, target in ((first, second), (second, first)):
        spreading = (values[source] != uncovered) & (values[target] == uncovered)
        updated[target[spreading]] = values[source[spreading]]
    elif operation == "shrink":
      for source, target in ((first, second), (second, first)):
        updated[target[(values[source] == uncovered) & (values[target] != uncovered)]] = uncovered
    else:
      candidates = numpy.unique(values)
      votes = numpy.zeros((len(values), len(candidates)))
      columns = numpy.searchsorted(candidates, values)
      votes[numpy.arange(len(values)), columns] += 1.5
      numpy.add.at(votes, (first, columns[second]), 1)
      numpy.add.at(votes, (second, columns[first]), 1)
      updated = candidates[votes.argmax(axis=1)]
    values = numpy.where(within, updated, values)
  writeFaceInts(mesh, layerAttributePrefix + layer, values)
  return {"changed": int((values != before).sum())} | compose(sceneObject)


# Broad strokes: taking an area back

def resetRegion(objectName, selector, passes, fadeDistance):
  """Take shaping back inside the selection: each named pass (every pass when none are named) loses what it moved there, faded out
  over fadeDistance from the selection's edge so the area rejoins its surroundings."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  available = [entry["name"] for entry in bridgePasses.passList(sceneObject)]
  if not available:
    raise ValueError(f"'{objectName}' has no shaping passes to take back; rebuildRegion reshapes an area without them")
  names = available if passes is None else passes
  unknown = sorted(set(names) - set(available))
  if unknown:
    raise ValueError(f"'{objectName}' has no shaping passes {unknown}; its passes: {available}")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = bridgeShaping.maskWeights(sceneObject, selector, fadeDistance, positions)
  keys = sceneObject.data.shape_keys
  base = bridgePasses.keyCoordinates(keys.reference_key)
  for name in names:
    key = keys.key_blocks[name]
    coordinates = bridgePasses.keyCoordinates(key)
    key.data.foreach_set("co", (base + (coordinates - base) * (1 - weights)[:, None]).ravel())
  sceneObject.data.update()
  return {"object": objectName, "passes": names, "affectedVertices": int((weights > 0).sum())}


def relaxedHeights(sceneObject, positions, free):
  """Heights for the free vertices that span smoothly between the fixed ones around them, each the mean of its neighbours: the
  discrete Laplace equation over the mesh's edges, solved by conjugate gradients."""
  edges = numpy.empty(len(sceneObject.data.edges) * 2, dtype=numpy.int64)
  sceneObject.data.edges.foreach_get("vertices", edges)
  first, second = edges[0::2], edges[1::2]
  heights = positions[:, 2].copy()
  freeIndices = numpy.flatnonzero(free)
  local = numpy.full(len(heights), -1)
  local[freeIndices] = numpy.arange(len(freeIndices))
  degrees = numpy.bincount(edges, minlength=len(heights))[freeIndices].astype(numpy.float64)
  if (degrees == 0).any():
    raise ValueError("The selection holds loose vertices with no neighbours to take a height from")
  knowns = numpy.zeros(len(freeIndices))
  for this, other in ((first, second), (second, first)):
    toFixed = free[this] & ~free[other]
    numpy.add.at(knowns, local[this[toFixed]], heights[other[toFixed]])
  inner = free[first] & free[second]
  innerFirst, innerSecond = local[first[inner]], local[second[inner]]

  def laplacian(values):
    result = degrees * values
    numpy.subtract.at(result, innerFirst, values[innerSecond])
    numpy.subtract.at(result, innerSecond, values[innerFirst])
    return result

  solution = heights[freeIndices]
  residual = knowns - laplacian(solution)
  direction = residual.copy()
  squared = float(residual @ residual)
  target = relaxTolerance * max(float(knowns @ knowns), 1.0)
  for _ in range(4 * len(freeIndices) + 100):
    if squared <= target:
      heights[freeIndices] = solution
      return heights
    stepped = laplacian(direction)
    step = squared / float(direction @ stepped)
    solution = solution + step * direction
    residual = residual - step * stepped
    nextSquared = float(residual @ residual)
    direction = residual + (nextSquared / squared) * direction
    squared = nextSquared
  raise ValueError("The rebuilt heights did not settle; every part of the selection must touch ground outside it to take heights from")


def rebuildRegion(objectName, selector, mode, height, fadeDistance):
  """Give the selection a fresh start: heights spanned smoothly from its surroundings (surroundings), or one level (height), faded in
  over fadeDistance from its edge. With shaping passes, the change goes into the active pass."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  if mode not in rebuildModes:
    raise ValueError(f"mode is one of {list(rebuildModes)}, got '{mode}'")
  if (mode == "height") != (height is not None):
    raise ValueError("A height rebuild takes a height, and only it does")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  weights = bridgeShaping.maskWeights(sceneObject, selector, fadeDistance, positions)
  free = weights > 0
  if mode == "surroundings":
    if free.all():
      raise ValueError("The selection covers the whole mesh, so there are no surroundings to span from")
    targets = relaxedHeights(sceneObject, positions, free)
  else:
    targets = numpy.full(len(positions), float(height))
  updated = positions.copy()
  updated[:, 2] += weights * (targets - positions[:, 2])
  bridgeShaping.writeWorldPositions(sceneObject, updated)
  return {"object": objectName, "mode": mode, "affectedVertices": int(free.sum()), "largestMove": round(float(numpy.abs(updated[:, 2] - positions[:, 2]).max()), 3)}


def objectsInRegion(regionName, terrainNames):
  """Rendered objects other than the terrain and regions whose origin lies inside the region, lights and emitters included."""
  regionObject = bridgeMeshAccess.requireRegion(regionName)
  candidates = [sceneObject for sceneObject in bpy.context.scene.objects
    if sceneObject.name not in terrainNames and bridgeMeshAccess.regionIntentProperty not in sceneObject and sceneObject.type != "CAMERA"
    and (not sceneObject.hide_render or bridgeEnvironment.isEmitter(sceneObject))]
  if not candidates:
    return []
  origins = numpy.array([list(sceneObject.matrix_world.translation) for sceneObject in candidates])
  inside = bridgeMeshAccess.insideRegion(regionObject, origins)
  return [sceneObject.name for sceneObject, isInside in zip(candidates, inside) if isInside]


def clearRegion(region, terrainObject, shaping, surfacing, objects, fadeDistance):
  """Take a region back to start it again: its shaping kept, reset (passes taken back), or rebuilt (spanned from its surroundings);
  its surfacing erased from every layer of the terrain; and the objects placed in it deleted."""
  if shaping not in shapingChoices:
    raise ValueError(f"shaping is one of {list(shapingChoices)}, got '{shaping}'")
  if not isinstance(surfacing, bool) or not isinstance(objects, bool):
    raise ValueError("surfacing and objects are true or false")
  bridgeMeshAccess.requireRegion(region)
  selector = {"region": region}
  outcome = {"region": region}
  if shaping == "reset":
    outcome["shaping"] = resetRegion(terrainObject, selector, None, fadeDistance)
  elif shaping == "rebuild":
    outcome["shaping"] = rebuildRegion(terrainObject, selector, "surroundings", None, fadeDistance)
  if surfacing:
    sceneObject = bridgeMeshAccess.requireMeshObject(terrainObject)
    layers = layerOrder(sceneObject)
    if not layers:
      raise ValueError(f"'{terrainObject}' has no surfacing layers to erase from")
    mask = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
    erased = 0
    for layer in layers:
      values = readFaceInts(sceneObject.data, layerAttributePrefix + layer["name"])
      erased += int((mask & (values != uncovered)).sum())
      values[mask] = uncovered
      writeFaceInts(sceneObject.data, layerAttributePrefix + layer["name"], values)
    compose(sceneObject)
    outcome["surfacing"] = {"erasedFaces": erased}
  if objects:
    terrainCollection = bpy.data.collections.get(bridgeExport.terrainCollectionName)
    terrainNames = {member.name for member in terrainCollection.all_objects} if terrainCollection else set()
    names = objectsInRegion(region, terrainNames | {terrainObject})
    for name in names:
      bpy.data.objects.remove(bpy.data.objects[name])
    outcome["objects"] = {"deleted": names}
  return outcome


commands = {
  "createRegion": (createRegion, True),
  "editRegion": (editRegion, True),
  "getRegions": (getRegions, False),
  "addSurfaceLayer": (addSurfaceLayer, True),
  "setSurfaceLayer": (setSurfaceLayer, True),
  "removeSurfaceLayer": (removeSurfaceLayer, True),
  "paintSurface": (paintSurface, True),
  "eraseSurface": (eraseSurface, True),
  "editSurface": (editSurface, True),
  "resetRegion": (resetRegion, True),
  "rebuildRegion": (rebuildRegion, True),
  "clearRegion": (clearRegion, True),
}
