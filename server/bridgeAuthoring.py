"""Authoring a zone the way an environment artist does: regions that say what an area is to become, surfacing layers painted by intent
and edited at their boundaries, and broad strokes that take an area back to start it again. Runs under Blender's Python."""
import json
import math

import bpy
import mathutils
import mathutils.kdtree
import numpy

import bridgeEnvironment
import bridgeExport
import bridgeMeshAccess
import bridgeNoise
import bridgeObjects
import bridgePasses
import bridgeShaping
import bridgeSurfacing

regionCollectionName = "regions"
layerOrderProperty = bridgeMeshAccess.surfaceLayersProperty
baseAttributeName = "zonewrightSurfaceBase"
layerAttributePrefix = "zonewrightSurface:"
uncovered = -1
surfaceOperations = ("grow", "shrink", "smooth", "clean")
# Snapped vertices whose faces would turn over or shrink below this share of their area go back, over up to this many rounds.
conformSliverShare = 0.1
conformRepairs = 4
# A vertex where a face around it turns more than this from their mean stays put: it sits on a crease (a cliff's edge), which
# sliding it would move, and a border there already runs on a modeled edge.
conformCreaseDegrees = 20
# A transition strip takes faces reaching this fraction past its width, so a contour cut at the width counts as inside it.
transitionTolerance = 1e-4
# Cleaning takes over small pieces, then pieces the first takeover left small, for up to this many rounds.
cleanRounds = 4
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

def requireLayer(sceneObject, name):
  layers = bridgeMeshAccess.surfaceLayers(sceneObject)
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


def shownSurface(sceneObject):
  """The material each face shows (the topmost unmuted layer covering it, else the material it had when the first layer was added)
  and the position in the layer order of the layer that decides it, -1 for the base."""
  mesh = sceneObject.data
  materialIndices = readFaceInts(mesh, baseAttributeName)
  deciders = numpy.full(len(materialIndices), -1)
  for position, layer in enumerate(bridgeMeshAccess.surfaceLayers(sceneObject)):
    if not layer["muted"]:
      values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
      covered = values != uncovered
      materialIndices = numpy.where(covered, values, materialIndices)
      deciders = numpy.where(covered, position, deciders)
  return materialIndices, deciders


def compose(sceneObject):
  mesh = sceneObject.data
  mesh.polygons.foreach_set("material_index", shownSurface(sceneObject)[0])
  mesh.update()
  return describeLayers(sceneObject)


def describeLayers(sceneObject):
  mesh = sceneObject.data
  slotNames = [slot.material.name if slot.material else None for slot in sceneObject.material_slots]
  described = []
  for layer in bridgeMeshAccess.surfaceLayers(sceneObject):
    values = readFaceInts(mesh, layerAttributePrefix + layer["name"])
    covered = values[values != uncovered]
    described.append({"name": layer["name"], "muted": layer["muted"], "faces": {slotNames[slot]: int((covered == slot).sum()) for slot in numpy.unique(covered)}})
  return {"object": sceneObject.name, "layers": described}


def hasSurfaceLayers(sceneObject):
  return bool(bridgeMeshAccess.surfaceLayers(sceneObject))


def addSurfaceLayer(objectName, name):
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  layers = bridgeMeshAccess.surfaceLayers(sceneObject)
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
  noise = bridgeNoise.fractalNoise(bridgeNoise.noiseSamplePoints(centers[near], edgeNoise["featureSize"], edgeNoise.get("seed", 0)), 2, 0.5)
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


def editSurface(objectName, layer, operation, steps, selector, minimumArea):
  """Grow a layer's covered faces outward, shrink them inward, or smooth them (each face takes the value most of itself and its
  neighbours hold, which absorbs islands and evens ragged edges), within the selector; or clean it, every island and hole smaller
  than minimumArea taken over by what surrounds it."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if operation not in surfaceOperations:
    raise ValueError(f"operation is one of {list(surfaceOperations)}, got '{operation}'")
  if not isinstance(steps, int) or steps < 1:
    raise ValueError(f"steps is a positive whole number, got {steps!r}")
  if (operation == "clean") != (minimumArea is not None):
    raise ValueError("clean needs a minimumArea, and only clean takes one")
  if minimumArea is not None and minimumArea <= 0:
    raise ValueError(f"minimumArea must be positive, got {minimumArea}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  mesh = sceneObject.data
  first, second = faceNeighbourPairs(mesh)
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  before = values.copy()
  if operation == "clean":
    positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
    shown, deciders = shownSurface(sceneObject)
    position = [entry["name"] for entry in bridgeMeshAccess.surfaceLayers(sceneObject)].index(layer)
    cleaned = cleanedValues(mesh, shown, within & (deciders <= position), faceAreas(sceneObject, positions), minimumArea)
    values = numpy.where(cleaned != shown, cleaned, values)
    writeFaceInts(mesh, layerAttributePrefix + layer, values)
    return {"changed": int((cleaned != shown).sum())} | compose(sceneObject)
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


def faceAreas(sceneObject, positions):
  return numpy.linalg.norm(bridgeMeshAccess.faceNormals(sceneObject, positions), axis=1)


def loopNeighbours(sceneObject):
  """For each face corner, the corner after it and the corner before it around the same face."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopStarts = numpy.repeat(numpy.cumsum(loopTotals) - loopTotals, loopTotals)
  repeated = numpy.repeat(loopTotals, loopTotals)
  offsets = numpy.arange(len(loopVertices)) - loopStarts
  return loopStarts + (offsets + 1) % repeated, loopStarts + (offsets - 1) % repeated


def movableVertices(sceneObject, within, positions):
  """Vertices every face around which lies within the face mask, off the mesh's open edge, and not on a crease."""
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  outside = numpy.bincount(loopVertices[~within[loopFaces]], minlength=len(positions))
  normals = bridgeMeshAccess.faceNormals(sceneObject, positions)
  vertexNormals = numpy.zeros_like(positions)
  numpy.add.at(vertexNormals, loopVertices, normals[loopFaces])
  vertexNormals /= numpy.maximum(numpy.linalg.norm(vertexNormals, axis=1, keepdims=True), 1e-12)
  units = normals / numpy.maximum(numpy.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
  flattest = numpy.ones(len(positions))
  numpy.minimum.at(flattest, loopVertices, (units[loopFaces] * vertexNormals[loopVertices]).sum(axis=1))
  return (outside == 0) & ~bridgeMeshAccess.boundaryVertexMask(sceneObject) & (flattest >= math.cos(math.radians(conformCreaseDegrees)))


def classField(sceneObject, values, smoothing, positions, edges, movable):
  """For each vertex, how much of the surface around it holds each value of a layer (uncovered among them), evened out over about
  `smoothing` world units by repeated averaging with its neighbours, only between vertices off creases, so a border on a crease (a
  cliff's edge) does not spread onto the flat beside it."""
  classes, faceClass = numpy.unique(values, return_inverse=True)
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  field = numpy.zeros((len(positions), len(classes)))
  numpy.add.at(field, (loopVertices, faceClass[loopFaces]), faceAreas(sceneObject, positions)[loopFaces])
  field /= numpy.maximum(field.sum(axis=1, keepdims=True), 1e-12)
  edgeLength = float(numpy.median(numpy.linalg.norm(positions[edges[:, 0]] - positions[edges[:, 1]], axis=1)))
  # Each half-and-half averaging step spreads a value about a third of an edge length squared along each axis.
  steps = math.ceil(3 * (smoothing / edgeLength) ** 2)
  edges = edges[movable[edges[:, 0]] & movable[edges[:, 1]]]
  degree = numpy.bincount(edges.ravel(), minlength=len(positions))[:, None]
  for _ in range(steps):
    neighbours = numpy.zeros_like(field)
    numpy.add.at(neighbours, edges[:, 0], field[edges[:, 1]])
    numpy.add.at(neighbours, edges[:, 1], field[edges[:, 0]])
    field = numpy.where(degree > 0, 0.5 * field + 0.5 * neighbours / numpy.maximum(degree, 1), field)
  return classes, faceClass, field


def borderSlides(field, edges, positions, movable):
  """Where the field's leading value changes along an edge, the point between its ends where the two values balance; the nearer end
  slides onto it. Each vertex takes the nearest such point; returned with its partner along that edge and how far toward it it slides."""
  labels = field.argmax(axis=1)
  crossing = (labels[edges[:, 0]] != labels[edges[:, 1]]) & movable[edges[:, 0]] & movable[edges[:, 1]]
  first, second = edges[crossing, 0], edges[crossing, 1]
  firstClass, secondClass = labels[first], labels[second]
  atFirst = field[first, firstClass] - field[first, secondClass]
  atSecond = field[second, firstClass] - field[second, secondClass]
  balance = atFirst / (atFirst - atSecond)
  nearFirst = balance <= 0.5
  movers = numpy.where(nearFirst, first, second)
  partners = numpy.where(nearFirst, second, first)
  fractions = numpy.where(nearFirst, balance, 1 - balance)
  distances = fractions * numpy.linalg.norm(positions[first] - positions[second], axis=1)
  order = numpy.lexsort((distances, movers))
  nearest = order[numpy.r_[True, movers[order][1:] != movers[order][:-1]]] if len(order) else order
  return movers[nearest], partners[nearest], fractions[nearest]


def slideAlongEdges(sceneObject, movers, partners, fractions):
  """Move each vertex the given fraction toward its partner in the mesh and in every shaping pass alike, so the passes stay as they
  were relative to each other, and carry each face's UVs along with the move."""
  mesh = sceneObject.data
  _, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  with bridgeMeshAccess.shapedMesh(sceneObject) as shown:
    before = numpy.empty(len(shown.vertices) * 3)
    shown.vertices.foreach_get("co", before)
  before = before.reshape(-1, 3)
  blocks = [mesh.vertices] + ([key.data for key in mesh.shape_keys.key_blocks] if mesh.shape_keys else [])
  for block in blocks:
    coordinates = numpy.empty(len(block) * 3)
    block.foreach_get("co", coordinates)
    coordinates = coordinates.reshape(-1, 3)
    coordinates[movers] += fractions[:, None] * (coordinates[partners] - coordinates[movers])
    block.foreach_set("co", coordinates.ravel())
  moves = numpy.zeros_like(before)
  moves[movers] = fractions[:, None] * (before[partners] - before[movers])
  nextLoops, previousLoops = loopNeighbours(sceneObject)
  moved = numpy.flatnonzero(numpy.isin(loopVertices, movers))
  here = before[loopVertices[moved]]
  spans = numpy.stack([before[loopVertices[nextLoops[moved]]] - here, before[loopVertices[previousLoops[moved]]] - here], axis=2)
  # The move in each face's own two spans, by least squares, carries that face's UVs as its mapping runs across it.
  weights = numpy.linalg.solve(numpy.einsum("lji,ljk->lik", spans, spans), numpy.einsum("lji,lj->li", spans, moves[loopVertices[moved]])[..., None])[..., 0]
  for uvLayer in mesh.uv_layers:
    uvs = numpy.empty(len(mesh.loops) * 2)
    uvLayer.data.foreach_get("uv", uvs)
    uvs = uvs.reshape(-1, 2)
    uvSpans = numpy.stack([uvs[nextLoops[moved]] - uvs[moved], uvs[previousLoops[moved]] - uvs[moved]], axis=2)
    uvs[moved] += numpy.einsum("lik,lk->li", uvSpans, weights)
    uvLayer.data.foreach_set("uv", uvs.ravel())
  mesh.update()


def conformSurfaceEdges(objectName, layer, smoothing, selector):
  """Bring a surfacing layer's edges onto the mesh's own edges along a smooth line: the layer's coverage is evened out over
  `smoothing` units, the vertex nearest where each edge of the mesh crosses the evened line slides along that edge onto it (in every
  shaping pass alike, carrying UVs), and the faces take the side they now lie on, so the border runs on modeled edges without saw
  teeth or one-face islands."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if smoothing <= 0:
    raise ValueError(f"smoothing must be positive, got {smoothing}")
  within = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  bridgeMeshAccess.requireSelection(within, selector, sceneObject, "faces")
  mesh = sceneObject.data
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  movable = movableVertices(sceneObject, within, positions)
  classes, faceClass, field = classField(sceneObject, values, smoothing, positions, edges, movable)
  if len(classes) < 2:
    raise ValueError(f"Layer '{layer}' of '{objectName}' holds one value everywhere, so it has no edge to conform")
  movers, partners, fractions = borderSlides(field, edges, positions, movable)
  areasBefore = faceAreas(sceneObject, positions)
  normalsBefore = bridgeMeshAccess.faceNormals(sceneObject, positions)
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  kept = numpy.zeros(len(positions), dtype=bool)
  for _ in range(conformRepairs):
    active = ~kept[movers]
    updated = positions.copy()
    updated[movers[active]] += fractions[active, None] * (positions[partners[active]] - positions[movers[active]])
    normalsAfter = bridgeMeshAccess.faceNormals(sceneObject, updated)
    spoiled = ((normalsBefore * normalsAfter).sum(axis=1) <= 0) | (numpy.linalg.norm(normalsAfter, axis=1) < conformSliverShare * areasBefore)
    newlyKept = numpy.intersect1d(numpy.unique(loopVertices[spoiled[loopFaces]]), movers[active])
    if not len(newlyKept):
      break
    kept[newlyKept] = True
  active = ~kept[movers]
  movers, partners, fractions = movers[active], partners[active], fractions[active]
  if len(movers):
    slideAlongEdges(sceneObject, movers, partners, fractions)
  onLine = numpy.zeros(len(positions), dtype=bool)
  onLine[movers] = True
  votes = numpy.zeros((len(loopTotals), len(classes)))
  counted = movable[loopVertices] & ~onLine[loopVertices]
  numpy.add.at(votes, loopFaces[counted], field[loopVertices[counted]])
  newClass = numpy.where(votes.sum(axis=1) > 0, votes.argmax(axis=1), faceClass)
  # A face on a crease keeps its value unless a corner of it moved: the evened field runs across the crease, the material should not.
  touchesMove = numpy.bincount(loopFaces, weights=onLine[loopVertices], minlength=len(loopTotals)) > 0
  allMovable = numpy.bincount(loopFaces, weights=~movable[loopVertices], minlength=len(loopTotals)) == 0
  updatedValues = numpy.where(within & (touchesMove | allMovable), classes[newClass], values)
  writeFaceInts(mesh, layerAttributePrefix + layer, updatedValues)
  return {
    "movedVertices": int(len(movers)), "keptInPlace": int(kept.sum()), "changedFaces": int((updatedValues != values).sum()),
  } | compose(sceneObject)


def faceComponents(first, second, count):
  """Each face's connected piece over the given neighbour pairs, named by the smallest face index in it."""
  component = numpy.arange(count)
  while True:
    lower = numpy.minimum(component[first], component[second])
    previous = component.copy()
    numpy.minimum.at(component, first, lower)
    numpy.minimum.at(component, second, lower)
    component = component[component]
    if (component == previous).all():
      return component


def cleanedValues(mesh, values, within, areas, minimumArea):
  """Values with every piece of one value smaller than minimumArea (a speck of a material, a hole in one) lying wholly inside the face
  mask taken over by the value it shares the most edges with."""
  first, second = faceNeighbourPairs(mesh)
  values = values.copy()
  for _ in range(cleanRounds):
    same = values[first] == values[second]
    component = faceComponents(first[same], second[same], len(values))
    pieceArea = numpy.bincount(component, weights=areas, minlength=len(values))
    pieceInside = numpy.bincount(component, weights=(~within).astype(numpy.float64), minlength=len(values)) == 0
    small = (pieceArea[component] < minimumArea) & pieceInside[component]
    border = ~same
    sides = numpy.r_[first[border], second[border]]
    across = numpy.r_[second[border], first[border]]
    taking = small[sides]
    if not taking.any():
      break
    pairs, counts = numpy.unique(numpy.column_stack([component[sides[taking]], values[across[taking]]]), axis=0, return_counts=True)
    order = numpy.lexsort((-counts, pairs[:, 0]))
    winners = pairs[order][numpy.r_[True, pairs[order][1:, 0] != pairs[order][:-1, 0]]]
    replacement = numpy.full(len(values), numpy.iinfo(numpy.int64).min)
    replacement[winners[:, 0]] = winners[:, 1]
    smallFaces = numpy.flatnonzero(small & (replacement[component] != numpy.iinfo(numpy.int64).min))
    values[smallFaces] = replacement[component[smallFaces]]
  return values


def borderChains(segments):
  """Border edges [vertex, vertex] joined end to end into chains, each a list of vertex indices."""
  touching = {}
  for index, (start, end) in enumerate(segments):
    touching.setdefault(int(start), []).append(index)
    touching.setdefault(int(end), []).append(index)
  used = numpy.zeros(len(segments), dtype=bool)
  chains = []
  for seed in range(len(segments)):
    if used[seed]:
      continue
    used[seed] = True
    chain = [int(segments[seed][0]), int(segments[seed][1])]
    for forward in (True, False):
      while True:
        tip = chain[-1] if forward else chain[0]
        onward = [index for index in touching[tip] if not used[index]] if len(touching[tip]) == 2 else []
        if not onward:
          break
        used[onward[0]] = True
        start, end = (int(vertex) for vertex in segments[onward[0]])
        following = end if start == tip else start
        if forward:
          chain.append(following)
        else:
          chain.insert(0, following)
    chains.append(chain)
  return chains


def paintTransition(objectName, layer, material, selector, toward, width, worldUnitsPerRepeat, onlyAbove):
  """Paint the faces lying wholly within `width` of where the faces picked by `selector` meet those picked by `toward`, on the
  selector's side, and map them so the texture's bottom edge lies on that border and its top `width` away, repeating along the border
  every worldUnitsPerRepeat units: how a transition texture blends one ground into the next. Faces straddling `width` are left and
  counted; cut a contour there first (cutContours with distanceFrom) so the strip ends on a modeled edge. With onlyAbove, only faces
  lying above the nearest point of the border are painted: a wall's foot, not the lip where ground ends above rock falling away."""
  sceneObject = bridgeMeshAccess.requireMeshObject(objectName)
  requireLayer(sceneObject, layer)
  if width <= 0 or worldUnitsPerRepeat <= 0:
    raise ValueError(f"width and worldUnitsPerRepeat must be positive, got {width} and {worldUnitsPerRepeat}")
  mesh = sceneObject.data
  side = bridgeMeshAccess.evaluateSelector(selector, sceneObject, "faces")
  other = bridgeMeshAccess.evaluateSelector(toward, sceneObject, "faces") & ~side
  borderEdges = bridgeMeshAccess.faceBorderEdges(sceneObject, side, other)
  if not len(borderEdges):
    raise ValueError(f"The faces {selector!r} picks never meet the faces {toward!r} picks on '{objectName}'")
  positions, _ = bridgeMeshAccess.readVertexArrays(sceneObject)
  edges = bridgeMeshAccess.meshEdges(mesh)
  chains = borderChains(edges[borderEdges])
  segments = numpy.array([[start, end] for chain in chains for start, end in zip(chain, chain[1:])])
  lengths = numpy.linalg.norm(positions[segments[:, 1]] - positions[segments[:, 0]], axis=1)
  chainStarts = numpy.cumsum([0] + [len(chain) - 1 for chain in chains])[:-1]
  travelled = numpy.cumsum(lengths) - lengths
  segmentArcs = travelled - numpy.repeat(travelled[chainStarts], [len(chain) - 1 for chain in chains])
  border = bridgeMeshAccess.BorderDistance(positions[segments[:, 0]], positions[segments[:, 1]])
  loopTotals, loopVertices = bridgeMeshAccess.faceLoops(sceneObject)
  loopFaces = numpy.repeat(numpy.arange(len(loopTotals)), loopTotals)
  distances = numpy.full(len(positions), numpy.inf)
  owner = numpy.zeros(len(positions), dtype=numpy.int64)
  fraction = numpy.zeros(len(positions))
  for vertex in numpy.unique(loopVertices[side[loopFaces]]):
    distances[vertex], owner[vertex], fraction[vertex] = border.nearest(positions[vertex])
  farthest = numpy.zeros(len(loopTotals))
  closest = numpy.full(len(loopTotals), numpy.inf)
  numpy.maximum.at(farthest, loopFaces, distances[loopVertices])
  numpy.minimum.at(closest, loopFaces, distances[loopVertices])
  reach = width * (1 + transitionTolerance)
  near = side & (closest < width * (1 - transitionTolerance))
  # A face between two stretches of border has every corner near one, yet its middle lies far from both: no strip runs across it.
  centered = numpy.zeros(len(loopTotals), dtype=bool)
  centers = faceCenters(sceneObject)
  above = numpy.ones(len(loopTotals), dtype=bool)
  for face in numpy.flatnonzero(near):
    distance, segment, along = border.nearest(centers[face])
    centered[face] = distance <= reach
    above[face] = centers[face][2] > (border.starts[segment] + along * border.spans[segment])[2]
  strip = near & centered & (farthest <= reach) & (above | (not onlyAbove))
  straddling = near & ~strip
  stripLoops = numpy.flatnonzero(strip[loopFaces])
  stripVertices = loopVertices[stripLoops]
  along = segmentArcs[owner[stripVertices]] + fraction[stripVertices] * lengths[owner[stripVertices]]
  if bridgeSurfacing.uvLayerName not in mesh.uv_layers:
    mesh.uv_layers.new(name=bridgeSurfacing.uvLayerName)
  uvLayer = mesh.uv_layers[bridgeSurfacing.uvLayerName]
  uvs = numpy.empty(len(mesh.loops) * 2)
  uvLayer.data.foreach_get("uv", uvs)
  uvs = uvs.reshape(-1, 2)
  uvs[stripLoops, 0] = along / worldUnitsPerRepeat
  uvs[stripLoops, 1] = numpy.minimum(distances[stripVertices] / width, 1.0)
  uvLayer.data.foreach_set("uv", uvs.ravel())
  values = readFaceInts(mesh, layerAttributePrefix + layer)
  values[strip] = materialSlot(sceneObject, material)
  writeFaceInts(mesh, layerAttributePrefix + layer, values)
  return {"painted": int(strip.sum()), "straddlingFaces": int(straddling.sum()), "borderLength": round(float(lengths.sum()), 1)} | compose(sceneObject)


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
    layers = bridgeMeshAccess.surfaceLayers(sceneObject)
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
  "conformSurfaceEdges": (conformSurfaceEdges, True),
  "paintTransition": (paintTransition, True),
  "resetRegion": (resetRegion, True),
  "rebuildRegion": (rebuildRegion, True),
  "clearRegion": (clearRegion, True),
}
